#!/usr/bin/env python3
"""Validate one GCM member of the multi-year generic-GM calibration."""
from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import gzip
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import fiona

from select_maturity_group import coordinates


VECTOR_PREFIX = "xy_from_vector::"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def site_path(run: dict) -> Path:
    value = str(run.get("sites", ""))
    if not value.startswith(VECTOR_PREFIX):
        raise ValueError(f"Calibration run {run.get('name')} has no vector sites")
    path = Path(value[len(VECTOR_PREFIX):])
    if not path.is_file():
        raise FileNotFoundError(f"Calibration sites not found: {path}")
    return path


def date_from_doy(year: int, day: int) -> dt.date | None:
    """Return an actual date, rejecting day 366 outside leap years."""
    if day < 1 or day > (dt.date(year + 1, 1, 1) - dt.date(year, 1, 1)).days:
        return None
    return dt.date(year, 1, 1) + dt.timedelta(days=day - 1)


def target_maturity_date(planted: dt.date, maturity_doy: int, growing_days: float) -> dt.date | None:
    """Resolve GGCMI MAT_DOY to the agricultural season beginning at ``planted``.

    Calendar days alone are ambiguous for seasons crossing 31 December.  The
    reported growing period is used only to select the correct calendar year;
    the directly reported GGCMI maturity date remains the phenology target.
    """
    candidates = []
    for year in (planted.year, planted.year + 1, planted.year + 2):
        maturity = date_from_doy(year, maturity_doy)
        if maturity is not None and maturity > planted:
            span = (maturity - planted).days
            candidates.append((abs(span - growing_days), span, maturity))
    return min(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else None


def yyyyddd(value: dt.date) -> str:
    return f"{value.year}{value.timetuple().tm_yday:03d}"


def targets(path: Path, year: int) -> dict[tuple[float, float], tuple[str, int, int, float, dt.date, dt.date]]:
    """Read one exact-year GGCMI planting/maturity calendar as real dates."""
    result: dict[tuple[float, float], tuple[str, int, int, float, dt.date, dt.date]] = {}
    with fiona.open(path) as source:
        required = {"CellID", "PLANT_DOY", "MAT_DOY", "GROW_DAYS", "CAL_YR0", "CAL_YRN", "CAL_N"}
        missing = required - set(source.schema["properties"])
        if missing:
            raise ValueError(f"Calendar sites missing {sorted(missing)}: {path}")
        for feature in source:
            lon, lat = feature["geometry"]["coordinates"]
            props = dict(feature["properties"])
            if int(props["CAL_YR0"]) != year or int(props["CAL_YRN"]) != year or int(props["CAL_N"]) != 1:
                raise ValueError(f"Calibration calendar is not an exact {year} calendar: {path}")
            plant, maturity, growing = int(props["PLANT_DOY"]), int(props["MAT_DOY"]), float(props["GROW_DAYS"])
            planted = date_from_doy(year, plant)
            target_maturity = target_maturity_date(planted, maturity, growing) if planted else None
            if planted is None or target_maturity is None or not 1 <= growing <= 366:
                raise ValueError(f"Invalid GGCMI calendar at ({lat}, {lon}) in {path}")
            key = (round(lat, 4), round(lon, 4))
            if key in result:
                raise ValueError(f"Duplicate calendar coordinate {key} in {path}")
            result[key] = (str(props["CellID"]), plant, maturity, growing, planted, target_maturity)
    if not result:
        raise ValueError(f"Calendar sites are empty: {path}")
    return result


def summary_dates(path: Path) -> tuple[str, str]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    header_index = next(i for i, line in enumerate(lines) if line.startswith("@") and "PDAT" in line and "MDAT" in line)
    for line in lines[header_index + 1:]:
        if re.match(r"^\s*\d+\s", line):
            values = line.split()
            # Summary.OUT's model identifier can occupy more than one
            # whitespace-delimited token.  Its fixed-width header consequently
            # cannot be safely aligned with ``split()``.  WSTA is the stable
            # weather-file token; DSSAT places PDAT and MDAT at +7 and +10 from
            # it, respectively.  This is also the parser used for final output.
            try:
                weather_index = next(i for i, value in enumerate(values) if value.upper().endswith(".WTH"))
                return values[weather_index + 7], values[weather_index + 10]
            except (IndexError, StopIteration) as exc:
                raise ValueError(f"Malformed Summary.OUT data row in {path}: {line}") from exc
    raise ValueError(f"No Summary.OUT data row found in {path}")


def dssat_date(value: str) -> dt.date | None:
    try:
        number = int(value)
        year, day = divmod(number, 1000)
        return dt.date(year, 1, 1) + dt.timedelta(days=day - 1) if 1 <= day <= (366 if calendar.isleap(year) else 365) else None
    except (TypeError, ValueError, OverflowError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_config", type=Path)
    parser.add_argument("scratch_work_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--job-id", default="")
    parser.add_argument("--array-task-id", default="")
    parser.add_argument("--weather-window-audit", type=Path)
    args = parser.parse_args()

    source_config = args.source_config.resolve()
    work_dir = args.scratch_work_dir.resolve()
    if not source_config.is_file() or not work_dir.is_dir():
        raise SystemExit("Canonical member configuration or scratch workDir is missing")
    if args.output_dir.exists():
        raise SystemExit(f"Refusing to overwrite ensemble-member output: {args.output_dir}")
    config = json.loads(source_config.read_text(encoding="utf-8"))
    metadata = config.get("pipeline", {})
    if metadata.get("purpose") != "inverse_maturity_ensemble_member":
        raise SystemExit("This collator accepts only inverse_maturity_ensemble_member configurations")
    if metadata.get("candidate_method") != "dssat_generic_gm_v1":
        raise SystemExit("This collator requires candidate_method=dssat_generic_gm_v1")
    gcm = str(metadata.get("gcm") or "")
    years = tuple(int(value) for value in metadata.get("calibration_years", []))
    if not gcm or len(years) < 2 or len(set(years)) != len(years):
        raise SystemExit("Ensemble member needs one GCM and at least two unique calibration_years")
    candidate_file = Path(str(metadata.get("maturity_candidates") or ""))
    if not candidate_file.is_file():
        raise SystemExit(f"Candidate JSON not found: {candidate_file}")
    candidate_data = json.loads(candidate_file.read_text(encoding="utf-8"))
    candidates = candidate_data.get("cultivars", [])
    candidate_by_gm = {str(item.get("gm") or ""): item for item in candidates}
    if not candidate_by_gm or "" in candidate_by_gm or len(candidate_by_gm) != len(candidates):
        raise SystemExit("Generic candidate JSON has missing or duplicate GMs")

    runs = list(config.get("runs", []))
    if not runs:
        raise SystemExit("Ensemble member has no runs")
    if any(str(run.get("dises")) != "N" for run in runs):
        raise SystemExit("Inverse generic-GM calibration must be disease-free")
    expected_names = {str(run.get("name") or "") for run in runs}
    if "" in expected_names or len(expected_names) != len(runs):
        raise SystemExit("Ensemble member has missing or duplicate run names")
    run_gms = {str(run.get("gm") or "") for run in runs}
    if run_gms != set(candidate_by_gm):
        raise SystemExit("Member runs and candidate JSON do not define the same GM set")
    if int(metadata.get("candidate_count", -1)) != len(candidate_by_gm):
        raise SystemExit("pipeline.candidate_count does not match candidate JSON")
    if int(metadata.get("case_count", -1)) != len(runs):
        raise SystemExit("pipeline.case_count does not match member runs")

    args.output_dir.mkdir(parents=True)
    diagnostics = args.output_dir / "maturity_member_candidates.csv.gz"
    fields = [
        "CELL_ID", "LATITUDE", "LONGITUDE", "GM", "INGENO", "CNAME",
        "GCM", "YEAR", "SIM_PDOY", "SIM_MDOY", "SIM_GROW_DAYS", "TARGET_PDOY", "TARGET_MDOY",
        "TARGET_MDAT", "TARGET_GROW_DAYS", "TARGET_DATE_GROW_DAYS", "GSL_DATE_OFFSET_DAYS",
        "PDATE_ERROR_DAYS", "ABS_GROW_ERROR_DAYS", "ABS_MAT_ERROR_DAYS", "STATUS",
    ]
    per_run: dict[str, int] = {}
    diagnostic_rows = 0
    with gzip.open(diagnostics, "wt", newline="", encoding="utf-8", compresslevel=9) as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for run in runs:
            name, gm, year = str(run["name"]), str(run["gm"]), int(run["startYear"])
            if year not in years:
                raise SystemExit(f"Run {name} has a year not declared in pipeline.calibration_years")
            candidate = candidate_by_gm.get(gm)
            if candidate is None or str(run.get("ingeno")) != str(candidate["ingeno"]):
                raise SystemExit(f"Run {name} does not match its declared generic GM")
            targets_by_coordinate = targets(site_path(run), year)
            summaries = sorted((work_dir / name).rglob("Summary.OUT")) if (work_dir / name).is_dir() else []
            per_run[name] = len(summaries)
            if len(summaries) != len(targets_by_coordinate):
                raise SystemExit(f"Incomplete member output for {name}: {len(summaries)}/{len(targets_by_coordinate)} Summary.OUT")
            observed: set[tuple[float, float]] = set()
            for summary in summaries:
                lat, lon = coordinates(summary.parent)
                key = (round(lat, 4), round(lon, 4))
                if key not in targets_by_coordinate or key in observed:
                    raise SystemExit(f"Unexpected or duplicate output coordinate {key} in {summary}")
                observed.add(key)
                cell_id, target_plant, target_maturity, target_growing, target_planted, target_matured = targets_by_coordinate[key]
                pdat, mdat = summary_dates(summary)
                planted, matured = dssat_date(pdat), dssat_date(mdat)
                row = {
                    "CELL_ID": cell_id, "LATITUDE": f"{key[0]:.4f}", "LONGITUDE": f"{key[1]:.4f}",
                    "GM": gm,
                    "INGENO": str(candidate["ingeno"]), "CNAME": str(candidate["cname"]),
                    "GCM": gcm, "YEAR": str(year), "SIM_PDOY": pdat, "SIM_MDOY": mdat,
                    "SIM_GROW_DAYS": "", "TARGET_PDOY": str(target_plant), "TARGET_MDOY": str(target_maturity),
                    "TARGET_MDAT": yyyyddd(target_matured), "TARGET_GROW_DAYS": f"{target_growing:.6g}",
                    "TARGET_DATE_GROW_DAYS": str((target_matured - target_planted).days),
                    "GSL_DATE_OFFSET_DAYS": f"{(target_matured - target_planted).days - target_growing:.6g}",
                    "PDATE_ERROR_DAYS": "", "ABS_GROW_ERROR_DAYS": "", "ABS_MAT_ERROR_DAYS": "",
                    "STATUS": "invalid_dates",
                }
                if planted is not None:
                    planting_error = abs((planted - target_planted).days)
                    row["PDATE_ERROR_DAYS"] = str(planting_error)
                    if planting_error != 0:
                        row["STATUS"] = "planting_mismatch"
                if planted is not None and matured is not None and matured > planted and row["STATUS"] != "planting_mismatch":
                    simulated_growing = (matured - planted).days
                    if simulated_growing <= 550:
                        row["SIM_GROW_DAYS"] = str(simulated_growing)
                        row["ABS_GROW_ERROR_DAYS"] = f"{abs(simulated_growing - target_growing):.6g}"
                        row["ABS_MAT_ERROR_DAYS"] = str(abs((matured - target_matured).days))
                        row["STATUS"] = "valid"
                writer.writerow(row)
                diagnostic_rows += 1
            if observed != set(targets_by_coordinate):
                raise SystemExit(f"Missing coordinate output in {name}")

    errors = list(work_dir.rglob("ERROR.OUT"))
    warnings = list(work_dir.rglob("WARNING.OUT"))
    if errors:
        raise SystemExit(f"Member generated {len(errors)} ERROR.OUT files; first={errors[0]}")
    weather_window = None
    if args.weather_window_audit is not None:
        if not args.weather_window_audit.is_file():
            raise SystemExit(f"Weather-window audit is missing: {args.weather_window_audit}")
        weather_window = json.loads(args.weather_window_audit.read_text(encoding="utf-8"))
    audit = {
        "schema_version": 3, "status": "ok", "created_utc": datetime.now(timezone.utc).isoformat(),
        "config_name": str(config.get("name") or source_config.stem), "source_config": str(source_config),
        "source_config_sha256": sha256(source_config), "slurm_job_id": str(args.job_id),
        "slurm_array_task_id": str(args.array_task_id), "country": metadata.get("country"), "gcm": gcm,
        "calibration_years": list(years), "candidate_method": "dssat_generic_gm_v1",
        "candidate_count": len(candidate_by_gm), "case_count": len(runs),
        "site_count": len(targets(site_path(runs[0]), int(runs[0]["startYear"]))),
        "files": {"Summary.OUT": {"expected": sum(per_run.values()), "observed": sum(per_run.values()), "by_run": per_run}, "ERROR.OUT": {"observed": 0}, "WARNING.OUT": {"observed": len(warnings)}},
        "diagnostic_rows": diagnostic_rows, "outputs": {"candidate_diagnostics": diagnostics.name},
    }
    if weather_window is not None:
        audit["weather_window"] = weather_window
    (args.output_dir / "audit.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(f"Validated {diagnostic_rows} member simulations for {gcm} -> {args.output_dir}")


if __name__ == "__main__":
    main()
