"""Area-weighted coverage and immutable point subsets shared by country jobs."""
from __future__ import annotations

import json
import math
from pathlib import Path

import fiona


def read_sites(path: Path):
    with fiona.open(path) as source:
        return list(source), source.schema.copy(), source.crs


def identity(feature):
    lon, lat = feature["geometry"]["coordinates"]
    return str(feature["properties"]["CellID"]), (round(lat, 4), round(lon, 4))


def subset_sites(source: Path, target: Path, keep: set[str]) -> int:
    if target.exists():
        raise FileExistsError(target)
    features, schema, crs = read_sites(source)
    selected = [f for f in features if identity(f)[0] in keep]
    target.parent.mkdir(parents=True, exist_ok=True)
    with fiona.open(target, "w", driver="GPKG", schema=schema, crs=crs, layer="sites") as sink:
        sink.writerecords(selected)
    return len(selected)


def coverage_report(reference: Path, keep: set[str], minimum: float, reasons: dict | None = None) -> dict:
    if not 0 < minimum <= 1:
        raise ValueError("Minimum area coverage must be in (0, 1]")
    features, _, _ = read_sites(reference)
    areas = {}
    for feature in features:
        cell, coord = identity(feature)
        area = float(feature["properties"]["HAREA"])
        if cell in areas or not math.isfinite(area) or area <= 0:
            raise ValueError(f"Invalid/duplicate reference HAREA for {cell}")
        areas[cell] = area
    if not areas or not keep <= set(areas):
        raise ValueError("Empty reference or unknown retained CellID")
    total = math.fsum(areas.values())
    retained = math.fsum(areas[c] for c in keep)
    fraction = retained / total
    return {"status": "ok" if keep and fraction + 1e-12 >= minimum else "insufficient_coverage",
            "reference_sites": str(reference.resolve()), "minimum_area_fraction": minimum,
            "original_points": len(areas), "retained_points": len(keep), "excluded_points": len(areas) - len(keep),
            "original_soy_area_ha": total, "retained_soy_area_ha": retained,
            "excluded_soy_area_ha": total - retained, "retained_area_pct": 100 * fraction,
            "retained_points_pct": 100 * len(keep) / len(areas),
            "excluded": [{"cell_id": c, "soy_area_ha": areas[c],
                          "reason": (reasons or {}).get(c, "not_complete_in_all_required_cases")}
                         for c in sorted(set(areas) - keep, key=int)],
            "note": "Excluded area is not zero yield and is not extrapolated to national totals."}


def write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
