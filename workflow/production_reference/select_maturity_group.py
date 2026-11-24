#!/usr/bin/env python3
"""Select the generic cultivar whose DSSAT maturity best matches GGCMI."""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import re
from pathlib import Path

import fiona


def coordinates(directory: Path) -> tuple[float, float]:
    lat_text, lon_text = directory.parts[-2:]
    def parse(value: str) -> float:
        number = float(value[:-1].replace("_", "."))
        return -number if value[-1] in {"S", "W"} else number
    return parse(lat_text), parse(lon_text)


def summary_mdat(path: Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    header_index = next(i for i, line in enumerate(lines) if line.startswith("@") and "MDAT" in line)
    start = header_index + 1
    for line in lines[start:]:
        if re.match(r"^\s*\d+\s", line):
            values = line.split()
            # Model identifiers can contain whitespace, so Summary.OUT header
            # positions do not align with split data.  WSTA is stable; MDAT is
            # the tenth field after the weather filename, as in collate_summary.
            try:
                weather_index = next(i for i, value in enumerate(values) if value.upper().endswith(".WTH"))
                return values[weather_index + 10]
            except (IndexError, StopIteration) as exc:
                raise ValueError(f"Malformed Summary.OUT data row in {path}: {line}") from exc
    raise ValueError(f"No MDAT found in {path}")


def doy(value: str) -> int:
    number = int(value)
    return number % 1000 if number > 999 else number


def circular_error(simulated: int, target: int, period: int) -> int:
    if not (1 <= simulated <= period and 1 <= target <= period):
        raise ValueError(f"Day of year must be within 1..{period}: simulated={simulated}, target={target}")
    delta = abs(simulated - target)
    return min(delta, period - delta)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("work_dir", type=Path, help="workDir of generate_maturity_calibration.py")
    parser.add_argument("sites", type=Path, help="Calendar-enriched input GPKG")
    parser.add_argument("candidates", type=Path, help="Same candidate JSON passed to the generator")
    parser.add_argument("output_sites", type=Path, help="GPKG with winning cultivar per point")
    parser.add_argument("summary_csv", type=Path)
    parser.add_argument("--reference-year", type=int, required=True)
    args = parser.parse_args()
    days_in_reference_year = 366 if calendar.isleap(args.reference_year) else 365
    cultivar_by_gm = {str(item["gm"]): item for item in json.loads(args.candidates.read_text(encoding="utf-8"))["cultivars"]}
    targets, source_features = {}, []
    with fiona.open(args.sites) as source:
        schema, crs = source.schema.copy(), source.crs
        for feature in source:
            lon, lat = feature["geometry"]["coordinates"]
            props = dict(feature["properties"])
            target = int(props["MAT_DOY"])
            if not 1 <= target <= days_in_reference_year:
                raise ValueError(f"Invalid MAT_DOY at ({lat}, {lon}): {target}")
            targets[(round(lat, 4), round(lon, 4))] = target
            source_features.append(feature)
    candidates: dict[tuple[float, float], list[dict[str, str]]] = {}
    for run_dir in sorted(path for path in args.work_dir.iterdir() if path.is_dir()):
        match = re.fullmatch(r"gm_(.+)", run_dir.name)
        if not match:
            continue
        gm_label = match.group(1)
        gm = gm_label[len("GM"):] if gm_label.startswith("GM") else gm_label
        for summary in run_dir.glob("**/Summary.OUT"):
            lat, lon = coordinates(summary.parent)
            key, simulated = (round(lat, 4), round(lon, 4)), doy(summary_mdat(summary))
            target = targets[key]
            row = {"GM": gm, "SIM_MDOY": str(simulated), "TARGET_MDOY": str(target)}
            if 1 <= simulated <= days_in_reference_year:
                row.update({"ABS_ERROR_DAYS": str(circular_error(simulated, target, days_in_reference_year)), "STATUS": "valid"})
            else:
                # DSSAT uses -99 when a treatment did not reach physiological
                # maturity.  Retain this fact in diagnostics but never let it
                # compete as a plausible maturity date.
                row.update({"ABS_ERROR_DAYS": "", "STATUS": "invalid_mdat"})
            candidates.setdefault(key, []).append(row)
    expected_gms = set(cultivar_by_gm)
    incomplete_results, no_maturity = [], []
    winners = {}
    for key, options in candidates.items():
        observed_gms = {row["GM"] for row in options}
        missing_gms = expected_gms - observed_gms
        if missing_gms:
            incomplete_results.append(f"{key}: {', '.join(sorted(missing_gms))}")
            continue
        valid_options = [row for row in options if row["STATUS"] == "valid"]
        if not valid_options:
            no_maturity.append(str(key))
            continue
        winners[key] = min(valid_options, key=lambda row: (int(row["ABS_ERROR_DAYS"]), row["GM"]))
    if incomplete_results:
        raise SystemExit(
            "Incomplete candidate-GM results (first 10 sites): "
            + "; ".join(incomplete_results[:10])
        )
    if no_maturity:
        raise SystemExit("No candidate reached physiological maturity (first 10 sites): " + "; ".join(no_maturity[:10]))
    if set(winners) != set(targets):
        raise SystemExit(f"Calibration missing results for {len(set(targets) - set(winners))} sites")
    args.summary_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.summary_csv.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=["LATITUDE", "LONGITUDE", "GM", "SIM_MDOY", "TARGET_MDOY", "ABS_ERROR_DAYS", "STATUS"])
        writer.writeheader()
        for key in sorted(candidates):
            for row in sorted(candidates[key], key=lambda item: (item["STATUS"] != "valid", int(item["ABS_ERROR_DAYS"]) if item["STATUS"] == "valid" else 999999, item["GM"])):
                writer.writerow({"LATITUDE": key[0], "LONGITUDE": key[1], **row})
    schema["properties"] = dict(schema["properties"])
    schema["properties"].update({"ZONE_ID": "str:48", "INGENO": "str:32", "CNAME": "str:64", "GM_WIN": "str:16", "GM_ERR_D": "int", "SIM_MDOY": "int", "GM_REFYR": "int"})
    with fiona.open(args.output_sites, "w", driver="GPKG", schema=schema, crs=crs, layer="sites") as target:
        for feature in source_features:
            lon, lat = feature["geometry"]["coordinates"]
            winner = winners[(round(lat, 4), round(lon, 4))]
            props = dict(feature["properties"])
            gm = winner["GM"]
            cultivar = cultivar_by_gm[gm]
            props.update({"ZONE_ID": f"GM_{gm}", "INGENO": str(cultivar["ingeno"]), "CNAME": str(cultivar["cname"]), "GM_WIN": gm, "GM_ERR_D": int(winner["ABS_ERROR_DAYS"]), "SIM_MDOY": int(winner["SIM_MDOY"]), "GM_REFYR": args.reference_year})
            target.write({"type": "Feature", "geometry": feature["geometry"], "properties": props})
    print(f"Wrote {len(winners)} winners -> {args.output_sites}; candidate diagnostics -> {args.summary_csv}")


if __name__ == "__main__":
    main()
