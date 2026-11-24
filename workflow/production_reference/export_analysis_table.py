#!/usr/bin/env python3
"""Export paired, long-format intermediate country summaries.

The annual task shards are intentionally wide because they retain all five
planting dates and the fields required to audit each DSSAT/DISMO execution.
Those shards and these paired long summaries are pipeline internals. The
public result is built by build_master_summary.py:

* ``summary_disease_<country>.csv.gz`` contains one calibrated-disease
  simulation per row and includes the final DISMO ``NSPRAYS`` value and the
  first day after emergence with printed severity greater than zero;
* ``summary_control_<country>.csv.gz`` contains the matching no-disease
  simulation per row.

One row is one pixel x period x planting year x scenario x weather GCM x
planting date.  Rows are externally sorted by pixel first so every simulation
for one point is contiguous even in a global campaign.  Severity, P01--P05
wide columns, planting-date means and MME statistics are deliberately absent.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import gzip
import hashlib
import heapq
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterator


SOURCE_IDENTITY = (
    "COUNTRY",
    "PERIOD",
    "SIM_YEAR",
    "SCENARIO",
    "GCM",
    "CELL_ID",
    "WEATHER_SOURCE_CELL_ID",
    "WEATHER_BORROWED",
    "WEATHER_DISTANCE_KM",
    "LATITUDE",
    "LONGITUDE",
    "HARVEST_AREA",
    "GM_WIN",
)

COMMON_OUTPUT_FIELDS = (
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
    "MATURITY_DATE",
    "YIELD_KG_HA",
)

PERIOD_ORDER = {"baseline": 0, "future_near": 1, "future_mid": 2}
SCENARIO_ORDER = {"historical": 0, "ssp126": 1, "ssp370": 2, "ssp585": 3}


def opener(path: Path, mode: str):
    if path.suffix == ".gz":
        return gzip.open(path, mode, newline="", encoding="utf-8")
    return path.open(mode, newline="", encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def audit_path(output: Path) -> Path:
    name = output.name
    for suffix in reversed(output.suffixes):
        name = name[: -len(suffix)]
    return output.with_name(f"{name}_audit.json")


def dssat_date(value: str) -> str:
    """Convert a DSSAT YYYYDDD or YYDDD date into an ISO date."""
    try:
        token = str(value).strip()
        if len(token) == 7 and token.isdigit():
            year, doy = int(token[:4]), int(token[4:])
        elif len(token) == 5 and token.isdigit():
            year = 1900 + int(token[:2]) if int(token[:2]) >= 80 else 2000 + int(token[:2])
            doy = int(token[2:])
        else:
            raise ValueError
        if not 1 <= doy <= (366 if calendar.isleap(year) else 365):
            raise ValueError
        return date.fromordinal(date(year, 1, 1).toordinal() + doy - 1).isoformat()
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid DSSAT date: {value!r}") from exc


def optional_dssat_date(value: str) -> str:
    """Return a blank for DSSAT missing-value codes, otherwise an ISO date."""
    token = str(value).strip()
    if token in {"", "-9", "-99", "-999", "-99.0", "-999.0"}:
        return ""
    return dssat_date(token)


def planting_labels(fieldnames: list[str]) -> tuple[str, ...]:
    labels = tuple(sorted(
        match.group(1)
        for field in fieldnames
        if (match := re.fullmatch(r"PDAT_(P\d+)", field))
    ))
    if labels != ("P01", "P02", "P03", "P04", "P05"):
        raise ValueError(f"Source must contain exactly PDAT_P01...PDAT_P05; found {labels}")
    return labels


def output_fields(kind: str) -> list[str]:
    fields = list(COMMON_OUTPUT_FIELDS)
    if kind == "disease":
        fields.extend(("TOTAL_SPRAYS", "DISEASE_ONSET_DAE"))
    return fields


def required_columns(fieldnames: list[str], columns: set[str], source: Path) -> None:
    missing = columns - set(fieldnames)
    if missing:
        raise ValueError(f"Source CSV lacks required columns {sorted(missing)}: {source}")


def build_record(row: dict[str, str], label: str, kind: str) -> dict[str, str]:
    disease = kind == "disease"
    suffix = "_disease" if disease else ""
    record = {
        "SIMULATION_ID": "|".join((
            row["COUNTRY"], row["CELL_ID"], row["PERIOD"], row["SIM_YEAR"],
            row["SCENARIO"], row["GCM"], label,
        )),
        "COUNTRY": row["COUNTRY"],
        "CELL_ID": row["CELL_ID"],
        "WEATHER_SOURCE_CELL_ID": row["WEATHER_SOURCE_CELL_ID"],
        "WEATHER_BORROWED": row["WEATHER_BORROWED"],
        "WEATHER_DISTANCE_KM": row["WEATHER_DISTANCE_KM"],
        "LATITUDE": row["LATITUDE"],
        "LONGITUDE": row["LONGITUDE"],
        "HARVEST_AREA_HA": row["HARVEST_AREA"],
        "MATURITY_GROUP": row["GM_WIN"],
        "PERIOD": row["PERIOD"],
        "SIM_YEAR": row["SIM_YEAR"],
        "SCENARIO": row["SCENARIO"],
        "GCM": row["GCM"],
        "PLANTING_LABEL": label,
        "PLANT_DATE": dssat_date(row[f"PDAT_{label}"]),
        "MATURITY_DATE": optional_dssat_date(row[f"MDAT{suffix}_{label}"]),
        "YIELD_KG_HA": row[f"HWAH{suffix}_{label}"],
    }
    if disease:
        record["TOTAL_SPRAYS"] = row[f"NSPRAYS_{label}"]
        record["DISEASE_ONSET_DAE"] = row[f"DISEASE_ONSET_DAE_{label}"]
    return record


def integer_key(value: str) -> tuple[int, int | str]:
    try:
        return 0, int(value)
    except (TypeError, ValueError):
        return 1, str(value)


def sort_key(row: dict[str, str]) -> tuple[object, ...]:
    """Keep every point together, then order its simulations chronologically."""
    return (
        integer_key(row["CELL_ID"]),
        PERIOD_ORDER.get(row["PERIOD"], 99),
        integer_key(row["SIM_YEAR"]),
        SCENARIO_ORDER.get(row["SCENARIO"], 99),
        row["SCENARIO"],
        row["GCM"],
        row["PLANTING_LABEL"],
        row["SIMULATION_ID"],
    )


def write_chunk(
    rows: list[dict[str, str]],
    fields: list[str],
    directory: Path,
    index: int,
) -> Path:
    rows.sort(key=sort_key)
    path = directory / f"chunk_{index:06d}.csv.gz"
    with gzip.open(path, "wt", newline="", encoding="utf-8", compresslevel=1) as destination:
        writer = csv.DictWriter(destination, fieldnames=fields, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    return path


def chunk_records(path: Path) -> Iterator[tuple[tuple[object, ...], str, dict[str, str]]]:
    with gzip.open(path, "rt", newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source, delimiter=";")
        for row in reader:
            yield sort_key(row), row["SIMULATION_ID"], row


def write_long(
    source_path: Path,
    output_path: Path,
    kind: str,
    *,
    chunk_rows: int = 100_000,
    temporary_root: Path | None = None,
) -> tuple[int, int]:
    """Expand P01--P05 to rows and externally sort them by point."""
    if kind not in {"disease", "control"}:
        raise ValueError(f"Unsupported output kind: {kind}")
    if chunk_rows < 1:
        raise ValueError("chunk_rows must be positive")

    fields = output_fields(kind)
    rows_in = rows_out = 0
    buffer: list[dict[str, str]] = []
    chunks: list[Path] = []
    temp_parent = temporary_root or output_path.parent
    temp_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f".{output_path.stem}.sort-", dir=temp_parent) as temp_name:
        temp_dir = Path(temp_name)
        with opener(source_path, "rt") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                raise ValueError(f"Source CSV has no header: {source_path}")
            labels = planting_labels(reader.fieldnames)
            suffix = "_disease" if kind == "disease" else ""
            required = set(SOURCE_IDENTITY) | {"N_PLANTING_DATES"}
            required.update(
                f"{field}{suffix}_{label}"
                for label in labels
                for field in ("MDAT", "HWAH")
            )
            required.update(f"PDAT_{label}" for label in labels)
            if kind == "disease":
                required.update(f"NSPRAYS_{label}" for label in labels)
                required.update(f"DISEASE_ONSET_DAE_{label}" for label in labels)
            required_columns(reader.fieldnames, required, source_path)

            for row in reader:
                rows_in += 1
                if row["N_PLANTING_DATES"] != "5":
                    raise ValueError(f"Expected five planting dates in {source_path}; row={rows_in}")
                for label in labels:
                    buffer.append(build_record(row, label, kind))
                    rows_out += 1
                    if len(buffer) >= chunk_rows:
                        chunks.append(write_chunk(buffer, fields, temp_dir, len(chunks)))
                        buffer = []

        if buffer:
            chunks.append(write_chunk(buffer, fields, temp_dir, len(chunks)))
        if not chunks:
            raise ValueError(f"Source CSV has no data rows: {source_path}")

        with opener(output_path, "wt") as destination:
            writer = csv.DictWriter(destination, fieldnames=fields, delimiter=";")
            writer.writeheader()
            previous_key: tuple[object, ...] | None = None
            for key, _simulation_id, row in heapq.merge(*(chunk_records(path) for path in chunks)):
                if previous_key is not None and key <= previous_key:
                    relation = "duplicate" if key == previous_key else "out-of-order"
                    raise ValueError(f"Final {relation} simulation sort key: {key}")
                writer.writerow(row)
                previous_key = key

    return rows_in, rows_out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_csv", type=Path, help="Internal merged wide CSV")
    parser.add_argument("output_csv", type=Path, help="Canonical long CSV (.csv or .csv.gz)")
    parser.add_argument("--kind", choices=("disease", "control"), required=True)
    parser.add_argument("--audit", type=Path, help="Export audit kept outside the public result directory")
    parser.add_argument("--chunk-rows", type=int, default=100_000)
    parser.add_argument(
        "--temporary-root",
        type=Path,
        help="Directory for compressed external-sort chunks (default: output directory)",
    )
    args = parser.parse_args()

    if not args.source_csv.is_file():
        raise SystemExit(f"Source CSV not found: {args.source_csv}")
    if args.output_csv.exists():
        raise SystemExit(f"Refusing to overwrite final summary: {args.output_csv}")
    report_path = args.audit or audit_path(args.output_csv)
    if report_path.exists():
        raise SystemExit(f"Refusing to overwrite summary audit: {report_path}")

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = ".partial" + "".join(args.output_csv.suffixes)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{args.output_csv.stem}.", suffix=suffix, dir=args.output_csv.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        rows_in, rows_out = write_long(
            args.source_csv,
            temporary,
            args.kind,
            chunk_rows=args.chunk_rows,
            temporary_root=args.temporary_root,
        )
        temporary.replace(args.output_csv)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    report = {
        "schema_version": 3,
        "status": "ok",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "kind": args.kind,
        "source_csv": str(args.source_csv.resolve()),
        "source_csv_sha256": sha256(args.source_csv),
        "output_csv": str(args.output_csv.resolve()),
        "output_csv_sha256": sha256(args.output_csv),
        "input_wide_rows": rows_in,
        "output_simulation_rows": rows_out,
        "row_definition": "pixel x period x planting year x scenario x weather GCM x planting date",
        "sort_order": [
            "CELL_ID", "PERIOD", "SIM_YEAR", "SCENARIO", "GCM", "PLANTING_LABEL"
        ],
        "delimiter": ";",
        "fields": output_fields(args.kind),
        "yield_variable": "HWAH (DSSAT harvested product yield, kg/ha)",
        "severity_included": False,
        "planting_date_means_included": False,
        "mme_included": False,
    }
    if args.kind == "disease":
        report["total_sprays"] = "Final NSPRAYS from DISMO.OUT for this planting-date simulation"
        report["disease_onset_dae"] = (
            "First DISMO.OUT record with printed SEV% > 0, expressed as days after emergence; "
            "blank when severity never becomes positive"
        )
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"Canonical {args.kind} summary written: input_wide_rows={rows_in}; "
        f"output_simulation_rows={rows_out}; output={args.output_csv}; audit={report_path}"
    )


if __name__ == "__main__":
    main()
