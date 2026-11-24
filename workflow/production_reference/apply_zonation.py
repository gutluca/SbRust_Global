#!/usr/bin/env python3
"""Attach maturity-zone cultivar attributes to a Pythia point GeoPackage.

Rules are deliberately external to the code: each country supplies a JSON file.
Current selectors support latitude bands and arbitrary GeoJSON Polygon/MultiPolygon
geometries, allowing a country to replace latitude rules with official zoning later.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import fiona


def inside_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    prev_lon, prev_lat = ring[-1]
    for current_lon, current_lat in ring:
        crosses = (current_lat > lat) != (prev_lat > lat)
        if crosses and lon < (prev_lon - current_lon) * (lat - current_lat) / (prev_lat - current_lat) + current_lon:
            inside = not inside
        prev_lon, prev_lat = current_lon, current_lat
    return inside


def contains(geometry: dict, lon: float, lat: float) -> bool:
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    if geometry["type"] not in {"Polygon", "MultiPolygon"}:
        raise ValueError("Zone geometry must be Polygon or MultiPolygon")
    return any(inside_ring(lon, lat, polygon[0]) and not any(inside_ring(lon, lat, hole) for hole in polygon[1:]) for polygon in polygons)


def matches(rule: dict, lon: float, lat: float, properties: dict) -> bool:
    selector = rule.get("selector", rule)
    selector_type = selector.get("type", "latitude_band")
    if selector_type == "latitude_band":
        minimum, maximum = float(selector["min_lat"]), float(selector["max_lat"])
        return minimum <= lat <= maximum
    if selector_type == "geometry":
        return contains(selector["geometry"], lon, lat)
    if selector_type == "growing_period_band":
        days = float(properties.get("GROW_DAYS", "nan"))
        return float(selector["min_days"]) <= days <= float(selector["max_days"])
    if selector_type == "all":
        return all(matches({"selector": item}, lon, lat, properties) for item in selector["selectors"])
    raise ValueError(f"Unsupported selector type: {selector_type}")


def validate_rules(rules: list[dict]) -> None:
    if not rules:
        raise ValueError("Zonation rules are empty")
    for rule in rules:
        cultivar = rule.get("cultivar", {})
        if not rule.get("zone_id") or not cultivar.get("ingeno") or not cultivar.get("cname"):
            raise ValueError("Every zone needs zone_id, cultivar.ingeno and cultivar.cname")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sites", type=Path, help="Point GeoPackage from build_sites.py")
    parser.add_argument("rules", type=Path, help="Country zonation JSON")
    parser.add_argument("output", type=Path, help="Zone-enriched Point GeoPackage")
    args = parser.parse_args()
    config = json.loads(args.rules.read_text(encoding="utf-8"))
    rules = config.get("rules", [])
    validate_rules(rules)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    matched, overlaps = 0, 0
    with fiona.open(args.sites) as source:
        schema = source.schema.copy()
        schema["properties"] = dict(schema["properties"])
        schema["properties"].update({"ZONE_ID": "str:48", "INGENO": "str:32", "CNAME": "str:64"})
        with fiona.open(args.output, "w", driver="GPKG", schema=schema, crs=source.crs, layer="sites") as target:
            for feature in source:
                lon, lat = feature["geometry"]["coordinates"]
                candidates = [rule for rule in rules if matches(rule, lon, lat, dict(feature["properties"]))]
                if not candidates:
                    raise ValueError(f"No maturity-zone rule covers point ({lat:.4f}, {lon:.4f})")
                if len(candidates) > 1:
                    overlaps += 1
                    raise ValueError(f"More than one maturity-zone rule covers point ({lat:.4f}, {lon:.4f})")
                rule, cultivar = candidates[0], candidates[0]["cultivar"]
                properties = dict(feature["properties"])
                properties.update({"ZONE_ID": str(rule["zone_id"]), "INGENO": str(cultivar["ingeno"]), "CNAME": str(cultivar["cname"])})
                target.write({"type": "Feature", "geometry": feature["geometry"], "properties": properties})
                matched += 1
    print(f"Zoned {matched} sites -> {args.output}; overlaps={overlaps}")


if __name__ == "__main__":
    main()
