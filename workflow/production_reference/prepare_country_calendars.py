#!/usr/bin/env python3
"""Create country calendar snapshots, with explicit same-year spatial fallback.

Original NetCDFs, site coordinates, weather assignments, and the model are never
modified. Historical calibration borrows a coherent three-field season; future
production needs planting only. No values from other years/scenarios are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import fiona
import numpy as np
import xarray as xr

from prepare_ggcmi_calendar import years_from_time

FIELDS = ("planting_day", "maturity_day", "growing_period")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def valid_value(name: str, values: np.ndarray) -> np.ndarray:
    return np.isfinite(values) & ((values > 0) if name == "growing_period" else ((values >= 1) & (values <= 366)))


def nearest_valid(valid: np.ndarray, lats: np.ndarray, lons: np.ndarray,
                  latitude: float, longitude: float) -> tuple[int, int, float]:
    """Nearest original valid grid cell by great-circle distance, deterministic ties."""
    rows, cols = np.where(valid)
    if not len(rows):
        raise ValueError("Calendar has no valid donor in the requested year/scenario")
    lat0, lon0 = np.radians([latitude, longitude])
    lat1, lon1 = np.radians(lats[rows]), np.radians(lons[cols])
    a = np.sin((lat1 - lat0) / 2) ** 2 + np.cos(lat0) * np.cos(lat1) * np.sin((lon1 - lon0) / 2) ** 2
    distance = 6371.0088 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    index = int(np.argmin(distance))
    return int(rows[index]), int(cols[index]), float(distance[index])


def serial_values(arrays: dict, time: int, row: int, col: int) -> dict:
    return {name: float(values[time, row, col]) if np.isfinite(values[time, row, col]) else None
            for name, values in arrays.items()}


def resolve_calendar(source: Path, output: Path, sites: list[dict], years: list[int],
                     historical: bool, max_distance_km: float | None = None) -> dict:
    if output.exists():
        raise FileExistsError(output)
    required = FIELDS if historical else ("planting_day",)
    replacements = []
    with xr.open_dataset(source, decode_times=False) as ds:
        if not set(FIELDS) <= set(ds.data_vars):
            raise ValueError(f"Missing GGCMI variables: {source}")
        available = years_from_time(ds.time.values, str(ds.time.attrs.get("units", "")))
        if len(set(available)) != len(available) or set(years) - set(available):
            raise ValueError(f"Missing/duplicate GGCMI years in {source}")
        # Match xarray.sel(method='nearest'), including midpoint tie handling.
        rows = ds.get_index("lat").get_indexer([s["latitude"] for s in sites], method="nearest")
        cols = ds.get_index("lon").get_indexer([s["longitude"] for s in sites], method="nearest")
        if np.any(rows < 0) or np.any(cols < 0):
            raise ValueError("Could not locate all sites in GGCMI coordinates")
        lat0, latn = int(rows.min()), int(rows.max()) + 1
        lon0, lonn = int(cols.min()), int(cols.max()) + 1
        # Load one source at a time; donor candidates always come from ORIGINAL
        # global values. Copy a compact country rectangle, not an entire globe.
        arrays = {name: ds[name].transpose("time", "lat", "lon").values for name in FIELDS}
        resolved = xr.Dataset({
            name: xr.DataArray(values[:, lat0:latn, lon0:lonn].copy(), dims=("time", "lat", "lon"),
                               attrs=dict(ds[name].attrs)) for name, values in arrays.items()
        }, coords={"time": ds.time.copy(deep=True), "lat": ds.lat.isel(lat=slice(lat0, latn)).copy(deep=True),
                   "lon": ds.lon.isel(lon=slice(lon0, lonn)).copy(deep=True)}, attrs=dict(ds.attrs))
        resolved.attrs.update({"sbrust_original_source": str(source.resolve()),
                               "sbrust_fallback": "nearest valid original cell, same year and scenario",
                               "sbrust_required_fields": ",".join(required)})
        for year in years:
            t = int(np.flatnonzero(available == year)[0])
            valid = np.logical_and.reduce([valid_value(name, arrays[name][t]) for name in required])
            for site, row, col in zip(sites, rows, cols):
                if valid[row, col]:
                    continue
                dr, dc, distance = nearest_valid(valid, ds.lat.values, ds.lon.values,
                                                  site["latitude"], site["longitude"])
                if max_distance_km is not None and distance > max_distance_km:
                    raise ValueError(f"Calendar donor for {site['cell_id']} y{year}: {distance:.2f} km > {max_distance_km}")
                before = serial_values(arrays, t, row, col)
                after = serial_values(arrays, t, dr, dc)
                # Whole historical season: do not mix a recipient's planting
                # with a donor's maturity/duration. All GM candidates use it.
                for name in FIELDS:
                    resolved[name].values[t, row - lat0, col - lon0] = arrays[name][t, dr, dc]
                replacements.append({**site, "year": int(year), "sampled_latitude": float(ds.lat.values[row]),
                                     "sampled_longitude": float(ds.lon.values[col]),
                                     "donor_latitude": float(ds.lat.values[dr]), "donor_longitude": float(ds.lon.values[dc]),
                                     "distance_km": distance, "before": before, "after": after})
        # Every required target value is validated before publishing a snapshot.
        for year in years:
            t = int(np.flatnonzero(available == year)[0])
            for name in required:
                values = resolved[name].values[t, rows - lat0, cols - lon0]
                if not valid_value(name, values).all():
                    raise ValueError(f"Unresolved {name} in {source}, year {year}")
        output.parent.mkdir(parents=True, exist_ok=True)
        resolved.to_netcdf(output, engine="netcdf4", encoding={name: {"zlib": True, "complevel": 4} for name in FIELDS})
        resolved.close()
    return {"source": str(source.resolve()), "source_sha256": sha256(source),
            "snapshot": str(output.resolve()), "snapshot_sha256": sha256(output),
            "required_fields": list(required), "replacement_point_years": len(replacements),
            "replacement_points": len({r["cell_id"] for r in replacements}),
            "maximum_distance_km": max((r["distance_km"] for r in replacements), default=0),
            "replacements": replacements}


def prepare(project_path: Path, sites_path: Path, output_dir: Path, max_distance_km: float | None = None) -> dict:
    if output_dir.exists():
        raise FileExistsError(f"Refusing to overwrite calendar campaign: {output_dir}")
    project = json.loads(project_path.read_text(encoding="utf-8"))
    calendar = project["experiment"]["calendar"]
    if calendar.get("mode") != "annual_scenario_reference":
        raise ValueError("This preparation requires annual_scenario_reference calendars")
    with fiona.open(sites_path) as source:
        sites = [{"cell_id": str(f["properties"]["CellID"]),
                  "latitude": float(f["geometry"]["coordinates"][1]),
                  "longitude": float(f["geometry"]["coordinates"][0])} for f in source]
    if not sites or len({s["cell_id"] for s in sites}) != len(sites):
        raise ValueError("Empty/duplicate sites")
    original_sources = dict(calendar["sources"])
    if calendar["historical_baseline"]["source"] != original_sources["historical"]:
        raise ValueError("Historical baseline and calibration must reference the same original calendar")
    output_dir.mkdir(parents=True)
    report = {"status": "preparing", "country": project["experiment"]["country"], "site_count": len(sites),
              "project_source": str(project_path.resolve()), "project_source_sha256": sha256(project_path),
              "sites": str(sites_path.resolve()), "sites_sha256": sha256(sites_path), "calendars": {},
              "policy": "Same-year same-scenario nearest valid original GGCMI cell; historical whole-season replacement; future planting required only",
              "maximum_allowed_distance_km": max_distance_km}
    for scenario, source in original_sources.items():
        if not isinstance(source, str):
            raise ValueError("Scenario-reference sources must be file paths")
        if scenario == "historical":
            years = list(range(1991, 2015))
        else:
            years = sorted({year for period in project["experiment"]["periods"] if scenario in period["scenarios"]
                            for year in range(period["start_year"], period["end_year"] + 1)})
        if not years:
            raise ValueError(f"No configured years for {scenario}")
        snapshot = output_dir / f"calendar_{scenario}.nc"
        item = resolve_calendar(Path(source), snapshot, sites, years, scenario == "historical", max_distance_km)
        report["calendars"][scenario] = item
        calendar["sources"][scenario] = str(snapshot.resolve())
        print(f"{scenario}: replacement point-years={item['replacement_point_years']}; points={item['replacement_points']}; max km={item['maximum_distance_km']:.3f}", flush=True)
    calendar["historical_baseline"]["source"] = calendar["sources"]["historical"]
    audit_path = output_dir / "calendar_assignment.json"
    calendar["spatial_fallback_audit"] = str(audit_path.resolve())
    report["status"] = "ok"
    audit_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    # Project is the completion marker, written only after all snapshots/audit.
    target_project = output_dir / "project.json"
    target_project.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    parser.add_argument("sites", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--calendar-fallback", choices=["nearest"], required=True,
                        help="Explicit authorization for spatial borrowing of calendars")
    parser.add_argument("--max-distance-km", type=float)
    args = parser.parse_args()
    if args.max_distance_km is not None and args.max_distance_km <= 0:
        parser.error("--max-distance-km must be positive")
    prepare(args.project, args.sites, args.output_dir, args.max_distance_km)


if __name__ == "__main__":
    main()
