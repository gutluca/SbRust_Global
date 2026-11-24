#!/usr/bin/env python3
"""Create Pythia point sites and a harvested-area raster for a country/region."""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import fiona
import numpy as np
import rasterio
from rasterio.transform import from_origin
from rasterio.windows import Window


def weather_cells(index_path: Path) -> dict[tuple[float, float], int]:
    cells: dict[tuple[float, float], int] = {}
    with index_path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            cells[(round(float(row["LATITUDE"]), 5), round(float(row["LONGITUDE"]), 5))] = int(row["CellID"])
    if not cells:
        raise ValueError(f"Weather index is empty: {index_path}")
    return cells


def global_cell_id(latitude: float, longitude: float, resolution: float) -> int:
    """ID of a missing 0.5-degree WTH grid cell, independent of its donor."""
    columns = round(360 / resolution)
    rows = round(180 / resolution)
    if not math.isclose(columns * resolution, 360) or not math.isclose(rows * resolution, 180):
        raise ValueError("Weather resolution must tile the globe exactly")
    row = round((90 - resolution / 2 - latitude) / resolution)
    col = round((longitude + 180 - resolution / 2) / resolution)
    if not 0 <= row < rows or not 0 <= col < columns:
        raise ValueError(f"Grid point outside global extent: {(latitude, longitude)}")
    return row * columns + col + 1


def nearest_weather(latitude: float, longitude: float, indexed: list[tuple[int, float, float]]) -> tuple[int, float, float, float]:
    """Choose the nearest indexed WTH by great-circle distance, then CellID."""
    lat1 = math.radians(latitude)
    lon1 = math.radians(longitude)

    def distance(item: tuple[int, float, float]) -> tuple[float, int]:
        cell_id, lat, lon = item
        dlat = math.radians(lat) - lat1
        dlon = math.radians(lon) - lon1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(math.radians(lat)) * math.sin(dlon / 2) ** 2
        return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(a))), cell_id

    cell_id, lat, lon = min(indexed, key=distance)
    return cell_id, lat, lon, distance((cell_id, lat, lon))[0]


def selected_geometry(path: Path, names: list[str], field: str) -> list[list[list[tuple[float, float]]]]:
    with fiona.open(path) as src:
        available = [dict(feature["properties"])[field] for feature in src]
    missing = sorted(set(names) - set(available))
    if missing:
        raise ValueError(f"Country names not found in {field}: {missing}. Available examples: {available[:10]}")
    polygons: list[list[list[tuple[float, float]]]] = []
    with fiona.open(path) as src:
        for feature in src:
            if dict(feature["properties"])[field] not in names:
                continue
            geometry = feature["geometry"]
            if geometry["type"] == "Polygon":
                polygons.append(geometry["coordinates"])
            elif geometry["type"] == "MultiPolygon":
                polygons.extend(geometry["coordinates"])
    return polygons


def inside_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test; country grids do not rely on boundaries."""
    inside = False
    previous_lon, previous_lat = ring[-1]
    for current_lon, current_lat in ring:
        crosses = (current_lat > lat) != (previous_lat > lat)
        if crosses and lon < (previous_lon - current_lon) * (lat - current_lat) / (previous_lat - current_lat) + current_lon:
            inside = not inside
        previous_lon, previous_lat = current_lon, current_lat
    return inside


def inside_region(lon: float, lat: float, polygons: list[list[list[tuple[float, float]]]]) -> bool:
    for polygon in polygons:
        if inside_ring(lon, lat, polygon[0]) and not any(inside_ring(lon, lat, hole) for hole in polygon[1:]):
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spam", required=True, type=Path)
    parser.add_argument("--countries", required=True, type=Path)
    parser.add_argument("--weather-index", required=True, type=Path)
    parser.add_argument("--country", action="append", required=True, help="Exact CNTRY_NAME; repeat for a region.")
    parser.add_argument("--country-field", default="CNTRY_NAME")
    parser.add_argument("--resolution", type=float, default=0.5)
    parser.add_argument("--sites", required=True, type=Path, help="Output GeoPackage (.gpkg).")
    parser.add_argument("--harvest-area", required=True, type=Path, help="Output aggregated GeoTIFF.")
    parser.add_argument(
        "--max-no-weather", type=int,
        help="Fail if more soybean grid cells than this lack a historical WTH match.",
    )
    parser.add_argument("--weather-fallback", choices=("none", "nearest"), default="none")
    parser.add_argument("--weather-assignment-audit", type=Path)
    args = parser.parse_args()
    if args.weather_fallback == "nearest" and args.weather_assignment_audit is None:
        parser.error("--weather-assignment-audit is required with nearest WTH fallback")
    for output in (args.sites, args.harvest_area, args.weather_assignment_audit):
        if output is not None and output.exists():
            raise FileExistsError(f"Refusing to overwrite output: {output}")

    region = selected_geometry(args.countries, args.country, args.country_field)
    region_points = [point for polygon in region for ring in polygon for point in ring]
    west, south = min(point[0] for point in region_points), min(point[1] for point in region_points)
    east, north = max(point[0] for point in region_points), max(point[1] for point in region_points)
    weather = weather_cells(args.weather_index)
    indexed_weather = sorted((cell_id, lat, lon) for (lat, lon), cell_id in weather.items())
    indexed_ids = {item[0] for item in indexed_weather}
    with rasterio.open(args.spam) as spam:
        if str(spam.crs) != "EPSG:4326":
            raise ValueError("SPAM raster must be EPSG:4326")
        native = abs(spam.transform.a)
        factor = args.resolution / native
        if not math.isclose(factor, round(factor), abs_tol=1e-6):
            raise ValueError(f"resolution {args.resolution} is not an integer multiple of source resolution {native}")
        factor = int(round(factor))
        # SPAM is finer than the DSSAT climate grid.  Associate each weather
        # cell with the enclosing requested-resolution SPAM block instead of
        # requiring its latitude/longitude to equal a fine SPAM pixel centre.
        # The previous exact-coordinate lookup made every Brazilian block
        # appear to have no weather.
        weather_by_block: dict[tuple[int, int], list[tuple[int, float, float]]] = {}
        for (lat, lon), cell_id in weather.items():
            weather_row, weather_col = spam.index(lon, lat)
            block = (weather_row // factor, weather_col // factor)
            weather_by_block.setdefault(block, []).append((cell_id, lat, lon))
        first_row, first_col = spam.index(west, north)
        last_row, last_col = spam.index(east, south)
        row0 = max(0, (min(first_row, last_row) // factor) * factor)
        col0 = max(0, (min(first_col, last_col) // factor) * factor)
        row1 = min(spam.height, math.ceil((max(first_row, last_row) + 1) / factor) * factor)
        col1 = min(spam.width, math.ceil((max(first_col, last_col) + 1) / factor) * factor)
        out_height, out_width = math.ceil((row1 - row0) / factor), math.ceil((col1 - col0) / factor)
        harvest = np.zeros((out_height, out_width), dtype="float32")
        schema = {"geometry": "Point", "properties": {"CellID": "int", "WTH_CELLID": "int", "WTH_BORROWED": "int", "WTH_DIST_KM": "float", "HAREA": "float", "COUNTRY": "str:80", "GRID_ROW": "int", "GRID_COL": "int"}}
        args.sites.parent.mkdir(parents=True, exist_ok=True)
        args.harvest_area.parent.mkdir(parents=True, exist_ok=True)
        written, no_weather, unresolved = 0, 0, 0
        borrowed: list[dict[str, float | int]] = []
        observed_ids: set[int] = set()
        with fiona.open(args.sites, "w", driver="GPKG", crs="EPSG:4326", schema=schema, layer="sites") as sink:
            for out_row in range(out_height):
                source_row = row0 + out_row * factor
                height = min(factor, spam.height - source_row)
                for out_col in range(out_width):
                    source_col = col0 + out_col * factor
                    width = min(factor, spam.width - source_col)
                    values = spam.read(1, window=Window(source_col, source_row, width, height), masked=True)
                    candidates: list[tuple[float, float, float]] = []
                    for r, c in np.ndindex(values.shape):
                        value = values[r, c]
                        if np.ma.is_masked(value) or not np.isfinite(value) or value <= 0:
                            continue
                        lon, lat = rasterio.transform.xy(spam.transform, source_row + r, source_col + c, offset="center")
                        if inside_region(float(lon), float(lat), region):
                            candidates.append((float(value), float(lat), float(lon)))
                    if not candidates:
                        continue
                    total_area = sum(value for value, _, _ in candidates)
                    block = (source_row // factor, source_col // factor)
                    weather_candidates = weather_by_block.get(block, [])
                    if not weather_candidates:
                        no_weather += 1
                        if args.weather_fallback == "none":
                            unresolved += 1
                            continue
                    # A weather grid normally has one cell per block.  Choose
                    # the nearest one to the block centre defensively if a
                    # future dataset contains more than one.
                    centre_lon, centre_lat = rasterio.transform.xy(
                        spam.transform,
                        source_row + (height - 1) / 2,
                        source_col + (width - 1) / 2,
                        offset="center",
                    )
                    if weather_candidates:
                        cell_id, lat, lon = min(
                            weather_candidates,
                            key=lambda item: (item[1] - centre_lat) ** 2 + (item[2] - centre_lon) ** 2,
                        )
                        weather_id, borrowed_flag, distance_km = cell_id, 0, 0.0
                    else:
                        # Coastal block centres can lie offshore.  Keep the
                        # target within actual soybean land for soil/calendar
                        # sampling, but retain the block's own global CellID.
                        _, lat, lon = max(candidates, key=lambda item: item[0])
                        cell_id = global_cell_id(float(centre_lat), float(centre_lon), args.resolution)
                        if cell_id in indexed_ids:
                            raise ValueError(f"Missing-weather CellID {cell_id} already exists in the WTH index")
                        weather_id, _, _, distance_km = nearest_weather(lat, lon, indexed_weather)
                        borrowed_flag = 1
                        borrowed.append({
                            "CellID": cell_id, "WTH_CELLID": weather_id,
                            "latitude": lat, "longitude": lon,
                            "weather_distance_km": distance_km,
                            "soy_harea_ha": total_area,
                            "grid_row": out_row, "grid_col": out_col,
                        })
                    if cell_id in observed_ids:
                        raise ValueError(f"Duplicate site CellID {cell_id}")
                    observed_ids.add(cell_id)
                    harvest[out_row, out_col] = total_area
                    sink.write({"geometry": {"type": "Point", "coordinates": (lon, lat)}, "properties": {"CellID": cell_id, "WTH_CELLID": weather_id, "WTH_BORROWED": borrowed_flag, "WTH_DIST_KM": distance_km, "HAREA": total_area, "COUNTRY": ";".join(args.country), "GRID_ROW": out_row, "GRID_COL": out_col}})
                    written += 1
        left, top = rasterio.transform.xy(spam.transform, row0, col0, offset="ul")
        profile = spam.profile.copy()
        profile.update(driver="GTiff", count=1, dtype="float32", nodata=0.0, width=out_width, height=out_height, transform=from_origin(left, top, args.resolution, args.resolution), compress="deflate")
        with rasterio.open(args.harvest_area, "w", **profile) as destination:
            destination.write(harvest, 1)
    print(f"Created {written} Pythia sites; {no_weather} missing-WTH cells, {len(borrowed)} assigned nearest WTH, {unresolved} unresolved.")
    if args.weather_assignment_audit is not None:
        borrowed_area = sum(item["soy_harea_ha"] for item in borrowed)
        report = {
            "status": "ok" if unresolved == 0 else "incomplete",
            "country": ";".join(args.country),
            "weather_fallback": args.weather_fallback,
            "site_count": written,
            "missing_weather_cell_count": no_weather,
            "borrowed_weather_cell_count": len(borrowed),
            "unresolved_weather_cell_count": unresolved,
            "borrowed_soy_harea_ha": borrowed_area,
            "borrowed_cells": borrowed,
            "weather_index": str(args.weather_index.resolve()),
            "note": "Borrowed WTH is selected by great-circle distance from a soybean pixel within each missing weather grid cell; the simulated site retains its own CellID and location.",
        }
        args.weather_assignment_audit.parent.mkdir(parents=True, exist_ok=True)
        args.weather_assignment_audit.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.max_no_weather is not None and unresolved > args.max_no_weather:
        raise SystemExit(
            f"Country sites are incomplete: {unresolved} soybean grid cells lack a usable WTH assignment "
            f"(allowed={args.max_no_weather}). Inspect outputs but do not use them for production."
        )


if __name__ == "__main__":
    main()
