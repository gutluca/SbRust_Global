#!/usr/bin/env python3
"""Join paired long summaries into one country-level simulation table.

The disease and no-disease long files remain internal, independently
validated intermediates. The only public product is summary_master_<country>.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

from export_analysis_table import opener, output_fields, sha256, sort_key
from validate_final_summaries import PAIRED_FIELDS


MASTER_FIELDS = (
    "SIMULATION_ID", "COUNTRY", "CELL_ID", "WEATHER_SOURCE_CELL_ID", "WEATHER_BORROWED", "WEATHER_DISTANCE_KM", "LATITUDE", "LONGITUDE",
    "HARVEST_AREA_HA", "MATURITY_GROUP", "PERIOD", "SIM_YEAR",
    "SCENARIO", "GCM", "PLANTING_LABEL", "PLANT_DATE", "MATURITY_DATE",
    "YIELD_WITH_DISEASE_KG_HA", "YIELD_NO_DISEASE_KG_HA",
    "LOSS_KG_HA", "LOSS_PCT", "TOTAL_SPRAYS", "DISEASE_ONSET_DAE",
)


def period_labels(project_config: Path) -> dict[str, tuple[str, int, int]]:
    project = json.loads(project_config.read_text(encoding="utf-8"))
    periods = project["experiment"]["periods"]
    labels: dict[str, tuple[str, int, int]] = {}
    for period in periods:
        name = period["name"]
        start, end = int(period["start_year"]), int(period["end_year"])
        if name in labels or start > end:
            raise ValueError(f"Invalid period definition: {period}")
        labels[name] = (f"{start}-{end}", start, end)
    if not labels:
        raise ValueError("Project configuration contains no HPG periods")
    return labels


def yield_value(value: str, label: str, row_number: int) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid {label} at row {row_number}: {value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"Invalid {label} at row {row_number}: {value!r}")
    return number


def format_number(value: float) -> str:
    return format(value, ".12g")


def build_master(
    disease_csv: Path,
    control_csv: Path,
    output_csv: Path,
    project_config: Path,
) -> dict[str, object]:
    """Stream a checked, one-to-one join in the prevalidated point order."""
    periods = period_labels(project_config)
    project_country = json.loads(project_config.read_text(encoding="utf-8"))["experiment"]["country"]
    counts: Counter[str] = Counter()
    point_counts: Counter[str] = Counter()
    previous_key = None
    previous_point = None
    completed_points: set[str] = set()
    row_count = 0
    max_loss_pct: float | None = None
    with opener(disease_csv, "rt") as disease_stream, opener(control_csv, "rt") as control_stream, opener(output_csv, "wt") as destination:
        disease_rows = csv.DictReader(disease_stream, delimiter=";")
        control_rows = csv.DictReader(control_stream, delimiter=";")
        if disease_rows.fieldnames != output_fields("disease"):
            raise ValueError(f"Unexpected disease header: {disease_rows.fieldnames}")
        if control_rows.fieldnames != output_fields("control"):
            raise ValueError(f"Unexpected control header: {control_rows.fieldnames}")
        writer = csv.DictWriter(destination, fieldnames=MASTER_FIELDS, delimiter=";")
        writer.writeheader()
        for row_count, (disease, control) in enumerate(
            zip_longest(disease_rows, control_rows), start=1
        ):
            if disease is None or control is None:
                raise ValueError("Disease/control row counts differ")
            for field in PAIRED_FIELDS:
                if disease[field] != control[field]:
                    raise ValueError(f"Unpaired row {row_count}: {field}")
            if disease["COUNTRY"] != project_country:
                raise ValueError(
                    f"Country {disease['COUNTRY']!r} does not match project configuration "
                    f"{project_country!r} at row {row_count}"
                )
            key = sort_key(disease)
            if previous_key is not None and key <= previous_key:
                raise ValueError(f"Duplicate or unsorted simulation at row {row_count}")
            previous_key = key
            point = disease["CELL_ID"]
            if point != previous_point:
                if point in completed_points:
                    raise ValueError(f"Non-contiguous point {point} at row {row_count}")
                completed_points.add(point)
                previous_point = point
            point_counts[point] += 1
            old_period = disease["PERIOD"]
            if old_period not in periods:
                raise ValueError(f"Unknown period {old_period!r} at row {row_count}")
            label, start, end = periods[old_period]
            try:
                year = int(disease["SIM_YEAR"])
            except ValueError as exc:
                raise ValueError(f"Invalid simulation year at row {row_count}") from exc
            if not start <= year <= end:
                raise ValueError(f"Year {year} outside period {label} at row {row_count}")
            counts[label] += 1
            disease_yield = yield_value(disease["YIELD_KG_HA"], "disease yield", row_count)
            control_yield = yield_value(control["YIELD_KG_HA"], "no-disease yield", row_count)
            loss = control_yield - disease_yield
            if loss > 0:
                counts["disease_lower"] += 1
            elif loss < 0:
                counts["disease_higher"] += 1
            else:
                counts["equal"] += 1
            if control_yield == 0:
                counts["zero_control_yield"] += 1
            else:
                loss_pct = 100 * loss / control_yield
                max_loss_pct = loss_pct if max_loss_pct is None else max(max_loss_pct, loss_pct)
                if loss_pct >= 50:
                    counts["loss_at_least_50_pct"] += 1
            record = {field: disease[field] for field in MASTER_FIELDS if field in disease}
            record["PERIOD"] = label
            record["SIMULATION_ID"] = "|".join((
                disease["COUNTRY"], point, label, disease["SIM_YEAR"],
                disease["SCENARIO"], disease["GCM"], disease["PLANTING_LABEL"],
            ))
            record["YIELD_WITH_DISEASE_KG_HA"] = disease["YIELD_KG_HA"]
            record["YIELD_NO_DISEASE_KG_HA"] = control["YIELD_KG_HA"]
            record["LOSS_KG_HA"] = format_number(loss)
            record["LOSS_PCT"] = (
                format_number(100 * loss / control_yield) if control_yield > 0 else ""
            )
            writer.writerow(record)
    if row_count == 0:
        raise ValueError("Paired summaries have no simulations")
    return {
        "simulation_rows": row_count,
        "points": len(point_counts),
        "rows_by_period": {key: counts[key] for key, _, _ in periods.values()},
        "disease_yield_lower_than_control": counts["disease_lower"],
        "disease_yield_higher_than_control": counts["disease_higher"],
        "equal_yield": counts["equal"],
        "zero_control_yield": counts["zero_control_yield"],
        "maximum_loss_pct": max_loss_pct,
        "loss_at_least_50_pct": counts["loss_at_least_50_pct"],
        "negative_losses_are_preserved": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("disease_csv", type=Path)
    parser.add_argument("control_csv", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument("project_config", type=Path)
    parser.add_argument("audit_json", type=Path)
    args = parser.parse_args()
    for path in (args.disease_csv, args.control_csv, args.project_config):
        if not path.is_file():
            raise SystemExit(f"Required input not found: {path}")
    for path in (args.output_csv, args.audit_json):
        if path.exists():
            raise SystemExit(f"Refusing to overwrite: {path}")
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    args.audit_json.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{args.output_csv.stem}.", suffix=".partial.csv.gz",
        dir=args.output_csv.parent,
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        stats = build_master(
            args.disease_csv, args.control_csv, temp_path, args.project_config
        )
        temp_path.replace(args.output_csv)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    audit = {
        "schema_version": 1,
        "status": "ok",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_disease_csv": str(args.disease_csv.resolve()),
        "source_disease_sha256": sha256(args.disease_csv),
        "source_control_csv": str(args.control_csv.resolve()),
        "source_control_sha256": sha256(args.control_csv),
        "project_config": str(args.project_config.resolve()),
        "project_config_sha256": sha256(args.project_config),
        "output_csv": str(args.output_csv.resolve()),
        "output_sha256": sha256(args.output_csv),
        "fields": list(MASTER_FIELDS),
        "loss_definition": "no-disease yield minus disease yield; percentage relative to no-disease yield; blank percentage if denominator is zero",
        **stats,
    }
    args.audit_json.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(f"Master summary: rows={stats['simulation_rows']}; points={stats['points']}; output={args.output_csv}")


if __name__ == "__main__":
    main()
