#!/usr/bin/env python3
"""Create one disease-free Pythia calibration run per generic maturity group."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import fiona


def required(value: str, name: str) -> str:
    if not value or value.startswith("REPLACE_WITH"):
        raise ValueError(f"Set {name}")
    return value


def cell_id_filename(value: object) -> str:
    """Return the numeric DSSAT weather filename represented by CellID."""
    try:
        return f"{int(float(value))}.WTH"
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid CellID value: {value!r}") from exc


def wth_contains_year(path: Path, year: int) -> bool:
    """Check the DATE column in a DSSAT WTH without loading it into memory."""
    with path.open(encoding="utf-8", errors="replace") as weather:
        for line in weather:
            match = re.match(r"\s*(\d{4})\d{3}\b", line)
            if match and int(match.group(1)) == year:
                return True
    return False


def validate_reference_inputs(sites: Path, weather_dir: Path, reference_year: int) -> None:
    """Refuse inverse calibration when calendar and weather years differ.

    The calibration is intentionally a single agricultural season.  Requiring
    CAL_YR0=CAL_YRN=reference_year and CAL_N=1 prevents a climatological GGCMI
    calendar from being silently compared with one historical WTH year.
    """
    with fiona.open(sites) as source:
        fields = set(source.schema["properties"])
        required_fields = {"CellID", "PLANT_DOY", "MAT_DOY", "GROW_DAYS", "CAL_YR0", "CAL_YRN", "CAL_N"}
        missing = required_fields - fields
        if missing:
            raise ValueError(f"Calendar sites missing {sorted(missing)}")
        cell_ids = set()
        for feature in source:
            props = feature["properties"]
            if (int(props["CAL_YR0"]) != reference_year or int(props["CAL_YRN"]) != reference_year
                    or int(props["CAL_N"]) != 1):
                raise ValueError(
                    "Inverse calibration requires a one-year GGCMI file with "
                    f"CAL_YR0=CAL_YRN={reference_year} and CAL_N=1. "
                    "Re-run prepare_ggcmi_calendar.py for that exact year."
                )
            cell_ids.add(cell_id_filename(props.get("WTH_CELLID") or props["CellID"]))
    missing_files, missing_year = [], []
    for filename in sorted(cell_ids):
        weather_file = weather_dir / filename
        if not weather_file.is_file():
            missing_files.append(filename)
        elif not wth_contains_year(weather_file, reference_year):
            missing_year.append(filename)
    if missing_files or missing_year:
        details = []
        if missing_files:
            details.append(f"missing WTH: {', '.join(missing_files[:10])}")
        if missing_year:
            details.append(f"without year {reference_year}: {', '.join(missing_year[:10])}")
        raise ValueError("Reference-weather validation failed (first 10 shown): " + "; ".join(details))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--profile", choices=["local", "hipergator"], required=True)
    parser.add_argument("--sites", type=Path, required=True, help="Calendar-enriched GPKG with PLANT_DOY and MAT_DOY")
    parser.add_argument("--harvest-area", type=Path, required=True)
    parser.add_argument("--weather-dir", required=True, help="One historical WTH directory used for every candidate")
    parser.add_argument("--reference-year", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    project = json.loads(args.project.read_text(encoding="utf-8"))
    profile, experiment = project["profiles"][args.profile], project["experiment"]
    for key in ("dssat_executable", "dssat_profile", "dssat_simulation_cde", "ghr_root", "soil_raster", "work_root"):
        required(profile[key], f"profiles.{args.profile}.{key}")
    candidates = json.loads(args.candidates.read_text(encoding="utf-8")).get("cultivars", [])
    if not candidates:
        raise ValueError("Candidate file has no cultivars")
    weather_dir = Path(args.weather_dir)
    if not weather_dir.is_dir():
        raise ValueError(f"Weather directory does not exist: {weather_dir}")
    validate_reference_inputs(args.sites, weather_dir, args.reference_year)
    with fiona.open(args.sites) as source:
        fields = set(source.schema["properties"])
    script_dir = Path(__file__).resolve().parent
    template_root = script_dir / "templates"
    if not template_root.is_dir():
        template_root = script_dir.parent / "templates"
    sites = str(args.sites.resolve()).replace("\\", "/")
    harea = str(args.harvest_area.resolve()).replace("\\", "/")
    management = experiment["management"]
    common = {
        "template": "SBG00001.SBX", "sites": f"xy_from_vector::{sites}", "nyers": 1,
        "startYear": args.reference_year, "id_soil": f"lookup_ghr::raster::{profile['soil_raster']}",
        "wsta": f"lookup_wth::WTH0::vector::{sites}::WTH_CELLID" if "WTH_CELLID" in fields else f"lookup_wth::WTH0::vector::{sites}::CellID", "ic_layers": "generate_ic_layers::$id_soil",
        "include": [profile["dssat_profile"], profile["dssat_simulation_cde"]],
        "harvestArea": f"raster::{harea}", "ph2ol": management["ph2ol"], "icin": management["icin"],
        "icsw%": management["icsw_percent"], "icrt": management["icrt"], "icres": management["icres"],
        "icren": management["icren"], "irrig": management["irrig"], "ramt": management["ramt"], "fen_tot": management["fen_tot"],
        "pfrst": "REPLACED_PER_RUN", "sdate": "date_offset::$pfrst::-10"
    }
    runs = []
    for cultivar in candidates:
        gm, ingeno, cname = str(cultivar["gm"]), str(cultivar["ingeno"]), str(cultivar["cname"])
        safe = "GM" + "".join(char if char.isalnum() else "_" for char in gm)
        runs.append({"name": f"gm_{safe}", "run_label": safe, "gm": gm, "ingeno": ingeno, "cname": cname,
                     "dises": "N", "planting_label": "GGCMI", "pfrst": f"auto_planting_window_doy_shape::vector::{sites}::PLANT_DOY::-1::0"})
    label = f"maturity_calibration_{experiment['country'].lower().replace(' ', '_')}_{args.reference_year}"
    candidate_path = str(args.candidates.resolve()).replace("\\", "/")
    cfg = {"name": label, "workDir": f"{profile['work_root'].rstrip('/')}/{label}", "templateDir": str(template_root.resolve()).replace("\\", "/"),
           "weatherDir": str(weather_dir).replace("\\", "/"), "threads": experiment["threads"], "cores": experiment["cores"], "ghr_root": profile["ghr_root"],
           "default_setup": common, "dssat": {"executable": profile["dssat_executable"]},
           "pipeline": {
               "country": experiment["country"],
               "reference_year": args.reference_year,
               "purpose": "inverse_maturity_calibration",
               "calibration_sites": sites,
               "maturity_candidates": candidate_path,
               "candidate_count": len(runs),
           }, "runs": runs}
    args.output.mkdir(parents=True, exist_ok=True)
    target = args.output / f"{label}.json"
    target.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {len(runs)} candidate-GM runs -> {target}")


if __name__ == "__main__":
    main()
