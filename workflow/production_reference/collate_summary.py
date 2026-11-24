#!/usr/bin/env python3
"""Create paired disease/control country CSVs for the calibrated DISMO input."""
from __future__ import annotations

import argparse
import csv
import gzip
import re
from pathlib import Path

SUMMARY_FIELDS = ("PDAT", "MDAT", "CWAM", "HWAM", "HWAH")
# DSSAT writes one Summary.OUT row for each sequential model run.  In a
# multi-year configuration the treatment number normally remains 1, whereas
# RUNNO distinguishes the simulated seasons.  Both are retained: TRTNO keeps
# DSSAT provenance and RUNNO is the key that aligns the paired disease/control
# results for a particular season.
COMMON_FIELDS = ["COUNTRY", "PERIOD", "CLIMATE_WINDOW", "SIM_YEAR", "SCENARIO", "GCM", "LATITUDE", "LONGITUDE", "CELL_ID", "WEATHER_SOURCE_CELL_ID", "WEATHER_BORROWED", "WEATHER_DISTANCE_KM", "HARVEST_AREA", "ZONE_ID", "INGENO", "CNAME", "GM_WIN", "GM_ERR_D", "SIM_MDOY", "GM_REFYR", "GM_METHOD", "GM_P90", "GM_MEA", "GM_MAT_D", "GM_GROW_D", "GM_FAIL", "GM_CASE", "GM_STAB", "GM_NEXT", "GM_MARG", "GM_GCM", "GM_YR0", "GM_YRN", "GM_OK", "GM_AMBIG", "GM_GSL", "GM_GSL_VALID", "GM_FALLBACK", "GM_LAT_ABS", "GM_RULE", "GM_GSL_YR0", "GM_GSL_YRN", "GM_GSL_N", "PHENO_ID", "GM_BASE", "PHENO_SC", "PHENO_ERR", "PHENO_P90", "PHENO_MEA", "PHENO_FAIL", "PHENO_CASE", "PHENO_STAB", "PHENO_NEXT", "PHENO_MARG", "PHENO_GCM", "PHENO_YR0", "PHENO_YRN", "PHENO_OK", "PHENO_EDGE", "PHENO_LHASH", "RUNNO", "TRTNO"]
CALENDAR_FIELDS = ["CAL_PLANT_DOY", "CAL_MAT_DOY", "CAL_GROW_DAYS", "CAL_YR0", "CAL_YRN", "CAL_N"]
DISEASE_MEAN_METRICS = ("SEV_FINAL",)
DISEASE_DETAIL_METRICS = ("SEV_FINAL", "DISEASE_ONSET_DAE")


def coordinates(directory: Path) -> tuple[float, float]:
    lat_text, lon_text = directory.parts[-2:]

    def parse(value: str) -> float:
        number = float(value[:-1].replace("_", "."))
        return -number if value[-1] in {"S", "W"} else number

    return parse(lat_text), parse(lon_text)


def summary_rows(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("@") and "HWAH" in line) + 1
    rows = []
    for line in lines[start:]:
        if not re.match(r"^\s*\d+\s", line):
            if rows:
                break
            continue
        values = line.split()
        try:
            wsta = next(i for i, value in enumerate(values) if value.upper().endswith(".WTH"))
            rows.append({"RUNNO": values[0], "TRTNO": values[1], "PDAT": values[wsta + 7], "MDAT": values[wsta + 10], "CWAM": values[wsta + 14], "HWAM": values[wsta + 15], "HWAH": values[wsta + 16]})
        except (IndexError, StopIteration) as exc:
            raise ValueError(f"Cannot parse Summary.OUT row in {path}: {line}") from exc
    if not rows:
        raise ValueError(f"No summary rows found in {path}")
    return rows


def final_dismo_metrics(path: Path) -> dict[str, str]:
    """Extract NSPRAYS, final severity and first positive-severity DAE.

    The annual execution design guarantees exactly one crop cycle in this file.
    Reading the final record therefore yields the requested number of fungicide
    applications for that pixel-year.  A multi-year DISMO.OUT is intentionally
    rejected by ``main`` because it retains only one detailed cycle.
    """
    if not path.is_file():
        raise ValueError(f"Missing DISMO.OUT: {path}")
    nsp_index = sev_index = dae_index = None
    last_nsprays = last_sev = None
    disease_onset_dae = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.lstrip().upper().startswith("@YEAR"):
            columns = line.split()
            if "NSPRAYS" in columns and "SEV%" in columns and "DAE" in columns:
                nsp_index = columns.index("NSPRAYS")
                sev_index = columns.index("SEV%")
                dae_index = columns.index("DAE")
            continue
        if nsp_index is None or not re.match(r"^\s*\d{4}\s+\d+", line):
            continue
        fields = line.split()
        if len(fields) <= max(nsp_index, sev_index, dae_index):
            continue
        try:
            last_nsprays = int(float(fields[nsp_index]))
            last_sev = float(fields[sev_index])
            dae = int(float(fields[dae_index]))
        except ValueError:
            continue
        # This deliberately uses the printed DISMO severity.  It describes the
        # first model day with detectable disease, not an imposed DAE_START.
        if disease_onset_dae is None and dae >= 0 and last_sev > 0.0:
            disease_onset_dae = dae
    if last_nsprays is None or last_sev is None:
        raise ValueError(f"Could not extract NSPRAYS, SEV%, and DAE from {path}")
    return {
        "NSPRAYS": str(last_nsprays),
        "SEV_FINAL": f"{last_sev:.6g}",
        "DISEASE_ONSET_DAE": "" if disease_onset_dae is None else str(disease_onset_dae),
    }


def site_attributes(paths: list[Path] | None) -> dict[tuple[float, float], dict[str, str]]:
    if not paths:
        return {}
    import fiona

    result = {}
    for path in paths:
        with fiona.open(path) as source:
            for feature in source:
                lon, lat = feature["geometry"]["coordinates"]
                prop = dict(feature["properties"])
                key = (round(lat, 4), round(lon, 4))
                attrs = {
                    "CELL_ID": str(prop.get("CellID", "")),
                    "WEATHER_SOURCE_CELL_ID": str(prop.get("WTH_CELLID") or prop.get("CellID", "")),
                    "WEATHER_BORROWED": str(prop.get("WTH_BORROWED") or 0),
                    "WEATHER_DISTANCE_KM": str(prop.get("WTH_DIST_KM") or 0),
                    "HARVEST_AREA": str(prop.get("HAREA", "")),
                    "ZONE_ID": str(prop.get("ZONE_ID", "")), "INGENO": str(prop.get("INGENO", "")),
                    "CNAME": str(prop.get("CNAME", "")), "GM_WIN": str(prop.get("GM_WIN", "")),
                    "GM_ERR_D": str(prop.get("GM_ERR_D", "")), "SIM_MDOY": str(prop.get("SIM_MDOY", "")),
                    "GM_REFYR": str(prop.get("GM_REFYR", "")), "GM_METHOD": str(prop.get("GM_METHOD", "")),
                    "GM_P90": str(prop.get("GM_P90", "")), "GM_MEA": str(prop.get("GM_MEA", "")),
                    "GM_MAT_D": str(prop.get("GM_MAT_D", "")), "GM_GROW_D": str(prop.get("GM_GROW_D", "")), "GM_FAIL": str(prop.get("GM_FAIL", "")),
                    "GM_CASE": str(prop.get("GM_CASE", "")), "GM_STAB": str(prop.get("GM_STAB", "")),
                    "GM_NEXT": str(prop.get("GM_NEXT", "")), "GM_MARG": str(prop.get("GM_MARG", "")),
                    "GM_GCM": str(prop.get("GM_GCM", "")), "GM_YR0": str(prop.get("GM_YR0", "")),
                    "GM_YRN": str(prop.get("GM_YRN", "")), "GM_OK": str(prop.get("GM_OK", "")),
                    "GM_AMBIG": str(prop.get("GM_AMBIG", "")),
                    "GM_GSL": str(prop.get("GM_GSL", "")), "GM_GSL_VALID": str(prop.get("GM_GSL_VALID", "")),
                    "GM_FALLBACK": str(prop.get("GM_FALLBACK", "")), "GM_LAT_ABS": str(prop.get("GM_LAT_ABS", "")),
                    "GM_RULE": str(prop.get("GM_RULE", "")), "GM_GSL_YR0": str(prop.get("GM_GSL_YR0", "")),
                    "GM_GSL_YRN": str(prop.get("GM_GSL_YRN", "")), "GM_GSL_N": str(prop.get("GM_GSL_N", "")),
                    "PHENO_ID": str(prop.get("PHENO_ID", "")), "GM_BASE": str(prop.get("GM_BASE", "")),
                    "PHENO_SC": str(prop.get("PHENO_SC", "")), "PHENO_ERR": str(prop.get("PHENO_ERR", "")),
                    "PHENO_P90": str(prop.get("PHENO_P90", "")), "PHENO_MEA": str(prop.get("PHENO_MEA", "")),
                    "PHENO_FAIL": str(prop.get("PHENO_FAIL", "")), "PHENO_CASE": str(prop.get("PHENO_CASE", "")),
                    "PHENO_STAB": str(prop.get("PHENO_STAB", "")), "PHENO_NEXT": str(prop.get("PHENO_NEXT", "")),
                    "PHENO_MARG": str(prop.get("PHENO_MARG", "")), "PHENO_GCM": str(prop.get("PHENO_GCM", "")),
                    "PHENO_YR0": str(prop.get("PHENO_YR0", "")), "PHENO_YRN": str(prop.get("PHENO_YRN", "")),
                    "PHENO_OK": str(prop.get("PHENO_OK", "")), "PHENO_EDGE": str(prop.get("PHENO_EDGE", "")),
                    "PHENO_LHASH": str(prop.get("PHENO_LHASH", "")),
                    "CAL_PLANT_DOY": str(prop.get("PLANT_DOY", "")),
                    "CAL_MAT_DOY": str(prop.get("MAT_DOY", "")),
                    "CAL_GROW_DAYS": str(prop.get("GROW_DAYS", "")),
                    "CAL_YR0": str(prop.get("CAL_YR0", "")),
                    "CAL_YRN": str(prop.get("CAL_YRN", "")),
                    "CAL_N": str(prop.get("CAL_N", "")),
                }
                if key in result and result[key] != attrs:
                    raise ValueError(f"Conflicting site attributes at {key} across --sites files")
                result[key] = attrs
    return result


def write_rows(path: Path, fields: list[str], rows: list[dict[str, str]], append: bool) -> None:
    exists = path.is_file() and path.stat().st_size > 0
    mode = "a" if append and exists else "w"
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, f"{mode}t", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        if mode == "w":
            writer.writeheader()
        writer.writerows(rows)


def mean(values: list[str]) -> str:
    if any(value == "" for value in values):
        return ""
    return f"{sum(float(value) for value in values) / len(values):.6g}"


def yield_loss_percent(control: str, disease: str) -> str:
    """Return yield loss relative to the paired no-disease simulation."""
    try:
        control_value, disease_value = float(control), float(disease)
    except (TypeError, ValueError):
        return ""
    if control_value <= 0 or disease_value < 0:
        return ""
    return f"{100.0 * (control_value - disease_value) / control_value:.6g}"


def add_yield_losses(
    disease_rows: list[dict[str, str]], control_rows: list[dict[str, str]], plantings: tuple[str, ...]
) -> None:
    """Attach explicit kg/ha yields and paired losses for every planting date."""
    controls = {tuple(row[field] for field in COMMON_FIELDS): row for row in control_rows}
    if len(controls) != len(control_rows):
        raise ValueError("Duplicate control rows after planting-date aggregation")
    for row in disease_rows:
        key = tuple(row[field] for field in COMMON_FIELDS)
        control = controls.get(key)
        if control is None:
            raise ValueError(f"Missing paired control result for disease row: {key}")
        # HWAH is DSSAT's harvested product yield (kg/ha).  Keep the native
        # fields for provenance, but also expose unambiguous aliases in the
        # disease table so the calibrated disease run and its control are paired.
        row["YIELD_KG_HA_control"] = control["HWAH"]
        row["YIELD_KG_HA_disease"] = row["HWAH_disease"]
        row["LOSS_PCT"] = yield_loss_percent(control["HWAH"], row["HWAH_disease"])
        for planting in plantings:
            if row[f"PDAT_{planting}"] != control[f"PDAT_{planting}"]:
                raise ValueError(f"Disease/control PDAT mismatch for {planting} at {key}")
            row[f"YIELD_KG_HA_control_{planting}"] = control[f"HWAH_{planting}"]
            row[f"YIELD_KG_HA_disease_{planting}"] = row[f"HWAH_disease_{planting}"]
            row[f"LOSS_PCT_{planting}"] = yield_loss_percent(
                control[f"HWAH_{planting}"], row[f"HWAH_disease_{planting}"]
            )


def aggregate_dates(
    rows: list[dict[str, str]], disease: bool, plantings: tuple[str, ...], planting_date_role: str
) -> list[dict[str, str]]:
    """Average productivity across the configured planting-date ensemble."""
    grouped: dict[tuple[str, ...], dict[str, dict[str, str]]] = {}
    for row in rows:
        key = tuple(row[field] for field in COMMON_FIELDS)
        planting = row["PLANTING_LABEL"]
        if planting in grouped.setdefault(key, {}):
            raise ValueError(f"Duplicate result for {planting} at {key}")
        grouped[key][planting] = row
    result = []
    for key, by_planting in grouped.items():
        missing = set(plantings) - set(by_planting)
        if missing:
            raise ValueError(f"Missing planting dates {sorted(missing)} at {key}")
        first = by_planting[plantings[0]]
        output = {field: first[field] for field in COMMON_FIELDS}
        output.update({"N_PLANTING_DATES": str(len(plantings)), "PLANTING_DATE_ROLE": planting_date_role})
        for planting in plantings:
            row = by_planting[planting]
            if disease:
                output[f"PDAT_{planting}"] = row["PDAT"]
                for variable in ("MDAT", "CWAM", "HWAM", "HWAH"):
                    output[f"{variable}_disease_{planting}"] = row[variable]
                for metric in DISEASE_DETAIL_METRICS:
                    output[f"{metric}_{planting}"] = row[metric]
                output[f"NSPRAYS_{planting}"] = row["NSPRAYS"]
            else:
                output[f"PDAT_{planting}"] = row["PDAT"]
                for variable in ("MDAT", "CWAM", "HWAM", "HWAH"):
                    output[f"{variable}_{planting}"] = row[variable]
        if disease:
            for variable in ("CWAM", "HWAM", "HWAH"):
                output[f"{variable}_disease"] = mean([by_planting[p][variable] for p in plantings])
            for metric in DISEASE_MEAN_METRICS:
                output[metric] = mean([by_planting[p][metric] for p in plantings])
            output["NSPRAYS"] = mean([by_planting[p]["NSPRAYS"] for p in plantings])
        else:
            for variable in ("CWAM", "HWAM", "HWAH"):
                output[variable] = mean([by_planting[p][variable] for p in plantings])
        result.append(output)
    return result


def record_key(planting: str, lat: float, lon: float, runno: str, trtno: str) -> tuple[str, float, float, str, str]:
    return planting, round(lat, 4), round(lon, 4), runno, trtno


def main() -> None:
    global COMMON_FIELDS
    parser = argparse.ArgumentParser()
    parser.add_argument("work_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--country", required=True)
    parser.add_argument("--period", default="")
    parser.add_argument("--climate-window", default="")
    parser.add_argument("--simulation-year", type=int)
    parser.add_argument("--scenario", default="")
    parser.add_argument("--gcm", default="")
    parser.add_argument("--planting-label", action="append")
    parser.add_argument("--planting-date-role", default="")
    parser.add_argument("--sites", type=Path, action="append", help="GeoPackage(s) providing site attributes")
    parser.add_argument("--include-calendar-fields", action="store_true", help="Append annual GGCMI fields to new output CSVs.")
    parser.add_argument("--append", action="store_true")
    parser.add_argument(
        "--gzip",
        action="store_true",
        help="Write .csv.gz files. Recommended for persistent global result shards.",
    )
    args = parser.parse_args()
    if args.include_calendar_fields:
        COMMON_FIELDS = [*COMMON_FIELDS, *CALENDAR_FIELDS]
    sites = site_attributes(args.sites)
    disease: dict[tuple[str, float, float, str, str], dict[str, str]] = {}
    control: list[dict[str, str]] = []
    for directory in sorted(path for path in args.work_dir.iterdir() if path.is_dir()):
        if directory.name.startswith("control_"):
            match = re.fullmatch(r"control_(P\d+)(?:_.+)?", directory.name)
            if not match:
                continue
            arm, planting_label = "control", match.group(1)
        else:
            match = re.fullmatch(r"disease_(P\d+)(?:_.+)?", directory.name)
            if not match:
                continue
            arm, planting_label = "disease", match.group(1)
        for summary in sorted(directory.glob("**/Summary.OUT")):
            results = summary_rows(summary)
            metrics = final_dismo_metrics(summary.parent / "DISMO.OUT") if arm == "disease" else {}
            if arm == "disease" and len(results) != 1:
                raise ValueError(
                    f"{summary} has {len(results)} DSSAT cycles. Annual NSPRAYS requires one independent "
                    "NYERS=1 configuration per planting year."
                )
            lat, lon = coordinates(summary.parent)
            attrs = sites.get((round(lat, 4), round(lon, 4)), {
                "CELL_ID": "", "WEATHER_SOURCE_CELL_ID": "", "WEATHER_BORROWED": "", "WEATHER_DISTANCE_KM": "", "HARVEST_AREA": "", "ZONE_ID": "", "INGENO": "", "CNAME": "",
                "GM_WIN": "", "GM_ERR_D": "", "SIM_MDOY": "", "GM_REFYR": "", "GM_METHOD": "",
                "GM_P90": "", "GM_MEA": "", "GM_MAT_D": "", "GM_GROW_D": "", "GM_FAIL": "", "GM_CASE": "", "GM_STAB": "",
                "GM_NEXT": "", "GM_MARG": "", "GM_GCM": "", "GM_YR0": "", "GM_YRN": "", "GM_OK": "",
                "GM_AMBIG": "",
                "GM_GSL": "", "GM_GSL_VALID": "", "GM_FALLBACK": "", "GM_LAT_ABS": "", "GM_RULE": "",
                "GM_GSL_YR0": "", "GM_GSL_YRN": "", "GM_GSL_N": "",
                "PHENO_ID": "", "GM_BASE": "", "PHENO_SC": "", "PHENO_ERR": "", "PHENO_P90": "", "PHENO_MEA": "",
                "PHENO_FAIL": "", "PHENO_CASE": "", "PHENO_STAB": "", "PHENO_NEXT": "", "PHENO_MARG": "",
                "PHENO_GCM": "", "PHENO_YR0": "", "PHENO_YRN": "", "PHENO_OK": "", "PHENO_EDGE": "", "PHENO_LHASH": "",
                "CAL_PLANT_DOY": "", "CAL_MAT_DOY": "", "CAL_GROW_DAYS": "", "CAL_YR0": "", "CAL_YRN": "", "CAL_N": "",
            })
            for result in results:
                base = {
                    "COUNTRY": args.country, "PERIOD": args.period,
                    "CLIMATE_WINDOW": args.climate_window,
                    "SIM_YEAR": "" if args.simulation_year is None else str(args.simulation_year),
                    "SCENARIO": args.scenario, "GCM": args.gcm,
                    "PLANTING_LABEL": planting_label, "LATITUDE": f"{lat:.4f}", "LONGITUDE": f"{lon:.4f}",
                    **attrs, "RUNNO": result["RUNNO"], "TRTNO": result["TRTNO"],
                }
                if arm == "control":
                    control.append({**base, **{field: result[field] for field in SUMMARY_FIELDS}})
                    continue
                key = record_key(planting_label, lat, lon, result["RUNNO"], result["TRTNO"])
                if key in disease:
                    raise ValueError(f"Duplicate disease result: {summary}")
                disease[key] = {
                    **base,
                    **{field: result[field] for field in SUMMARY_FIELDS},
                    "SEV_FINAL": metrics["SEV_FINAL"],
                    "NSPRAYS": metrics["NSPRAYS"],
                    "DISEASE_ONSET_DAE": metrics["DISEASE_ONSET_DAE"],
                }
    if not disease or not control:
        raise SystemExit(f"Missing disease or control Summary.OUT below {args.work_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    safe_country = re.sub(r"[^A-Za-z0-9_-]+", "_", args.country).strip("_")
    observed_plantings = {row["PLANTING_LABEL"] for row in [*disease.values(), *control]}
    plantings = tuple(args.planting_label or sorted(observed_plantings))
    if not plantings:
        raise SystemExit("No planting-date labels found")
    missing_plantings = set(plantings) - observed_plantings
    unexpected_plantings = observed_plantings - set(plantings)
    if missing_plantings or unexpected_plantings:
        raise SystemExit(f"Planting labels disagree with configuration; missing={sorted(missing_plantings)}, unexpected={sorted(unexpected_plantings)}")
    planting_date_role = args.planting_date_role or ";".join(plantings)
    disease_rows = aggregate_dates(list(disease.values()), disease=True, plantings=plantings, planting_date_role=planting_date_role)
    control_rows = aggregate_dates(control, disease=False, plantings=plantings, planting_date_role=planting_date_role)
    add_yield_losses(disease_rows, control_rows, plantings)
    date_fields = [f"PDAT_{planting}" for planting in plantings]
    yield_mean_fields = ["YIELD_KG_HA_control", "YIELD_KG_HA_disease"]
    yield_detail_fields = [
        field
        for planting in plantings
        for field in (
            f"YIELD_KG_HA_control_{planting}",
            f"YIELD_KG_HA_disease_{planting}",
        )
    ]
    disease_mean_fields = ["CWAM_disease", "HWAM_disease", "HWAH_disease", "SEV_FINAL", "NSPRAYS"]
    loss_mean_fields = ["LOSS_PCT"]
    disease_detail_fields = [
        field
        for planting in plantings
        for field in (
            f"MDAT_disease_{planting}", f"CWAM_disease_{planting}",
            f"HWAM_disease_{planting}", f"HWAH_disease_{planting}",
            f"SEV_FINAL_{planting}", f"NSPRAYS_{planting}",
            f"DISEASE_ONSET_DAE_{planting}",
        )
    ]
    loss_detail_fields = [f"LOSS_PCT_{planting}" for planting in plantings]
    control_mean_fields = ["CWAM", "HWAM", "HWAH"]
    control_detail_fields = [f"{variable}_{planting}" for planting in plantings for variable in ("MDAT", "CWAM", "HWAM", "HWAH")]
    disease_fields = COMMON_FIELDS + ["N_PLANTING_DATES", "PLANTING_DATE_ROLE"] + date_fields + yield_mean_fields + disease_mean_fields + loss_mean_fields + yield_detail_fields + disease_detail_fields + loss_detail_fields
    control_fields = COMMON_FIELDS + ["N_PLANTING_DATES", "PLANTING_DATE_ROLE"] + date_fields + control_mean_fields + control_detail_fields
    suffix = ".csv.gz" if args.gzip else ".csv"
    write_rows(args.output_dir / f"summary_disease_{safe_country}{suffix}", disease_fields, disease_rows, args.append)
    write_rows(args.output_dir / f"summary_control_{safe_country}{suffix}", control_fields, control_rows, args.append)
    print(f"Wrote {len(disease_rows)} date-averaged disease rows and {len(control_rows)} date-averaged control rows to {args.output_dir}")


if __name__ == "__main__":
    main()
