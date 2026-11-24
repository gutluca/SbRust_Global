#!/usr/bin/env python3
"""Create a task-local copy of a Pythia configuration.

Only ``workDir`` is changed.  Inputs, templates, WTH and soil files remain in
their canonical shared locations on Blue/Orange; the numerous temporary DSSAT
run directories are created on the node-local Slurm scratch filesystem.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

VECTOR_PREFIX = "xy_from_vector::"


def vector_site_paths(config: dict) -> list[tuple[str, Path]]:
    """Return the persistent GeoPackages referenced by Pythia runs."""
    paths: list[tuple[str, Path]] = []
    default = config.get("default_setup", {})
    for run in config.get("runs", []):
        value = str(run.get("sites", default.get("sites", "")))
        if not value.startswith(VECTOR_PREFIX):
            raise ValueError(f"Annual calendar run {run.get('name')} has no vector sites: {value!r}")
        raw = value[len(VECTOR_PREFIX):]
        if not raw:
            raise ValueError(f"Annual calendar run {run.get('name')} has an empty vector sites path")
        paths.append((raw, Path(raw)))
    if not paths:
        raise ValueError("Annual calendar configuration has no runs")
    return paths


def replace_strings(value, replacements: dict[str, str]):
    """Replace site-file paths everywhere a Pythia expression can use them."""
    if isinstance(value, dict):
        return {key: replace_strings(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [replace_strings(item, replacements) for item in value]
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
    return value


def materialize_annual_calendar(config: dict, scratch_root: Path) -> None:
    """Overlay one annual GGCMI year on each GM-zone file in node-local scratch.

    The durable GeoPackage keeps the fixed GM.  Only the annual calendar is
    materialized per task, avoiding hundreds of persistent country-scale files.
    """
    metadata = config.setdefault("pipeline", {})
    calendar = metadata.get("calendar", {})
    if not metadata.get("calendar_driven") or calendar.get("mode") not in {
        "annual_matched", "annual_scenario_reference", "climatological_reference"
    }:
        return
    if not metadata.get("annual"):
        raise ValueError("Calendar-driven scratch materialization is valid only for annual NYERS=1 configurations")
    source = Path(str(calendar.get("source") or ""))
    if not source.is_file():
        raise FileNotFoundError(
            f"GGCMI calendar is not readable: {source}. "
            "Upload/register the declared scenario-reference calendar before submitting the task."
        )
    year = int(calendar.get("year"))
    if year != int(metadata.get("simulation_year")):
        raise ValueError("Calendar year and pipeline.simulation_year must agree")
    source_start_year = int(calendar.get("source_start_year", year))
    source_end_year = int(calendar.get("source_end_year", year))
    if source_end_year < source_start_year:
        raise ValueError("Calendar source_end_year must be >= source_start_year")
    stat = str(calendar.get("stat") or "median")
    if stat not in {"median", "mean"}:
        raise ValueError("Calendar stat must be 'median' or 'mean'")

    # Keep the legacy static-calendar path independent of the optional xarray
    # dependency.  The import is needed only by the annual GGCMI branch.
    from prepare_ggcmi_calendar import enrich_sites

    target_root = scratch_root / "calendar_sites"
    target_root.mkdir(parents=True, exist_ok=False)
    replacements: dict[str, str] = {}
    materialized = []
    for index, (raw, site_path) in enumerate(dict(vector_site_paths(config)).items(), start=1):
        if not site_path.is_file():
            raise FileNotFoundError(f"Calibrated zone GeoPackage is not readable: {site_path}")
        target = target_root / f"{index:03d}_{site_path.name}"
        # Production uses GGCMI planting dates only. Maturity is simulated by
        # DSSAT with the already selected, fixed GM, not forced by GGCMI.
        count = enrich_sites(site_path, source, target, source_start_year, source_end_year, stat,
                             require_maturity=False)
        replacements[raw] = str(target)
        materialized.append({"source_sites": raw, "scratch_sites": str(target), "site_count": count})
    rewritten = replace_strings(config, replacements)
    rewritten["pipeline"]["calendar_materialization"] = {
        "source": str(source),
        "scenario": calendar.get("scenario"),
        "reference_gcm": calendar.get("reference_gcm"),
        "weather_gcm": calendar.get("weather_gcm"),
        "year": year,
        "source_start_year": source_start_year,
        "source_end_year": source_end_year,
        "stat": stat,
        "site_files": materialized,
    }
    config.clear()
    config.update(rewritten)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_config", type=Path)
    parser.add_argument("output_config", type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()

    source = args.source_config.resolve()
    if not source.is_file():
        raise SystemExit(f"Configuration not found: {source}")
    work_dir = args.work_dir.resolve()
    if work_dir == Path("/"):
        raise SystemExit("Refusing to use filesystem root as a scratch work directory")

    config = json.loads(source.read_text(encoding="utf-8"))
    pipeline = config.get("pipeline", {})
    purpose = str(pipeline.get("purpose", ""))
    if not pipeline.get("annual") and purpose not in {"inverse_maturity_calibration", "inverse_maturity_ensemble_member"}:
        raise SystemExit(
            "Scratch execution is restricted to annual NYERS=1 configurations "
            "or inverse maturity calibration configurations"
        )
    declared_work_dir = config.get("workDir")
    if not declared_work_dir:
        raise SystemExit("Configuration has no workDir")

    config["workDir"] = str(work_dir)
    config.setdefault("pipeline", {})["scratch_execution"] = True
    config["pipeline"]["declared_work_dir"] = declared_work_dir
    config["pipeline"]["source_config"] = str(source)
    materialize_annual_calendar(config, args.output_config.parent)

    args.output_config.parent.mkdir(parents=True, exist_ok=True)
    args.output_config.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Scratch configuration written: {args.output_config}")


if __name__ == "__main__":
    main()
