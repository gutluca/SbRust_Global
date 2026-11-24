#!/usr/bin/env python3
"""Generate one Pythia JSON per climate scenario × GCM for both DISMO states."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path

import fiona

from prepare_disease_input import prepare_disease_input
from gm_production_qc import policy_from_sites, validate_gm


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def required(value: str, name: str) -> str:
    if not value or value.startswith("REPLACE_WITH"):
        raise ValueError(f"Set profiles.<profile>.{name} in the project configuration")
    return value


def safe_country_label(value: str) -> str:
    """Use a filesystem-safe config name without changing country metadata."""
    label = re.sub(r"[^A-Za-z0-9_-]+", "_", value.lower()).strip("_")
    if not label:
        raise ValueError(f"Country has no usable ASCII configuration label: {value!r}")
    return label


def calendar_source(calendar: dict, scenario: str, gcm: str) -> str:
    """Return the calendar source for a weather scenario and GCM.

    ``annual_matched`` keeps one GGCMI file per GCM x scenario.  The current
    project design instead uses ``annual_scenario_reference``: one explicitly
    declared, scenario-specific calendar supplies the five management dates to
    every GCM, while climate uncertainty is represented exclusively by the WTH
    ensemble.  Both modes remain explicit and auditable.
    """
    mode = str(calendar.get("mode", "static"))
    if mode == "static":
        return required(str(calendar.get("source", "")), "experiment.calendar.source")
    if mode not in {"annual_matched", "annual_scenario_reference"}:
        raise ValueError(
            "experiment.calendar.mode must be 'static', 'annual_matched', "
            "or 'annual_scenario_reference'"
        )
    sources = calendar.get("sources")
    if not isinstance(sources, dict):
        raise ValueError(f"{mode} calendar requires experiment.calendar.sources")
    by_scenario = sources.get(scenario)
    if mode == "annual_scenario_reference":
        source = by_scenario if isinstance(by_scenario, str) else ""
        if not source:
            raise ValueError(
                f"No scenario-reference GGCMI calendar for scenario={scenario!r}. "
                "Register one explicit calendar path per scenario."
            )
        return source
    if not isinstance(by_scenario, dict):
        raise ValueError(
            f"No GGCMI calendar mapping for scenario={scenario!r}. "
            "Add the matching GCM calendar before generating this production configuration."
        )
    source = by_scenario.get(gcm)
    if not source:
        raise ValueError(
            f"No GGCMI calendar mapping for scenario={scenario!r}, gcm={gcm!r}. "
            "Do not borrow another GCM calendar; download and register the matching file."
        )
    return str(source)


def calendar_reference_gcm(calendar: dict, scenario: str, weather_gcm: str) -> str:
    """Return the GGCMI calendar provenance label for a configuration."""
    if str(calendar.get("mode", "static")) == "annual_scenario_reference":
        references = calendar.get("reference_gcms")
        if not isinstance(references, dict) or not str(references.get(scenario) or ""):
            raise ValueError(
                "annual_scenario_reference requires calendar.reference_gcms "
                "to identify the provenance of every scenario calendar"
            )
        return str(references[scenario])
    return weather_gcm


def task_calendar_metadata(calendar: dict, scenario: str, weather_gcm: str, year: int) -> dict[str, object]:
    """Describe the calendar that supplies planting dates for one annual task.

    Future SSPs use their annual scenario-reference files.  Historical WTH
    years predate the available GGCMI file, so baseline management uses one
    *fixed, pixel-specific* historical climatology.  It is not an annual
    calendar extrapolation and it never changes the previously calibrated GM.
    """
    historical = calendar.get("historical_baseline")
    if scenario == "historical" and isinstance(historical, dict):
        if str(historical.get("mode") or "") != "climatological_reference":
            raise ValueError(
                "calendar.historical_baseline.mode must be 'climatological_reference'"
            )
        source = required(
            str(historical.get("source") or ""),
            "experiment.calendar.historical_baseline.source",
        )
        first = int(historical.get("start_year"))
        last = int(historical.get("end_year"))
        if last < first:
            raise ValueError("historical_baseline.end_year must be >= start_year")
        stat = str(historical.get("stat") or "median")
        if stat not in {"median", "mean"}:
            raise ValueError("historical_baseline.stat must be 'median' or 'mean'")
        reference_gcm = required(
            str(historical.get("reference_gcm") or ""),
            "experiment.calendar.historical_baseline.reference_gcm",
        )
        return {
            "mode": "climatological_reference",
            "source": source,
            "scenario": scenario,
            "reference_gcm": reference_gcm,
            "weather_gcm": weather_gcm,
            "year": year,
            "source_start_year": first,
            "source_end_year": last,
            "stat": stat,
        }
    return {
        "mode": str(calendar.get("mode", "static")),
        "source": calendar_source(calendar, scenario, weather_gcm),
        "scenario": scenario,
        "reference_gcm": calendar_reference_gcm(calendar, scenario, weather_gcm),
        "weather_gcm": weather_gcm,
        "year": year,
    }


def make_planting_dates(management: dict) -> list[dict[str, str]]:
    """Return explicit DSSAT treatments for planting-date uncertainty."""
    if management.get("planting_dates"):
        return management["planting_dates"]
    ensemble = management.get("planting_ensemble")
    if not ensemble:
        return [{"label": "P01", "pdate": management["pfrst"], "sdate": management["sdate"]}]
    first = dt.date.fromisoformat(ensemble["first_date"])
    last = dt.date.fromisoformat(ensemble["last_date"])
    step = int(ensemble["step_days"])
    if step < 1 or last < first:
        raise ValueError("planting_ensemble needs first_date <= last_date and step_days >= 1")
    dates, current = [], first
    while current <= last:
        dates.append({"label": f"P{len(dates) + 1:02d}", "pdate": current.isoformat(), "sdate": (current - dt.timedelta(days=10)).isoformat()})
        current += dt.timedelta(days=step)
    return dates


def simulation_specs(
    experiment: dict,
    selected_periods: list[str] | None,
    selected_scenarios: list[str] | None,
    selected_years: list[int] | None,
) -> list[dict[str, int | str | bool]]:
    """Return one specification per scenario, period and planting year.

    ``experiment.periods`` activates annual mode.  Each annual configuration
    uses NYERS=1, which is necessary because DISMO writes detailed diagnostics
    (including final NSPRAYS) per DSSAT invocation rather than per NYERS cycle.
    Projects without periods retain their previous multi-year behavior.
    """
    periods = experiment.get("periods")
    if not periods:
        if selected_periods or selected_years:
            raise ValueError("--period and --year require experiment.periods in the project configuration")
        return [
            {
                "period": "",
                "scenario": scenario,
                "start_year": int(experiment["start_year"]),
                "nyers": int(experiment["nyers"]),
                "annual": False,
            }
            for scenario in (selected_scenarios or experiment["scenarios"])
        ]

    known_periods = {str(item["name"]): item for item in periods}
    unknown = set(selected_periods or []) - set(known_periods)
    if unknown:
        raise ValueError(f"Unknown period(s): {sorted(unknown)}")
    period_names = selected_periods or list(known_periods)
    allowed_scenarios = set(selected_scenarios or experiment["scenarios"])
    specs: list[dict[str, int | str | bool]] = []
    for period_name in period_names:
        period = known_periods[period_name]
        first, last = int(period["start_year"]), int(period["end_year"])
        if last < first:
            raise ValueError(f"Period {period_name} has end_year before start_year")
        scenarios = [str(value) for value in period.get("scenarios", [period.get("scenario")]) if value]
        if not scenarios:
            raise ValueError(f"Period {period_name} must define scenario or scenarios")
        for scenario in scenarios:
            if scenario not in experiment["scenarios"]:
                raise ValueError(f"Period {period_name} references unknown scenario: {scenario}")
            if scenario not in allowed_scenarios:
                continue
            for year in range(first, last + 1):
                if selected_years and year not in selected_years:
                    continue
                specs.append({
                    "period": period_name,
                    "climate_window": f"{period_name}_{first}_{last}",
                    "scenario": scenario,
                    "start_year": year,
                    "nyers": 1,
                    "annual": True,
                })
    if not specs:
        raise ValueError("No simulation configurations match the requested period, scenario and year filters")
    return specs


def zoned_site_files(
    sites: Path, output_dir: Path, require_calendar: bool, require_generic_gm_qc: bool,
    gm_production_qc: dict | None = None,
) -> list[dict[str, str]]:
    """Split a zone-enriched point file so each Pythia run has one cultivar."""
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    with fiona.open(sites) as source:
        schema, crs = source.schema.copy(), source.crs
        required_fields = {"ZONE_ID", "INGENO", "CNAME"}
        missing = required_fields - set(schema["properties"])
        phenology_cohorts = "PHENO_ID" in schema["properties"]
        if phenology_cohorts:
            missing |= {"PHENO_OK", "PHENO_SC", "GM_BASE", "PHENO_LHASH"} - set(schema["properties"])
        if require_generic_gm_qc:
            missing |= {"GM_OK", "GM_AMBIG", "GM_CASE", "GM_FAIL", "GM_METHOD"} - set(schema["properties"])
        if require_calendar:
            missing |= {"PLANT_DOY", "GROW_DAYS"} - set(schema["properties"])
        if missing:
            raise ValueError(
                f"Zoned sites are missing fields: {sorted(missing)}. "
                "Run apply_zonation.py or select_maturity_group.py first."
            )
        for feature in source:
            props = dict(feature["properties"])
            if phenology_cohorts and int(props.get("PHENO_OK") or 0) != 1:
                raise ValueError(
                    "Phenology-cohort sites include a pixel that failed the calibration QC; "
                    "inspect PHENO_ERR/PHENO_FAIL before generating production configurations."
                )
            if require_generic_gm_qc:
                validate_gm(props, gm_production_qc)
            key = (str(props.get("ZONE_ID") or ""), str(props.get("INGENO") or ""), str(props.get("CNAME") or ""))
            if not all(key):
                raise ValueError("Every zoned site needs non-empty ZONE_ID, INGENO and CNAME")
            grouped.setdefault(key, []).append(feature)
    if not grouped:
        raise ValueError("Zoned sites file has no points")
    output_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for zone_id, ingeno, cname in sorted(grouped):
        safe_zone = re.sub(r"[^A-Za-z0-9_-]+", "_", zone_id).strip("_") or "ZONE"
        target = output_dir / f"{safe_zone}.gpkg"
        # zone_sites is generated data.  Rebuild it each time so a changed GM
        # selection or an updated point set cannot silently reuse stale zones.
        if target.exists():
            target.unlink()
        with fiona.open(target, "w", driver="GPKG", schema=schema, crs=crs, layer="sites") as sink:
            for feature in grouped[(zone_id, ingeno, cname)]:
                sink.write(feature)
        result.append({"zone_id": zone_id, "safe_zone": safe_zone, "ingeno": ingeno, "cname": cname, "sites": str(target.resolve()).replace("\\", "/")})
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("--profile", choices=["local", "hipergator"], required=True)
    parser.add_argument("--sites", type=Path, required=True)
    parser.add_argument("--harvest-area", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scenario", action="append")
    parser.add_argument("--gcm", action="append")
    parser.add_argument(
        "--allow-single-gcm-test",
        action="store_true",
        help=(
            "Permit an intentionally one-GCM technical test. Production campaigns "
            "must omit --gcm (all configured GCMs) and therefore build the MME."
        ),
    )
    parser.add_argument("--period", action="append", help="Named period from experiment.periods; repeat to select more than one.")
    parser.add_argument("--year", type=int, action="append", help="Planting year within selected periods; repeat to select more than one.")
    parser.add_argument(
        "--cultivar-library", type=Path,
        help="Generated SBGRO048.CUL required when --sites contains PHENO_ID phenology cohorts.",
    )
    args = parser.parse_args()
    project = json.loads(args.project.read_text(encoding="utf-8"))
    profile = project["profiles"][args.profile]
    experiment = project["experiment"]
    if "inoculum_scenarios" in experiment:
        raise ValueError(
            "experiment.inoculum_scenarios is obsolete. The current pipeline uses "
            "one immutable calibrated disease_parameters.txt input."
        )
    for key in ("dssat_executable", "dssat_profile", "dssat_simulation_cde", "disease_parameters", "ghr_root", "soil_raster", "weather_root", "work_root"):
        required(profile[key], key)
    configured_gcms = [str(value) for value in experiment["gcms"]]
    gcms = [str(value) for value in (args.gcm or configured_gcms)]
    if len(gcms) != len(set(gcms)):
        raise ValueError("Each requested GCM may appear only once")
    unknown_gcms = set(gcms) - set(configured_gcms)
    if unknown_gcms:
        raise ValueError(f"Requested GCM(s) are not in experiment.gcms: {sorted(unknown_gcms)}")
    if len(gcms) == 1 and args.gcm and not args.allow_single_gcm_test:
        raise ValueError(
            "A one-GCM run is restricted to an explicit technical test. "
            "Use --allow-single-gcm-test together with --gcm, or omit --gcm "
            "to generate the full multi-model campaign."
        )
    if 1 < len(gcms) < len(configured_gcms):
        raise ValueError(
            "Partial GCM subsets are not supported: request all configured GCMs "
            "for production, or one GCM with --allow-single-gcm-test."
        )
    specs = simulation_specs(experiment, args.period, args.scenario, args.year)
    cultivar_source = experiment.get("cultivar_source", "zonation_rules")
    if cultivar_source not in {"zonation_rules", "inverse_calibration"}:
        raise ValueError("experiment.cultivar_source must be 'zonation_rules' or 'inverse_calibration'")
    if not args.sites.is_file():
        raise ValueError(f"Sites GeoPackage does not exist: {args.sites}")
    with fiona.open(args.sites) as source:
        phenology_cohorts = "PHENO_ID" in source.schema["properties"]
        library_hashes = {str(dict(feature["properties"]).get("PHENO_LHASH") or "") for feature in source} if phenology_cohorts else set()
        site_fields = set(source.schema["properties"])
    if cultivar_source == "inverse_calibration" and phenology_cohorts:
        raise ValueError("Production GM assignment accepts only discrete generic-GM sites, not scaled phenology cohorts")
    generic_gm_calibration = cultivar_source == "inverse_calibration"
    gm_production_qc = policy_from_sites(args.sites) if generic_gm_calibration else None
    if generic_gm_calibration:
        missing_gm_qc = {"GM_OK", "GM_AMBIG", "GM_CASE", "GM_FAIL", "GM_METHOD"} - site_fields
        if missing_gm_qc:
            raise ValueError(
                "Inverse-calibration sites are missing generic-GM QC fields: "
                f"{sorted(missing_gm_qc)}"
            )
    cultivar_library = None
    if phenology_cohorts:
        if args.cultivar_library is None or not args.cultivar_library.is_file() or args.cultivar_library.name != "SBGRO048.CUL":
            raise ValueError(
                "Phenology-cohort sites require --cultivar-library pointing to the generated SBGRO048.CUL"
            )
        cultivar_library = str(args.cultivar_library.resolve()).replace("\\", "/")
        if len(library_hashes) != 1 or len(next(iter(library_hashes))) != 64:
            raise ValueError("Phenology-cohort sites have no single valid PHENO_LHASH")
        if sha256(args.cultivar_library) != next(iter(library_hashes)):
            raise ValueError("--cultivar-library SHA-256 does not match PHENO_LHASH in the selected sites")
    elif args.cultivar_library is not None:
        raise ValueError("--cultivar-library is only valid with PHENO_ID phenology-cohort sites")
    zonation_rules = None
    if cultivar_source == "zonation_rules":
        zonation_rules = Path(required(experiment.get("zonation", {}).get("rules_file", ""), "experiment.zonation.rules_file"))
        if not zonation_rules.is_file():
            raise ValueError(f"Country zonation rules not found: {zonation_rules}")
    args.output.mkdir(parents=True, exist_ok=True)
    script_dir = Path(__file__).resolve().parent
    template_root = script_dir / "templates"
    if not template_root.is_dir():
        template_root = script_dir.parent / "templates"
    template_dir = str(template_root.resolve()).replace("\\", "/")
    management = experiment["management"]
    calendar = experiment.get("calendar", {})
    calendar_driven = bool(calendar.get("enabled", False))
    calendar_mode = str(calendar.get("mode", "static"))
    annual_calendar = calendar_driven and calendar_mode in {"annual_matched", "annual_scenario_reference"}
    if calendar_driven and calendar_mode not in {"static", "annual_matched", "annual_scenario_reference"}:
        raise ValueError(
            "experiment.calendar.mode must be 'static', 'annual_matched', "
            "or 'annual_scenario_reference'"
        )
    if annual_calendar and any(not bool(spec["annual"]) for spec in specs):
        raise ValueError("Annual GGCMI calendars require annual NYERS=1 experiment.periods configurations")
    if calendar_driven:
        offsets = calendar.get("planting_offsets_days", [-30, -15, 0, 15, 30])
        if len(offsets) != 5:
            raise ValueError("calendar.planting_offsets_days must contain exactly five offsets")
        planting_dates = [{"label": f"P{i + 1:02d}", "offset": int(offset)} for i, offset in enumerate(offsets)]
    else:
        planting_dates = make_planting_dates(management)
    if calendar_driven:
        planting_date_role = ";".join(
            f"{item['label']}=GGCMI{int(item['offset']):+d}d" for item in planting_dates
        )
    else:
        planting_date_role = ";".join(item["label"] for item in planting_dates)
    disease_source = Path(profile["disease_parameters"])
    disease_input = prepare_disease_input(
        disease_source,
        args.output / "disease_parameters",
    )
    disease_input_text = str(disease_input).replace("\\", "/")
    disease_input_sha256 = sha256(disease_input)
    # In either annual calendar mode the run-local calendar is sampled in Slurm
    # scratch just before Pythia runs.  The durable calibrated sites therefore
    # only need the fixed GM attributes; PLANT_DOY/GROW_DAYS are not read here.
    zones = zoned_site_files(
        args.sites, args.output / "zone_sites", calendar_driven and not annual_calendar,
        generic_gm_calibration,
        gm_production_qc,
    )
    with fiona.open(args.sites) as source:
        weather_id_field = "WTH_CELLID" if "WTH_CELLID" in source.schema["properties"] else "CellID"
    harvest_area = str(args.harvest_area.resolve()).replace("\\", "/")
    generated = []
    for spec in specs:
        scenario = str(spec["scenario"])
        period = str(spec["period"])
        climate_window = str(spec.get("climate_window", ""))
        start_year = int(spec["start_year"])
        nyers = int(spec["nyers"])
        annual = bool(spec["annual"])
        for gcm in gcms:
            task_calendar = (
                task_calendar_metadata(calendar, scenario, gcm, start_year)
                if calendar_driven else {"mode": "disabled"}
            )
            weather_templates = profile.get("weather_dir_templates", {})
            weather_template = weather_templates.get(
                scenario,
                profile.get("weather_dir_template", "{weather_root}/{scenario}/{gcm}/{gcm}_WTH"),
            )
            weather_dir = weather_template.format(
                weather_root=profile["weather_root"].rstrip("/"), scenario=scenario, gcm=gcm
            )
            label_parts = ["soybean", safe_country_label(experiment["country"])]
            if period:
                label_parts.append(period)
            label_parts.extend([scenario, gcm])
            if annual:
                label_parts.append(f"y{start_year}")
            label = "_".join(label_parts)
            work_dir = f"{profile['work_root'].rstrip('/')}/{label}"
            shared_includes = [profile["dssat_profile"], profile["dssat_simulation_cde"]]
            if cultivar_library:
                shared_includes.append(cultivar_library)
            common = {
                "template": "SBG00001.SBX", "sites": "REPLACED_PER_ZONE",
                "nyers": nyers, "startYear": start_year,
                "ingeno": "REPLACED_PER_ZONE", "cname": "REPLACED_PER_ZONE",
                "ph2ol": management["ph2ol"], "icin": management["icin"], "icsw%": management["icsw_percent"],
                "icrt": management["icrt"], "icres": management["icres"], "icren": management["icren"],
                "irrig": management["irrig"], "ramt": management["ramt"], "fen_tot": management["fen_tot"],
                "id_soil": f"lookup_ghr::raster::{profile['soil_raster']}",
                "wsta": "REPLACED_PER_ZONE",
                "ic_layers": "generate_ic_layers::$id_soil",
                "harvestArea": f"raster::{harvest_area}",
                "include": [*shared_includes, disease_input_text]
            }
            if calendar_driven:
                # pfrst is resolved first; sdate then becomes ten days before that cell's planting day.
                common.update({"pfrst": "REPLACED_PER_RUN", "sdate": "date_offset::$pfrst::-10"})
            else:
                common.update({"sdate": management["sdate"], "pfrst": management["pfrst"], "plast": management["plast"]})
            runs = []
            for zone in zones:
                zone_common = {"sites": f"xy_from_vector::{zone['sites']}", "wsta": f"lookup_wth::WTH0::vector::{zone['sites']}::{weather_id_field}", "ingeno": zone["ingeno"], "cname": zone["cname"], "zone_id": zone["zone_id"]}
                for planting in planting_dates:
                    runs.append({
                        **zone_common,
                        "name": f"disease_{planting['label']}_{zone['safe_zone']}",
                        "run_label": f"SBR disease {planting['label']}",
                        "planting_label": planting["label"],
                        "dises": "Y",
                    })
                    if calendar_driven:
                        runs[-1]["pfrst"] = f"auto_planting_window_doy_shape::vector::{zone['sites']}::PLANT_DOY::{int(planting['offset']) - 1}::0"
                    else:
                        runs[-1].update({"sdate": planting["sdate"], "pdate": planting["pdate"], "pfrst": planting["pdate"], "plast": planting["pdate"]})
                    runs.append({
                        **zone_common, "name": f"control_{planting['label']}_{zone['safe_zone']}", "run_label": f"No disease {planting['label']}",
                        "planting_label": planting["label"], "dises": "N",
                    })
                    if calendar_driven:
                        runs[-1]["pfrst"] = f"auto_planting_window_doy_shape::vector::{zone['sites']}::PLANT_DOY::{int(planting['offset']) - 1}::0"
                    else:
                        runs[-1].update({"sdate": planting["sdate"], "pdate": planting["pdate"], "pfrst": planting["pdate"], "plast": planting["pdate"]})
            config = {
                "schema_version": 2,
                "name": label, "workDir": work_dir, "templateDir": template_dir,
                "weatherDir": weather_dir, "threads": experiment["threads"], "cores": experiment["cores"],
                "ghr_root": profile["ghr_root"], "default_setup": common,
                "dssat": {"executable": profile["dssat_executable"]},
                "pipeline": {
                    "country": experiment["country"], "scenario": scenario, "gcm": gcm,
                    "period": period, "climate_window": climate_window,
                    "simulation_year": start_year if annual else None, "annual": annual,
                    "planting_labels": [item["label"] for item in planting_dates],
                    "planting_date_role": planting_date_role,
                    "cultivar_source": cultivar_source,
                    "phenology_cohorts": phenology_cohorts,
                    "generic_gm_calibration": generic_gm_calibration,
                    "gm_production_qc": gm_production_qc,
                    "cultivar_library": cultivar_library,
                    "cultivar_library_sha256": sha256(args.cultivar_library) if args.cultivar_library else None,
                    "zonation_rules": str(zonation_rules.resolve()).replace("\\", "/") if zonation_rules else None,
                    "calendar_driven": calendar_driven,
                    "calendar": task_calendar,
                    "disease_design": "single_calibrated_parameter_set",
                    "disease_parameters_source": str(disease_source.resolve()).replace("\\", "/"),
                    "disease_parameters_snapshot": disease_input_text,
                    "disease_parameters_sha256": disease_input_sha256,
                    "treatments_per_site": 2 * len(planting_dates),
                    "disease_treatments_per_site": len(planting_dates),
                    "expected_gcms": list(experiment["gcms"]),
                    "multimodel_ensemble": "equal_weight_after_within_gcm_planting_ensemble",
                    "country_summary_dir": f"{profile['work_root'].rstrip('/')}/country_summaries/{safe_country_label(experiment['country'])}"
                },
                "runs": runs
            }
            target = args.output / f"{label}.json"
            target.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
            generated.append(target)
    manifest = args.output / "manifest.txt"
    manifest.write_text("\n".join(str(path.resolve()) for path in generated) + "\n", encoding="utf-8")
    print(f"Generated {len(generated)} Pythia configurations and {manifest}")


if __name__ == "__main__":
    main()
