#!/usr/bin/env python3
"""Validate and compact one completed annual Pythia task while it is in scratch.

This script is deliberately called *inside* an array task.  It verifies the
expected number of DSSAT outputs, runs the established collate_summary.py
logic, and writes two compressed result shards plus audit.json.  The caller can
then stage that tiny directory to persistent Blue storage and allow the raw
DSSAT/Pythia directory tree to disappear with node-local scratch.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import fiona

from collate_summary import coordinates, summary_rows


def is_disease(run: dict) -> bool:
    return str(run.get("name", "")).startswith("disease_")


def site_path(run: dict, default_setup: dict) -> Path:
    value = str(run.get("sites", default_setup.get("sites", "")))
    prefix = "xy_from_vector::"
    if not value.startswith(prefix):
        raise ValueError(f"Run {run.get('name')} does not use a vector site file: {value}")
    path = Path(value[len(prefix):])
    if not path.is_file():
        raise ValueError(f"Site file is not readable: {path}")
    return path


def expected_outputs(config: dict) -> tuple[int, int, int]:
    """Return expected Summary.OUT, DISMO.OUT and unique site-point counts."""
    feature_counts: dict[Path, int] = {}
    summary = dismo = 0
    for run in config.get("runs", []):
        path = site_path(run, config.get("default_setup", {}))
        if path not in feature_counts:
            with fiona.open(path) as source:
                feature_counts[path] = len(source)
        count = feature_counts[path]
        summary += count
        if is_disease(run):
            dismo += count
    if not config.get("runs") or summary == 0 or dismo == 0:
        raise ValueError("Configuration has no expected annual disease/control outputs")
    return summary, dismo, sum(feature_counts.values())


def count_files(root: Path, filename: str) -> int:
    return sum(1 for _ in root.rglob(filename))


def dssat_date(value: str) -> dt.date | None:
    """Decode the YYYYDDD values written by annual DSSAT Summary.OUT files."""
    try:
        number = int(value)
        year, day = divmod(number, 1000)
        first = dt.date(year, 1, 1)
        if not 1 <= day <= (dt.date(year + 1, 1, 1) - first).days:
            return None
        return first + dt.timedelta(days=day - 1)
    except (TypeError, ValueError, OverflowError):
        return None


def verify_forced_planting_dates(config: dict, work_dir: Path) -> int:
    """Check every PDAT against its run-local annual GGCMI PLANT_DOY.

    The check is deliberately performed before raw scratch output is discarded.
    It confirms both forced planting and that DSSAT used the intended century in
    a multidecadal WTH file.
    """
    default = config.get("default_setup", {})
    checked = 0
    for run in config.get("runs", []):
        pfrst = str(run.get("pfrst", default.get("pfrst", "")))
        match = re.search(r"::PLANT_DOY::(-?\d+)::0$", pfrst)
        if not match:
            raise ValueError(f"Annual run {run.get('name')} has no parseable GGCMI PLANT_DOY rule: {pfrst}")
        pythia_offset = int(match.group(1))
        start_year = int(run.get("startYear", default.get("startYear")))
        path = site_path(run, default)
        expected: dict[tuple[float, float], dt.date] = {}
        with fiona.open(path) as source:
            if "PLANT_DOY" not in source.schema["properties"]:
                raise ValueError(f"Annual scratch sites lack PLANT_DOY: {path}")
            for feature in source:
                lon, lat = feature["geometry"]["coordinates"]
                doy = int(dict(feature["properties"])["PLANT_DOY"])
                expected[(round(lat, 4), round(lon, 4))] = dt.date(start_year, 1, 1) + dt.timedelta(days=doy + pythia_offset)
        run_dir = work_dir / str(run.get("name") or "")
        summaries = sorted(run_dir.rglob("Summary.OUT")) if run_dir.is_dir() else []
        if len(summaries) != len(expected):
            raise ValueError(f"Cannot verify planting dates for {run.get('name')}: {len(summaries)}/{len(expected)} Summary.OUT")
        for summary in summaries:
            key = tuple(round(value, 4) for value in coordinates(summary.parent))
            wanted = expected.get(key)
            rows = summary_rows(summary)
            actual = dssat_date(rows[0]["PDAT"]) if len(rows) == 1 else None
            if wanted is None or actual != wanted:
                raise ValueError(
                    f"Forced GGCMI planting date mismatch in {summary}: "
                    f"expected={wanted}, observed={rows[0]['PDAT'] if rows else 'none'}"
                )
            checked += 1
    return checked


def safe_country(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")


def open_csv(path: Path):
    return gzip.open(path, "rt", newline="", encoding="utf-8") if path.suffix == ".gz" else path.open("r", newline="", encoding="utf-8")


def output_statistics(disease_csv: Path, control_csv: Path) -> tuple[int, int, int | None, int | None]:
    def count_and_nsprays(path: Path, disease: bool) -> tuple[int, list[int]]:
        count, sprays = 0, []
        with open_csv(path) as source:
            for row in csv.DictReader(source):
                count += 1
                if disease:
                    for field, value in row.items():
                        if field.startswith("NSPRAYS_P") and value not in {None, ""}:
                            sprays.append(int(float(value)))
        return count, sprays

    disease_rows, sprays = count_and_nsprays(disease_csv, disease=True)
    control_rows, _ = count_and_nsprays(control_csv, disease=False)
    return disease_rows, control_rows, min(sprays) if sprays else None, max(sprays) if sprays else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_config", type=Path, help="Canonical JSON from the manifest on Blue")
    parser.add_argument("scratch_work_dir", type=Path, help="Completed task-local Pythia workDir")
    parser.add_argument("shard_dir", type=Path, help="Local directory to receive compact outputs")
    parser.add_argument(
        "--execution-config",
        type=Path,
        help="Task-local JSON used by Pythia; preserves annual scratch calendar attributes in the CSV.",
    )
    parser.add_argument("--job-id", default="")
    parser.add_argument("--array-task-id", default="")
    parser.add_argument("--weather-window-audit", type=Path)
    args = parser.parse_args()

    source_config = args.source_config.resolve()
    work_dir = args.scratch_work_dir.resolve()
    if not source_config.is_file() or not work_dir.is_dir():
        raise SystemExit("Canonical configuration or completed scratch work directory is missing")
    config = json.loads(source_config.read_text(encoding="utf-8"))
    metadata = config.get("pipeline", {})
    if not metadata.get("annual"):
        raise SystemExit("This collator accepts only annual NYERS=1 configurations")
    country = str(metadata.get("country", ""))
    if not country:
        raise SystemExit("Configuration has no pipeline.country")
    if metadata.get("disease_design") != "single_calibrated_parameter_set":
        raise SystemExit("Configuration does not use the single calibrated disease input design")
    disease_snapshot = Path(str(metadata.get("disease_parameters_snapshot") or ""))
    disease_hash = str(metadata.get("disease_parameters_sha256") or "")
    if not disease_snapshot.is_file() or len(disease_hash) != 64 or sha256(disease_snapshot) != disease_hash:
        raise SystemExit("Calibrated disease_parameters.txt snapshot is missing or changed")

    execution = config
    if args.execution_config is not None:
        execution_path = args.execution_config.resolve()
        if not execution_path.is_file():
            raise SystemExit(f"Task-local execution configuration is missing: {execution_path}")
        execution = json.loads(execution_path.read_text(encoding="utf-8"))
        if str(execution.get("name") or execution_path.stem) != str(config.get("name") or source_config.stem):
            raise SystemExit("Canonical and task-local configuration names do not agree")
        if not execution.get("pipeline", {}).get("annual"):
            raise SystemExit("Task-local execution configuration is not annual")

    expected_summary, expected_dismo, site_count = expected_outputs(execution)
    observed_summary = count_files(work_dir, "Summary.OUT")
    observed_dismo = count_files(work_dir, "DISMO.OUT")
    errors = count_files(work_dir, "ERROR.OUT")
    warnings = count_files(work_dir, "WARNING.OUT")
    if observed_summary != expected_summary or observed_dismo != expected_dismo or errors:
        raise SystemExit(
            "Task output audit failed: "
            f"Summary.OUT {observed_summary}/{expected_summary}; "
            f"DISMO.OUT {observed_dismo}/{expected_dismo}; ERROR.OUT={errors}"
        )
    planting_dates_checked = verify_forced_planting_dates(execution, work_dir)
    if planting_dates_checked != expected_summary:
        raise SystemExit(f"Forced planting-date audit checked {planting_dates_checked}/{expected_summary} Summary.OUT files")

    args.shard_dir.mkdir(parents=True, exist_ok=False)
    collator = Path(__file__).with_name("collate_summary.py")
    command = [
        sys.executable, str(collator), str(work_dir), str(args.shard_dir),
        "--country", country,
        "--period", str(metadata.get("period") or ""),
        "--climate-window", str(metadata.get("climate_window") or ""),
        "--scenario", str(metadata.get("scenario") or ""),
        "--gcm", str(metadata.get("gcm") or ""),
        "--gzip",
    ]
    simulation_year = metadata.get("simulation_year")
    if simulation_year is not None:
        command.extend(["--simulation-year", str(simulation_year)])
    for label in metadata.get("planting_labels", []):
        command.extend(["--planting-label", str(label)])
    if metadata.get("planting_date_role"):
        command.extend(["--planting-date-role", str(metadata["planting_date_role"])])
    if metadata.get("calendar", {}).get("mode") in {
        "annual_matched", "annual_scenario_reference", "climatological_reference"
    }:
        command.append("--include-calendar-fields")
    for path in sorted({site_path(run, execution.get("default_setup", {})) for run in execution.get("runs", [])}):
        command.extend(["--sites", str(path)])
    subprocess.run(command, check=True)

    country_name = safe_country(country)
    disease_csv = args.shard_dir / f"summary_disease_{country_name}.csv.gz"
    control_csv = args.shard_dir / f"summary_control_{country_name}.csv.gz"
    if not disease_csv.is_file() or not control_csv.is_file():
        raise SystemExit("Collator did not create both compressed output shards")
    disease_rows, control_rows, nsp_min, nsp_max = output_statistics(disease_csv, control_csv)
    if disease_rows != site_count or control_rows != site_count:
        raise SystemExit(
            f"Compact row audit failed: disease={disease_rows}/{site_count}; control={control_rows}/{site_count}"
        )

    calendar = metadata.get("calendar", {})
    calendar_audit = {
        key: calendar.get(key)
        for key in (
            "mode", "source", "scenario", "reference_gcm", "weather_gcm", "year",
            "source_start_year", "source_end_year", "stat",
        )
    }
    if calendar_audit.get("mode") in {
        "annual_matched", "annual_scenario_reference", "climatological_reference"
    }:
        calendar_path = Path(str(calendar_audit.get("source") or ""))
        if not calendar_path.is_file():
            raise SystemExit(f"GGCMI calendar disappeared before audit: {calendar_path}")
        calendar_audit["sha256"] = sha256(calendar_path)

    weather_window = None
    if args.weather_window_audit is not None:
        if not args.weather_window_audit.is_file():
            raise SystemExit(f"Weather-window audit is missing: {args.weather_window_audit}")
        weather_window = json.loads(args.weather_window_audit.read_text(encoding="utf-8"))

    audit = {
        "schema_version": 2,
        "status": "ok",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "config_name": str(config.get("name") or source_config.stem),
        "source_config": str(source_config),
        "source_config_sha256": sha256(source_config),
        "slurm_job_id": str(args.job_id),
        "slurm_array_task_id": str(args.array_task_id),
        "country": country,
        "period": metadata.get("period") or "",
        "climate_window": metadata.get("climate_window") or "",
        "scenario": metadata.get("scenario") or "",
        "gcm": metadata.get("gcm") or "",
        "simulation_year": simulation_year,
        "disease_design": metadata.get("disease_design"),
        "disease_parameters": {
            "snapshot": metadata.get("disease_parameters_snapshot"),
            "sha256": metadata.get("disease_parameters_sha256"),
        },
        "site_count": site_count,
        "gm_production_qc": metadata.get("gm_production_qc"),
        "files": {
            "Summary.OUT": {"expected": expected_summary, "observed": observed_summary},
            "DISMO.OUT": {"expected": expected_dismo, "observed": observed_dismo},
            "ERROR.OUT": {"observed": errors},
            "WARNING.OUT": {"observed": warnings},
        },
        "result_rows": {"disease": disease_rows, "control": control_rows},
        "nsprays": {"minimum": nsp_min, "maximum": nsp_max},
        "forced_planting_dates": {"expected": expected_summary, "verified": planting_dates_checked},
        "calendar": calendar_audit,
        "outputs": {"disease": disease_csv.name, "control": control_csv.name},
    }
    if weather_window is not None:
        audit["weather_window"] = weather_window
    (args.shard_dir / "audit.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(
        f"Task audit OK: sites={site_count}; Summary.OUT={observed_summary}; "
        f"DISMO.OUT={observed_dismo}; warnings={warnings}; NSPRAYS={nsp_min}..{nsp_max}"
    )


if __name__ == "__main__":
    main()
