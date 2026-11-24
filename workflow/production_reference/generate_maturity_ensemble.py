#!/usr/bin/env python3
"""Generate the historical-reference member for inverse generic-GM calibration.

Each member tests the complete, unmodified DSSAT generic soybean maturity
library against every annual GGCMI historical calendar season available in the
registered NetCDF.  The selector chooses one discrete GM per pixel and that
GM remains fixed in production.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import fiona
import xarray as xr

from generate_configs import calendar_reference_gcm, calendar_source, required
from prepare_ggcmi_calendar import enrich_sites, years_from_time


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_") or "value"


def cell_id_filename(value: object) -> str:
    try:
        return f"{int(float(value))}.WTH"
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid CellID value: {value!r}") from exc


def wth_years(path: Path, wanted: set[int]) -> set[int]:
    found: set[int] = set()
    with path.open(encoding="utf-8", errors="replace") as weather:
        for line in weather:
            match = re.match(r"\s*(\d{4})\d{3}\b", line)
            if match:
                year = int(match.group(1))
                if year in wanted:
                    found.add(year)
                    if found == wanted:
                        break
    return found


def validate_weather_years(sites: Path, weather_dir: Path, years: tuple[int, ...]) -> None:
    """Confirm the weather supports a season crossing either year boundary."""
    cell_ids: set[str] = set()
    with fiona.open(sites) as source:
        if "CellID" not in source.schema["properties"]:
            raise ValueError("Calibration sites must contain CellID")
        for feature in source:
            props = dict(feature["properties"])
            cell_ids.add(cell_id_filename(props.get("WTH_CELLID") or props["CellID"]))
    wanted = {year + offset for year in years for offset in (-1, 0, 1)}
    missing_files, missing_years = [], []
    for filename in sorted(cell_ids):
        path = weather_dir / filename
        if not path.is_file():
            missing_files.append(filename)
            continue
        missing = wanted - wth_years(path, wanted)
        if missing:
            missing_years.append(f"{filename}: {','.join(str(year) for year in sorted(missing))}")
    if missing_files or missing_years:
        details = []
        if missing_files:
            details.append("missing WTH: " + ", ".join(missing_files[:10]))
        if missing_years:
            details.append("missing years: " + "; ".join(missing_years[:10]))
        raise ValueError("Historical weather validation failed (first 10 shown): " + " | ".join(details))


def calendar_years(path: Path) -> tuple[int, ...]:
    """Return the complete ordered annual record encoded in one GGCMI file."""
    with xr.open_dataset(path, decode_times=False) as dataset:
        if "time" not in dataset.coords:
            raise ValueError(f"GGCMI calendar has no time coordinate: {path}")
        years = years_from_time(
            dataset["time"].values,
            str(dataset["time"].attrs.get("units", "")),
        )
    result = tuple(int(year) for year in years)
    if len(result) < 2 or len(set(result)) != len(result):
        raise ValueError(f"GGCMI historical calendar needs at least two unique annual records: {path}")
    if tuple(sorted(result)) != result:
        raise ValueError(f"GGCMI historical calendar time coordinate is not strictly increasing: {path}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--profile", choices=["local", "hipergator"], required=True)
    parser.add_argument("--sites", type=Path, required=True, help="Base point GPKG; it may already contain an old calendar.")
    parser.add_argument("--harvest-area", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--name-suffix",
        default="",
        help="Unique shard label appended to the member name when sites are split across jobs.",
    )
    parser.add_argument(
        "--gcm",
        action="append",
        help=(
            "Optional explicit historical reference GCM. In the current "
            "scenario-reference design it must equal calendar.reference_gcms.historical; "
            "omitting it uses that reference automatically."
        ),
    )
    parser.add_argument(
        "--year", type=int, action="append",
        help="Smoke-test override. Omit it in production to use every historical year in the GGCMI file.",
    )
    args = parser.parse_args()

    project = json.loads(args.project.read_text(encoding="utf-8"))
    profile, experiment = project["profiles"][args.profile], project["experiment"]
    for key in ("dssat_executable", "dssat_profile", "dssat_simulation_cde", "ghr_root", "soil_raster", "work_root"):
        required(profile[key], f"profiles.{args.profile}.{key}")
    calendar = experiment.get("calendar", {})
    if not calendar.get("enabled") or calendar.get("mode") not in {"annual_matched", "annual_scenario_reference"}:
        raise ValueError(
            "Ensemble GM calibration requires calendar.enabled=true and an annual GGCMI calendar mode"
        )
    candidate_data = json.loads(args.candidates.read_text(encoding="utf-8"))
    candidates = candidate_data.get("cultivars", [])
    if not candidates:
        raise ValueError("Candidate file has no cultivars")
    method = str(candidate_data.get("method") or "")
    if method != "dssat_generic_gm_v1":
        raise ValueError("This ensemble generator requires method=dssat_generic_gm_v1")
    candidate_gms = [str(item.get("gm") or "") for item in candidates]
    if not all(candidate_gms) or len(set(candidate_gms)) != len(candidate_gms):
        raise ValueError("Generic GM candidate file has missing or duplicate GM identifiers")
    for candidate in candidates:
        if not str(candidate.get("ingeno") or "") or not str(candidate.get("cname") or ""):
            raise ValueError("Every generic GM candidate needs ingeno and cname")
    if not args.sites.is_file() or not args.harvest_area.is_file():
        raise ValueError("Calibration sites or harvested-area raster does not exist")
    with fiona.open(args.sites) as source:
        site_fields = set(source.schema["properties"])
    configured_gcms = [str(value) for value in experiment["gcms"]]
    requested_gcms = [str(value) for value in (args.gcm or [])]
    unknown = set(requested_gcms) - set(configured_gcms)
    if unknown:
        raise ValueError(f"GCM(s) are not configured in experiment.gcms: {sorted(unknown)}")
    if str(calendar.get("mode")) == "annual_scenario_reference":
        historical_reference = calendar_reference_gcm(calendar, "historical", "")
        if requested_gcms and requested_gcms != [historical_reference]:
            raise ValueError(
                "Inverse GM calibration must use only the GGCMI historical-reference GCM "
                f"{historical_reference!r}; using multiple WTH GCMs would confound cultivar "
                "selection with structural climate-model differences."
            )
        gcms = [historical_reference]
    else:
        gcms = requested_gcms or configured_gcms
    args.output.mkdir(parents=True, exist_ok=True)
    template_root = Path(__file__).resolve().parents[1] / "templates"
    candidate_path = str(args.candidates.resolve()).replace("\\", "/")
    base_sites = str(args.sites.resolve()).replace("\\", "/")
    harea = str(args.harvest_area.resolve()).replace("\\", "/")
    management = experiment["management"]
    generated: list[Path] = []

    for gcm in gcms:
        source_calendar = Path(calendar_source(calendar, "historical", gcm))
        reference_gcm = calendar_reference_gcm(calendar, "historical", gcm)
        available_years = calendar_years(source_calendar)
        years = tuple(args.year) if args.year else available_years
        unknown_years = sorted(set(years) - set(available_years))
        if unknown_years:
            raise ValueError(
                f"Requested calibration year(s) are absent from {source_calendar}: {unknown_years}; "
                f"available={available_years[0]}..{available_years[-1]}"
            )
        if len(years) < 2 or len(set(years)) != len(years):
            raise ValueError("Use at least two distinct historical years for inverse calibration")
        weather_template = profile.get("weather_dir_templates", {}).get(
            "historical", profile.get("weather_dir_template", "{weather_root}/historical/{gcm}/{gcm}_hist_WTH")
        )
        weather_dir = Path(weather_template.format(
            weather_root=profile["weather_root"].rstrip("/"), scenario="historical", gcm=gcm
        ))
        if not weather_dir.is_dir():
            raise ValueError(f"Historical weather directory does not exist for {gcm}: {weather_dir}")
        validate_weather_years(args.sites, weather_dir, years)

        calendar_sites: dict[int, str] = {}
        for year in years:
            target = args.output / "calendar_sites" / safe_name(gcm) / f"calendar_y{year}.gpkg"
            if target.exists():
                raise FileExistsError(f"Refusing to overwrite ensemble calendar input: {target}")
            count = enrich_sites(args.sites, source_calendar, target, year, year)
            if count == 0:
                raise ValueError(f"No sites were sampled for {gcm} {year}")
            calendar_sites[year] = str(target.resolve()).replace("\\", "/")

        runs = []
        for year in years:
            sites = calendar_sites[year]
            for cultivar in candidates:
                gm = str(cultivar["gm"])
                safe_gm = safe_name(f"GM{gm}")
                runs.append({
                    "name": f"gm_{safe_gm}_y{year}",
                    "run_label": f"{safe_gm} {year}",
                    "gm": gm,
                    "ingeno": str(cultivar["ingeno"]),
                    "cname": str(cultivar["cname"]),
                    "dises": "N",
                    "planting_label": "GGCMI",
                    "startYear": year,
                    "sites": f"xy_from_vector::{sites}",
                    "wsta": f"lookup_wth::WTH0::vector::{sites}::WTH_CELLID" if "WTH_CELLID" in site_fields else f"lookup_wth::WTH0::vector::{sites}::CellID",
                    "pfrst": f"auto_planting_window_doy_shape::vector::{sites}::PLANT_DOY::-1::0",
                })
        label = f"maturity_ensemble_{safe_name(experiment['country'].lower())}_{safe_name(gcm.lower())}"
        if args.name_suffix:
            if not re.fullmatch(r"[A-Za-z0-9_-]+", args.name_suffix):
                raise ValueError("--name-suffix must contain only ASCII letters, digits, _ or -")
            label += f"_{args.name_suffix}"
        common = {
            "template": "SBG00001.SBX", "nyers": 1,
            "id_soil": f"lookup_ghr::raster::{profile['soil_raster']}",
            "ic_layers": "generate_ic_layers::$id_soil",
            "include": [profile["dssat_profile"], profile["dssat_simulation_cde"]],
            "harvestArea": f"raster::{harea}",
            "ph2ol": management["ph2ol"], "icin": management["icin"],
            "icsw%": management["icsw_percent"], "icrt": management["icrt"], "icres": management["icres"],
            "icren": management["icren"], "irrig": management["irrig"], "ramt": management["ramt"],
            # Pythia resolves expressions in dictionary insertion order.  Keep
            # the placeholder here (rather than only in each run) so that the
            # run-specific automatic PFRST is resolved before SDATE refers to it.
            "fen_tot": management["fen_tot"], "pfrst": "REPLACED_PER_RUN",
            "sdate": "date_offset::$pfrst::-10",
        }
        cfg = {
            "name": label,
            "workDir": f"{profile['work_root'].rstrip('/')}/{label}",
            "templateDir": str(template_root.resolve()).replace("\\", "/"),
            "weatherDir": str(weather_dir).replace("\\", "/"),
            "threads": experiment["threads"], "cores": experiment["cores"], "ghr_root": profile["ghr_root"],
            "default_setup": common,
            "dssat": {"executable": profile["dssat_executable"]},
            "pipeline": {
                "country": experiment["country"],
                "purpose": "inverse_maturity_ensemble_member",
                "gcm": gcm,
                "scenario": "historical",
                "calibration_years": list(years),
                "calibration_sites": base_sites,
                "calendar_source": str(source_calendar),
                "calendar_reference_gcm": reference_gcm,
                "calendar_sites": {str(year): path for year, path in calendar_sites.items()},
                "maturity_candidates": candidate_path,
                "candidate_method": method,
                "candidate_count": len(candidates),
                "case_count": len(candidates) * len(years),
                "calendar_available_years": list(available_years),
                "calendar_years_mode": "all_available" if not args.year else "explicit_smoke_test",
            },
            "runs": runs,
        }
        target = args.output / f"{label}.json"
        if target.exists():
            raise FileExistsError(f"Refusing to overwrite ensemble configuration: {target}")
        target.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        generated.append(target)
    manifest = args.output / "manifest.txt"
    if manifest.exists():
        raise FileExistsError(f"Refusing to overwrite ensemble manifest: {manifest}")
    manifest.write_text("\n".join(str(path.resolve()) for path in generated) + "\n", encoding="utf-8")
    print(
        f"Generated {len(generated)} GCM calibration members; each has "
        f"{len(candidates)} generic GMs x {len(years)} historical years. Manifest: {manifest}"
    )


if __name__ == "__main__":
    main()
