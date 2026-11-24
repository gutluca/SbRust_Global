#!/usr/bin/env python3
"""Select one fixed, unmodified generic DSSAT soybean GM per pixel.

The inverse calibration evaluates every available annual historical GGCMI
season.  GGCMI planting dates are forced in DSSAT, and the winning candidate
minimizes the *mean absolute maturity-date error* across the full historical
record, unless the explicit --extreme-policy option adds a documented soft
preference after the original ranking. A selected GM is fixed for production
simulation; future calendars change planting dates only.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import fiona

from maturity_extreme_policy import choose, load_policy


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def open_csv(path: Path):
    return gzip.open(path, "rt", newline="", encoding="utf-8") if path.suffix == ".gz" else path.open("r", newline="", encoding="utf-8")


def coord_key(lat: float, lon: float) -> tuple[str, str]:
    return f"{round(lat, 4):.4f}", f"{round(lon, 4):.4f}"


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return float(ordered[max(0, math.ceil(fraction * len(ordered)) - 1)])


def median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return float(ordered[middle]) if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path, help="Manifest from generate_maturity_ensemble.py")
    parser.add_argument("member_results_root", type=Path, help="Persistent root containing calibration_members/<config-name>/")
    parser.add_argument("base_sites", type=Path, help="Original points; fixed GM attributes are written to a copy")
    parser.add_argument("candidates", type=Path, help="generic_maturity_candidates.json")
    parser.add_argument("output_sites", type=Path)
    parser.add_argument("score_csv", type=Path, help="One compact score row per generic GM/pixel (.gz allowed)")
    parser.add_argument("--scratch-dir", type=Path, required=True, help="Node-local directory for temporary SQLite state")
    parser.add_argument("--ambiguity-margin-days", type=float, default=2.0, help="Flag GM_AMBIG when runner-up MAE is within this many days")
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--extreme-policy", type=Path,
                        help="Optional versioned soft preference; absent retains original selection")
    args = parser.parse_args()
    policy = load_policy(args.extreme_policy) if args.extreme_policy else None

    if args.ambiguity_margin_days < 0:
        raise ValueError("--ambiguity-margin-days must be non-negative")
    if not args.base_sites.is_file() or not args.candidates.is_file():
        raise SystemExit("Base sites or generic GM candidate file is missing")
    if args.output_sites.exists() or args.score_csv.exists():
        raise SystemExit("Refusing to overwrite an existing ensemble selection output")
    audit_path = args.audit or args.output_sites.with_name(args.output_sites.stem + "_audit.json")
    if audit_path.exists():
        raise SystemExit(f"Refusing to overwrite audit output: {audit_path}")

    configs = [Path(line.strip()) for line in args.manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not configs:
        raise SystemExit("Ensemble manifest is empty")
    candidate_data = json.loads(args.candidates.read_text(encoding="utf-8"))
    if candidate_data.get("method") != "dssat_generic_gm_v1":
        raise SystemExit("Selector requires method=dssat_generic_gm_v1")
    candidates = candidate_data.get("cultivars", [])
    candidate_by_gm = {str(item.get("gm") or ""): item for item in candidates}
    order = {str(item.get("gm") or ""): index for index, item in enumerate(candidates)}
    if not candidate_by_gm or "" in candidate_by_gm or len(candidate_by_gm) != len(candidates):
        raise SystemExit("Candidate file has no generic GMs or has duplicate GM labels")
    if any(not str(item.get("ingeno") or "") or not str(item.get("cname") or "") for item in candidates):
        raise SystemExit("Every generic GM candidate needs ingeno and cname")

    members = []
    reference_years: tuple[int, ...] | None = None
    gcms: set[str] = set()
    for config_path in configs:
        if not config_path.is_file():
            raise SystemExit(f"Member configuration missing: {config_path}")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        metadata = config.get("pipeline", {})
        if metadata.get("purpose") != "inverse_maturity_ensemble_member" or metadata.get("candidate_method") != "dssat_generic_gm_v1":
            raise SystemExit(f"Not a generic-GM ensemble member: {config_path}")
        name = str(config.get("name") or config_path.stem)
        gcm = str(metadata.get("gcm") or "")
        years = tuple(int(value) for value in metadata.get("calibration_years", []))
        if not gcm or not years or gcm in gcms:
            raise SystemExit("Member manifest must contain one unique GCM per configuration")
        if reference_years is None:
            reference_years = years
        elif years != reference_years:
            raise SystemExit("All ensemble members must use the same ordered reference years")
        if int(metadata.get("candidate_count", -1)) != len(candidates):
            raise SystemExit(f"Candidate count differs in {config_path}")
        result_dir = args.member_results_root / "calibration_members" / name
        audit, data = result_dir / "audit.json", result_dir / "maturity_member_candidates.csv.gz"
        if not audit.is_file() or not data.is_file():
            raise SystemExit(f"Completed member output missing for {name}: {result_dir}")
        report = json.loads(audit.read_text(encoding="utf-8"))
        if report.get("status") != "ok" or report.get("source_config_sha256") != sha256(config_path):
            raise SystemExit(f"Member audit does not match its canonical configuration: {audit}")
        if report.get("candidate_method") != "dssat_generic_gm_v1" or report.get("gcm") != gcm:
            raise SystemExit(f"Member audit is not a matching generic-GM result: {audit}")
        if tuple(int(value) for value in report.get("calibration_years", [])) != years:
            raise SystemExit(f"Member audit metadata differs from configuration: {audit}")
        members.append((name, gcm, years, data))
        gcms.add(gcm)
    assert reference_years is not None
    cases = {(gcm, year) for _, gcm, years, _ in members for year in years}
    expected_per_gm = len(cases)

    with fiona.open(args.base_sites) as source:
        schema, crs = source.schema.copy(), source.crs
        base_features = list(source)
    if not base_features:
        raise SystemExit("Base sites are empty")
    base_keys = set()
    for feature in base_features:
        lon, lat = feature["geometry"]["coordinates"]
        key = coord_key(lat, lon)
        if key in base_keys:
            raise SystemExit(f"Duplicate base coordinate: {key}")
        base_keys.add(key)

    args.scratch_dir.mkdir(parents=True, exist_ok=True)
    database = args.scratch_dir / f"generic_gm_ensemble_{os.getpid()}.sqlite"
    if database.exists():
        raise SystemExit(f"Temporary selection database already exists: {database}")
    connection = sqlite3.connect(database)
    try:
        connection.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=FILE;
            CREATE TABLE cases (
                lat TEXT NOT NULL, lon TEXT NOT NULL, cell_id TEXT NOT NULL,
                gm TEXT NOT NULL, gcm TEXT NOT NULL, year INTEGER NOT NULL,
                grow_error REAL, mat_error REAL, status TEXT NOT NULL,
                PRIMARY KEY (lat, lon, gm, gcm, year)
            ) WITHOUT ROWID;
        """)
        inserted = 0
        for _, gcm, years, data_path in members:
            with open_csv(data_path) as source:
                reader = csv.DictReader(source)
                required_fields = {
                    "CELL_ID", "LATITUDE", "LONGITUDE", "GM", "INGENO", "CNAME", "GCM", "YEAR",
                    "ABS_GROW_ERROR_DAYS", "ABS_MAT_ERROR_DAYS", "STATUS",
                }
                if reader.fieldnames is None or not required_fields <= set(reader.fieldnames):
                    raise SystemExit(f"Unexpected diagnostic columns in {data_path}")
                batch = []
                for row in reader:
                    row_gcm, year, gm = str(row["GCM"]), int(row["YEAR"]), str(row["GM"])
                    candidate = candidate_by_gm.get(gm)
                    if row_gcm != gcm or year not in years or candidate is None:
                        raise SystemExit(f"Unexpected case in {data_path}: {row}")
                    if str(row["INGENO"]) != str(candidate["ingeno"]) or str(row["CNAME"]) != str(candidate["cname"]):
                        raise SystemExit(f"Candidate identity mismatch in {data_path}: {row}")
                    key = coord_key(float(row["LATITUDE"]), float(row["LONGITUDE"]))
                    if key not in base_keys:
                        raise SystemExit(f"Member result is outside base sites: {key}")
                    valid = row["STATUS"] == "valid"
                    batch.append((
                        key[0], key[1], row["CELL_ID"], gm, gcm, year,
                        float(row["ABS_GROW_ERROR_DAYS"]) if valid else None,
                        float(row["ABS_MAT_ERROR_DAYS"]) if valid and row["ABS_MAT_ERROR_DAYS"] else None,
                        row["STATUS"],
                    ))
                    if len(batch) >= 10000:
                        connection.executemany("INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
                        inserted += len(batch)
                        batch.clear()
                if batch:
                    connection.executemany("INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)
                    inserted += len(batch)
        connection.commit()
        expected_rows = len(base_features) * len(candidates) * expected_per_gm
        if inserted != expected_rows:
            raise SystemExit(f"Member diagnostics contain {inserted}/{expected_rows} expected candidate cases")

        args.score_csv.parent.mkdir(parents=True, exist_ok=True)
        opener = gzip.open if args.score_csv.suffix == ".gz" else open
        score_fields = [
            "CELL_ID", "LATITUDE", "LONGITUDE", "GM", "INGENO", "CNAME",
            "MEAN_ABS_MAT_ERROR_DAYS", "MEDIAN_ABS_MAT_ERROR_DAYS", "P90_ABS_MAT_ERROR_DAYS",
            "MEAN_ABS_GROW_ERROR_DAYS", "P90_ABS_GROW_ERROR_DAYS", "FAILED_CASES", "CASE_COUNT", "STABILITY", "SELECTED",
            "RUNNER_UP_GM", "QC_OK", "AMBIGUOUS",
        ]
        if policy:
            score_fields += ["BASELINE_SELECTED", "BASELINE_STABILITY", "SELECTION_POLICY",
                             "BASELINE_GM", "ALTERNATIVE_GM", "ALTERNATIVE_MAE_INCREASE_DAYS",
                             "EXTREME_MARGIN_DAYS", "SELECTION_REASON"]
        schema["properties"] = dict(schema["properties"])
        schema["properties"].update({
            "ZONE_ID": "str:48", "INGENO": "str:32", "CNAME": "str:64", "GM_WIN": "str:16",
            "GM_ERR_D": "float", "SIM_MDOY": "int", "GM_REFYR": "int", "GM_METHOD": "str:32",
            "GM_P90": "float", "GM_MEA": "float", "GM_MAT_D": "float", "GM_GROW_D": "float", "GM_FAIL": "int",
            "GM_CASE": "int", "GM_STAB": "float", "GM_NEXT": "str:16", "GM_MARG": "float",
            "GM_GCM": "int", "GM_YR0": "int", "GM_YRN": "int", "GM_OK": "int", "GM_AMBIG": "int",
        })
        if policy:
            schema["properties"].update({"GM_POLICY": "str:40", "GM_BASE": "str:16",
                "GM_BERR": "float", "GM_BSTAB": "float", "GM_BMARG": "float",
                "GM_ALT": "str:16", "GM_DELTA": "float", "GM_LIMIT": "float", "GM_REASON": "str:48"})
        args.output_sites.parent.mkdir(parents=True, exist_ok=True)
        with opener(args.score_csv, "wt", newline="", encoding="utf-8") as score_file, fiona.open(
            args.output_sites, "w", driver="GPKG", schema=schema, crs=crs, layer="sites"
        ) as target:
            writer = csv.DictWriter(score_file, fieldnames=score_fields)
            writer.writeheader()
            for feature in base_features:
                lon, lat = feature["geometry"]["coordinates"]
                key = coord_key(lat, lon)
                rows = connection.execute(
                    "SELECT cell_id, gm, gcm, year, grow_error, mat_error, status FROM cases WHERE lat=? AND lon=?", key
                ).fetchall()
                if len(rows) != len(candidates) * expected_per_gm:
                    raise SystemExit(f"Incomplete candidate matrix at {key}: {len(rows)}/{len(candidates) * expected_per_gm}")
                by_gm: dict[str, dict[tuple[str, int], tuple[float | None, float | None, str, str]]] = {gm: {} for gm in candidate_by_gm}
                cell_id = ""
                for this_cell_id, gm, row_gcm, year, grow_error, mat_error, status in rows:
                    case = (str(row_gcm), int(year))
                    if case not in cases or case in by_gm[gm]:
                        raise SystemExit(f"Duplicate or unknown case at {key}: {gm} {case}")
                    by_gm[gm][case] = (None if grow_error is None else float(grow_error), None if mat_error is None else float(mat_error), str(status), str(this_cell_id))
                    cell_id = str(this_cell_id)
                scores: dict[str, dict[str, float | int]] = {}
                for gm, values in by_gm.items():
                    if set(values) != cases:
                        raise SystemExit(f"Missing GCM/year cases for GM {gm} at {key}")
                    grow_errors = [values[case][0] if values[case][2] == "valid" else 999.0 for case in cases]
                    mat_errors = [values[case][1] if values[case][2] == "valid" and values[case][1] is not None else 999.0 for case in cases]
                    failures = sum(values[case][2] != "valid" for case in cases)
                    scores[gm] = {
                        "mat_mean": sum(float(value) for value in mat_errors) / len(mat_errors),
                        "mat_median": median([float(value) for value in mat_errors]),
                        "mat_p90": percentile([float(value) for value in mat_errors], 0.90),
                        "grow_mean": sum(float(value) for value in grow_errors) / len(grow_errors),
                        "grow_p90": percentile([float(value) for value in grow_errors], 0.90),
                        "failures": failures,
                    }
                eligible = [gm for gm in candidate_by_gm if int(scores[gm]["failures"]) == 0]
                pool = eligible if eligible else list(candidate_by_gm)
                ranked = sorted(
                    candidate_by_gm,
                    key=lambda gm: (
                        0 if int(scores[gm]["failures"]) == 0 else 1,
                        int(scores[gm]["failures"]), float(scores[gm]["mat_mean"]), float(scores[gm]["mat_median"]),
                        float(scores[gm]["mat_p90"]), float(scores[gm]["grow_mean"]), float(scores[gm]["grow_p90"]), order[gm],
                    ),
                )
                decision = choose(ranked, scores, policy)
                baseline, winner = decision["baseline"], decision["winner"]
                runner_up = next((gm for gm in ranked if gm != winner), "")
                def candidate_stability(group):
                    hits = 0
                    for case in cases:
                        error = by_gm[group][case][1] if by_gm[group][case][2] == "valid" else 999.0
                        best_error = min(by_gm[gm][case][1] if by_gm[gm][case][2] == "valid" else 999.0 for gm in pool)
                        hits += abs(float(error) - float(best_error)) < 1e-9
                    return hits / expected_per_gm
                stability = candidate_stability(winner)
                baseline_stability = candidate_stability(baseline) if winner != baseline else stability
                winner_candidate, winner_score = candidate_by_gm[winner], scores[winner]
                qc_ok = int(int(winner_score["failures"]) == 0)
                margin = float(scores[runner_up]["mat_mean"] - winner_score["mat_mean"]) if runner_up else float("inf")
                ambiguous = int(abs(margin) <= args.ambiguity_margin_days) if policy else int(margin <= args.ambiguity_margin_days)
                extra = {}
                if policy:
                    extra = dict(SELECTION_POLICY=policy["version"], BASELINE_GM=baseline,
                        ALTERNATIVE_GM=decision["alternative"],
                        ALTERNATIVE_MAE_INCREASE_DAYS=decision["delta"],
                        EXTREME_MARGIN_DAYS=decision["threshold"], SELECTION_REASON=decision["reason"])
                for gm in ranked:
                    candidate, score = candidate_by_gm[gm], scores[gm]
                    precision = ".17g" if policy else ".6g"
                    writer.writerow({
                        "CELL_ID": cell_id, "LATITUDE": key[0], "LONGITUDE": key[1], "GM": gm,
                        "INGENO": candidate["ingeno"], "CNAME": candidate["cname"],
                        "MEAN_ABS_MAT_ERROR_DAYS": format(float(score['mat_mean']), precision),
                        "MEDIAN_ABS_MAT_ERROR_DAYS": format(float(score['mat_median']), precision),
                        "P90_ABS_MAT_ERROR_DAYS": format(float(score['mat_p90']), precision),
                        "MEAN_ABS_GROW_ERROR_DAYS": format(float(score['grow_mean']), precision),
                        "P90_ABS_GROW_ERROR_DAYS": format(float(score['grow_p90']), precision),
                        "FAILED_CASES": score["failures"], "CASE_COUNT": expected_per_gm,
                        "STABILITY": f"{stability:.6g}" if gm == winner else "", "SELECTED": int(gm == winner),
                        "RUNNER_UP_GM": runner_up if gm == winner else "", "QC_OK": qc_ok if gm == winner else "",
                        "AMBIGUOUS": ambiguous if gm == winner else "",
                        **(dict(extra, BASELINE_SELECTED=int(gm == baseline),
                                BASELINE_STABILITY=f"{baseline_stability:.17g}" if gm == baseline else "") if policy else {}),
                    })
                props = dict(feature["properties"])
                props.update({
                    "ZONE_ID": f"GM_{winner}", "INGENO": str(winner_candidate["ingeno"]), "CNAME": str(winner_candidate["cname"]),
                    "GM_WIN": winner, "GM_ERR_D": float(winner_score["mat_mean"]), "SIM_MDOY": -99, "GM_REFYR": 0,
                    "GM_METHOD": "GGCMI_HIST_MAT_V2", "GM_P90": float(winner_score["mat_p90"]), "GM_MEA": float(winner_score["mat_mean"]),
                    "GM_MAT_D": float(winner_score["mat_median"]), "GM_GROW_D": float(winner_score["grow_mean"]), "GM_FAIL": int(winner_score["failures"]),
                    "GM_CASE": expected_per_gm, "GM_STAB": stability, "GM_NEXT": runner_up,
                    "GM_MARG": margin if runner_up else -99.0,
                    "GM_GCM": len(gcms), "GM_YR0": min(reference_years), "GM_YRN": max(reference_years),
                    "GM_OK": qc_ok, "GM_AMBIG": ambiguous,
                })
                if policy:
                    props.update(GM_POLICY=policy["version"], GM_BASE=baseline,
                        GM_BERR=float(scores[baseline]["mat_mean"]), GM_BSTAB=baseline_stability,
                        GM_BMARG=float(scores[ranked[1]]["mat_mean"] - scores[baseline]["mat_mean"]),
                        GM_ALT=decision["alternative"], GM_DELTA=decision["delta"],
                        GM_LIMIT=decision["threshold"], GM_REASON=decision["reason"])
                target.write({"type": "Feature", "geometry": feature["geometry"], "properties": props})
    finally:
        connection.close()
        database.unlink(missing_ok=True)

    report = {
        "schema_version": 4, "status": "ok", "created_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": str(args.manifest.resolve()), "member_results_root": str(args.member_results_root.resolve()),
        "base_sites": str(args.base_sites.resolve()), "candidate_file": str(args.candidates.resolve()),
        "candidate_method": "dssat_generic_gm_v1", "candidate_count": len(candidates),
        "gcm_count": len(gcms), "gcms": sorted(gcms), "reference_years": list(reference_years),
        "cases_per_gm_pixel": expected_per_gm, "site_count": len(base_features),
        "score_rows": len(base_features) * len(candidates),
        "ambiguity_margin_days": args.ambiguity_margin_days,
        "outputs": {"calibrated_sites": args.output_sites.name, "scores": args.score_csv.name},
        "method": "among GMs completing every matched historical case: minimum(mean absolute GGCMI maturity-date error, median maturity-date error, p90 maturity-date error, mean duration error, p90 duration error, catalog order); GGCMI planting dates are forced and planting mismatches are ineligible",
    }
    if policy:
        report.update(schema_version=5, selection_policy=policy,
            selection_policy_sha256=sha256(args.extreme_policy),
            method="Original failure-count/maturity-error ranking, followed by versioned soft preference for a non-extreme candidate with equal failures; no geographic masks or cultivar coefficient changes",
            margin_semantics="GM_NEXT is best other candidate in original ranking; GM_MARG is its MAE minus final selected MAE, possibly negative; ambiguity uses absolute difference; GM_BMARG retains original ranking margin",
            stability_semantics="Both final and baseline stability recomputed from original annual candidate diagnostics, with the original comparison pool",
            input_sha256={str(p.resolve()): sha256(p) for p in
                          [args.base_sites, args.candidates, args.manifest, *configs, *[m[3] for m in members]]})
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Selected one fixed generic DSSAT GM for {len(base_features)} sites from {expected_per_gm} historical cases per GM -> {args.output_sites}")


if __name__ == "__main__":
    main()
