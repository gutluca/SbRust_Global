#!/usr/bin/env python3
"""Validate internal paired disease/control long summaries."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from datetime import date, datetime, timezone
from itertools import zip_longest
from pathlib import Path

from export_analysis_table import output_fields, sha256, sort_key


PLANTINGS = ("P01", "P02", "P03", "P04", "P05")
PAIRED_FIELDS = (
    "SIMULATION_ID",
    "COUNTRY",
    "CELL_ID",
    "WEATHER_SOURCE_CELL_ID",
    "WEATHER_BORROWED",
    "WEATHER_DISTANCE_KM",
    "LATITUDE",
    "LONGITUDE",
    "HARVEST_AREA_HA",
    "MATURITY_GROUP",
    "PERIOD",
    "SIM_YEAR",
    "SCENARIO",
    "GCM",
    "PLANTING_LABEL",
    "PLANT_DATE",
)


def reader(path: Path):
    stream = (
        gzip.open(path, "rt", newline="", encoding="utf-8")
        if path.suffix == ".gz"
        else path.open("r", newline="", encoding="utf-8")
    )
    rows = csv.DictReader(stream, delimiter=";")
    return stream, rows


def numeric(value: str, field: str, row_number: int, *, nonnegative: bool = True) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {field} at row {row_number}: {value!r}") from exc
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError(f"Invalid {field} at row {row_number}: {value!r}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("disease_csv", type=Path)
    parser.add_argument("control_csv", type=Path)
    parser.add_argument("audit_json", type=Path)
    args = parser.parse_args()

    for path in (args.disease_csv, args.control_csv):
        if not path.is_file():
            raise SystemExit(f"Final summary not found: {path}")
    if args.audit_json.exists():
        raise SystemExit(f"Refusing to overwrite final validation audit: {args.audit_json}")

    disease_stream, disease_rows = reader(args.disease_csv)
    control_stream, control_rows = reader(args.control_csv)
    row_count = point_count = 0
    disease_lower_count = disease_equal_count = disease_higher_count = 0
    positive_sprays_count = 0
    onset_present_count = onset_missing_count = 0
    previous_key = None
    current_point = None
    current_group = None
    group_plantings: list[str] = []
    seen_points: set[str] = set()

    try:
        if disease_rows.fieldnames != output_fields("disease"):
            raise ValueError(f"Unexpected disease header: {disease_rows.fieldnames}")
        if control_rows.fieldnames != output_fields("control"):
            raise ValueError(f"Unexpected control header: {control_rows.fieldnames}")

        for row_count, pair in enumerate(
            zip_longest(disease_rows, control_rows), start=1
        ):
            disease, control = pair
            if disease is None or control is None:
                raise ValueError("Disease and control summaries have different row counts")
            for field in PAIRED_FIELDS:
                if disease[field] != control[field]:
                    raise ValueError(
                        f"Disease/control mismatch at row {row_count}, field {field}: "
                        f"{disease[field]!r} != {control[field]!r}"
                    )

            key = sort_key(disease)
            if previous_key is not None and key <= previous_key:
                raise ValueError(f"Duplicate or out-of-order row at {row_count}: {key}")
            previous_key = key

            point = disease["CELL_ID"]
            if point != current_point:
                if point in seen_points:
                    raise ValueError(f"Non-contiguous CELL_ID block at row {row_count}: {point}")
                seen_points.add(point)
                current_point = point
                point_count += 1

            group = (
                disease["CELL_ID"], disease["PERIOD"], disease["SIM_YEAR"],
                disease["SCENARIO"], disease["GCM"],
            )
            if current_group is None:
                current_group = group
            elif group != current_group:
                if tuple(group_plantings) != PLANTINGS:
                    raise ValueError(
                        f"Group {current_group} has planting labels {group_plantings}; expected {PLANTINGS}"
                    )
                current_group = group
                group_plantings = []
            group_plantings.append(disease["PLANTING_LABEL"])

            date.fromisoformat(disease["PLANT_DATE"])
            if disease["MATURITY_DATE"]:
                date.fromisoformat(disease["MATURITY_DATE"])
            if control["MATURITY_DATE"]:
                date.fromisoformat(control["MATURITY_DATE"])
            if numeric(disease["HARVEST_AREA_HA"], "HARVEST_AREA_HA", row_count) <= 0:
                raise ValueError(f"HARVEST_AREA_HA must be positive at row {row_count}")
            disease_yield = numeric(
                disease["YIELD_KG_HA"], "disease YIELD_KG_HA", row_count,
                nonnegative=False,
            )
            control_yield = numeric(
                control["YIELD_KG_HA"], "control YIELD_KG_HA", row_count,
                nonnegative=False,
            )
            if math.isclose(disease_yield, control_yield, rel_tol=0.0, abs_tol=1e-9):
                disease_equal_count += 1
            elif disease_yield < control_yield:
                disease_lower_count += 1
            else:
                disease_higher_count += 1
            sprays = numeric(disease["TOTAL_SPRAYS"], "TOTAL_SPRAYS", row_count)
            if not sprays.is_integer():
                raise ValueError(f"TOTAL_SPRAYS is not an integer at row {row_count}: {sprays}")
            if sprays > 0:
                positive_sprays_count += 1
            onset = disease["DISEASE_ONSET_DAE"].strip()
            if onset:
                onset_value = numeric(onset, "DISEASE_ONSET_DAE", row_count)
                if not onset_value.is_integer():
                    raise ValueError(
                        f"DISEASE_ONSET_DAE is not an integer at row {row_count}: {onset_value}"
                    )
                onset_present_count += 1
            else:
                onset_missing_count += 1

        if row_count == 0:
            raise ValueError("Final summaries have no data rows")
        if tuple(group_plantings) != PLANTINGS:
            raise ValueError(
                f"Group {current_group} has planting labels {group_plantings}; expected {PLANTINGS}"
            )
        if disease_lower_count == 0:
            raise ValueError(
                "No simulation has disease yield lower than its paired control; "
                "refusing to approve a campaign with no demonstrated disease effect"
            )
    finally:
        disease_stream.close()
        control_stream.close()

    args.audit_json.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "status": "ok",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "disease_csv": str(args.disease_csv.resolve()),
        "disease_csv_sha256": sha256(args.disease_csv),
        "control_csv": str(args.control_csv.resolve()),
        "control_csv_sha256": sha256(args.control_csv),
        "simulation_rows_each": row_count,
        "points": point_count,
        "planting_labels_per_group": list(PLANTINGS),
        "paired_by": "SIMULATION_ID",
        "point_rows_contiguous": True,
        "disease_yield_lower_than_control": disease_lower_count,
        "disease_yield_equal_to_control": disease_equal_count,
        "disease_yield_higher_than_control": disease_higher_count,
        "positive_total_sprays": positive_sprays_count,
        "disease_onset_dae_present": onset_present_count,
        "disease_onset_dae_missing": onset_missing_count,
        "paired_directory_file_count": len([
            path for path in args.disease_csv.parent.iterdir() if path.is_file()
        ]),
    }
    if report["paired_directory_file_count"] != 2:
        raise ValueError(
            "Internal paired directory must contain exactly two files; "
            f"found {report['paired_directory_file_count']}"
        )
    args.audit_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"Final summaries validated: rows_each={row_count}; points={point_count}; "
        f"paired=yes; point_blocks=contiguous; disease_lower={disease_lower_count}; "
        f"disease_equal={disease_equal_count}; disease_higher={disease_higher_count}; "
        f"positive_sprays={positive_sprays_count}; onset_present={onset_present_count}; "
        f"onset_missing={onset_missing_count}; files_in_paired_directory=2"
    )


if __name__ == "__main__":
    main()
