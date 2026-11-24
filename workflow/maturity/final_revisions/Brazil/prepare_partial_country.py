#!/usr/bin/env python3
"""Freeze changed Brazilian/Bolivian cells for a partial paired production run."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import fiona


RULE = "br_gm_cap20s_partial_20261117_v3"


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_assignments(path, country, expected):
    result = {}
    with Path(path).open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row["COUNTRY"] != country:
                continue
            cell = row["CELL_ID"]
            if cell in result:
                raise ValueError(f"Duplicate assignment CELL_ID: {country} {cell}")
            result[cell] = row
    if len(result) != expected:
        raise ValueError(f"Assignment population {country}: {len(result)}/{expected}")
    return result


def read_scores(path, cells):
    found = defaultdict(dict)
    with gzip.open(path, "rt", newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            cell = row["CELL_ID"]
            if cell in cells:
                gm = row["GM"]
                if gm in found[cell]:
                    raise ValueError(f"Duplicate score {cell} {gm}")
                found[cell][gm] = row
    if set(found) != set(cells) or any(len(rows) != 13 for rows in found.values()):
        raise ValueError("Incomplete 13-GM candidate score matrix")
    return found


def close(a, b, label, cell, atol=1e-5):
    if not math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=atol):
        raise ValueError(f"{label} mismatch at {cell}: {a} != {b}")


def make_sites(source_gpkg, target_gpkg, assignments, scores, catalog, country):
    changed = {cell: row for cell, row in assignments.items() if row["OLD_GM"] != row["NEW_GM"]}
    known = set()
    groups = Counter()
    with fiona.open(source_gpkg) as source:
        schema, crs = source.schema.copy(), source.crs
        schema["properties"] = dict(schema["properties"])
        schema["properties"].update({
            "GM_POLICY": "str:40", "GM_BASE": "str:16", "GM_BERR": "float",
            "GM_DELTA": "float", "GM_REASON": "str:48",
        })
        with fiona.open(target_gpkg, "w", driver="GPKG", layer="sites", schema=schema, crs=crs) as target:
            for feature in source:
                old = dict(feature["properties"])
                cell = str(old["CellID"])
                if cell not in assignments:
                    continue  # Calibration support can exceed the retained master support.
                if cell in known:
                    raise ValueError(f"Duplicate calibrated CellID: {cell}")
                known.add(cell)
                assigned = assignments[cell]
                lon, lat = feature["geometry"]["coordinates"]
                close(lat, assigned["LATITUDE"], "latitude", cell)
                close(lon, assigned["LONGITUDE"], "longitude", cell)
                close(old["HAREA"], assigned["HARVEST_AREA_HA"], "harvest area", cell, 0.01)
                if old["GM_WIN"] != assigned["OLD_GM"]:
                    raise ValueError(f"Old GM mismatch at {country} {cell}")
                close(old["GM_MEA"], assigned["OLD_MAE_DAYS"], "old MAE", cell, 1e-4)
                newgm = assigned["NEW_GM"]
                if (newgm == "10" or (newgm == "9" and lat <= -10) or
                        (lat <= -20 and int(newgm) > 7)):
                    raise ValueError(f"Regional GM restriction violated: {country} {cell}")
                if cell not in changed:
                    continue
                if int(old["GM_FAIL"]) != 0 or int(old["GM_CASE"]) != 24 or int(old["GM_OK"]) != 1:
                    raise ValueError(f"Original GM was not valid in 24/24 cases: {cell}")
                candidate = scores[cell][newgm]
                if int(candidate["FAILED_CASES"]) != 0 or int(candidate["CASE_COUNT"]) != 24:
                    raise ValueError(f"Selected candidate is not valid in 24/24 cases: {cell}")
                close(candidate["MEAN_ABS_MAT_ERROR_DAYS"], assigned["NEW_MAE_DAYS"],
                      "new MAE", cell, 1e-4)
                cultivar = catalog[newgm]
                old.update({
                    "ZONE_ID": f"GM_{newgm}", "INGENO": cultivar["ingeno"], "CNAME": cultivar["cname"],
                    "GM_WIN": newgm, "GM_ERR_D": float(candidate["MEAN_ABS_MAT_ERROR_DAYS"]),
                    "GM_MEA": float(candidate["MEAN_ABS_MAT_ERROR_DAYS"]),
                    "GM_MAT_D": float(candidate["MEDIAN_ABS_MAT_ERROR_DAYS"]),
                    "GM_P90": float(candidate["P90_ABS_MAT_ERROR_DAYS"]),
                    "GM_GROW_D": float(candidate["MEAN_ABS_GROW_ERROR_DAYS"]),
                    "GM_FAIL": 0, "GM_CASE": 24, "GM_OK": 1,
                    "GM_STAB": -99.0, "GM_NEXT": assigned["OLD_GM"],
                    "GM_MARG": float(assigned["OLD_MAE_DAYS"]) - float(assigned["NEW_MAE_DAYS"]),
                    "GM_AMBIG": int(abs(float(assigned["OLD_MAE_DAYS"]) - float(assigned["NEW_MAE_DAYS"])) <= 2),
                    "GM_POLICY": RULE, "GM_BASE": assigned["OLD_GM"],
                    "GM_BERR": float(assigned["OLD_MAE_DAYS"]),
                    "GM_DELTA": float(assigned["MAE_INCREASE_DAYS"]),
                    "GM_REASON": "regional_rule_frozen_assignment",
                })
                groups[f'{assigned["OLD_GM"]}->{newgm}'] += 1
                target.write({"type": "Feature", "geometry": feature["geometry"], "properties": old})
    if known != set(assignments) or sum(groups.values()) != len(changed):
        raise ValueError("Source calibration and frozen assignment populations differ")
    with fiona.open(target_gpkg) as check:
        if len(check) != len(changed):
            raise ValueError("Partial GeoPackage was not fully written")
    return dict(changed_cells=len(changed), transitions_by_cell=dict(groups))


def prepare(state_path, lock_path, assignments_path, country, output_root, results_root, catalog_path):
    lock = load(lock_path)
    if country != "Brazil" or set(lock["countries"]) != {"Brazil"} or lock["version"] != RULE:
        raise ValueError("Only the frozen Brazil v3 campaign is allowed")
    if Path(state_path).resolve() != Path(lock["source_state"]).resolve() or sha256(state_path) != lock["source_state_sha256"]:
        raise ValueError("Live source state differs from the approved snapshot")
    if sha256(assignments_path) != lock["assignments_sha256"]:
        raise ValueError("Frozen assignments changed")
    info = lock["countries"][country]
    score_path = Path(lock_path).parent / info["score_file"]
    if sha256(score_path) != info["score_sha256"]:
        raise ValueError("Candidate scores changed")
    state = load(state_path)
    if state.get("status") != "finished":
        raise ValueError("Original campaign is not finished")
    source = [c for c in state["countries"] if c["country"] == country]
    if len(source) != 1 or source[0]["key"] != info["key"] or source[0]["status"] != "completed":
        raise ValueError("Expected completed source country not found")
    original = source[0]["original"]
    original_master = Path(original["master"])
    if sha256(original_master) != info["original_paired_master_sha256"]:
        raise ValueError("First paired master differs from the audited controls source")
    baseline_master = (Path(state["results"]) / info["key"] / "annual" /
                       "country_summaries" / country / f"summary_master_{country}.csv.gz")
    if sha256(baseline_master) != info["old_master_sha256"]:
        raise ValueError("Final disease-only master differs from the preserved 103-country backup")
    final_project_path = Path(state["output"]) / info["key"] / "project.json"
    if sha256(final_project_path) != info["final_project_sha256"]:
        raise ValueError("Final country project differs from the preserved backup")
    final_project = load(final_project_path)
    if final_project["experiment"]["country"] != country:
        raise ValueError("Final project country mismatch")
    regional = final_project["experiment"].get("regional_parameter_assignment", {})
    if (source[0].get("parameter_set") != info["parameter_set"] or
            regional.get("parameter_set") != info["parameter_set"] or
            regional.get("input_sha256") != info["disease_input_sha256"]):
        raise ValueError("Final regional disease-parameter assignment changed")
    final_profile = final_project["profiles"]["hipergator"]
    disease_input = Path(final_profile["disease_parameters"])
    if sha256(disease_input) != info["disease_input_sha256"]:
        raise ValueError("Final disease-parameter file changed")
    assignments = read_assignments(assignments_path, country, info["expected_cells"])
    if sum(r["OLD_GM"] != r["NEW_GM"] for r in assignments.values()) != info["changed_cells"]:
        raise ValueError("Changed cell count differs from the frozen decision")
    scores = read_scores(score_path, assignments)
    if sha256(catalog_path) != lock["catalog_sha256"]:
        raise ValueError("Frozen generic-GM cultivar catalog changed")
    catalog_data = load(catalog_path)
    if catalog_data.get("method") != "dssat_generic_gm_v1":
        raise ValueError("Unexpected cultivar catalog")
    catalog = {c["gm"]: c for c in catalog_data["cultivars"]}
    manifest = Path(original["manifest"])
    configs = [Path(line) for line in manifest.read_text().splitlines() if line.strip()]
    if len(configs) != lock["expected_configurations_per_country"]:
        raise ValueError("Original annual configuration count is not 920")
    first_config = load(configs[0])
    if first_config.get("pipeline", {}).get("country") != country or first_config.get("pipeline", {}).get("disease_design") != "single_calibrated_parameter_set":
        raise ValueError("Original annual configuration is not the paired campaign")
    harvest = first_config["default_setup"]["harvestArea"]
    if not harvest.startswith("raster::") or not Path(harvest[8:]).is_file():
        raise ValueError("Original harvest-area raster not available")
    old_project = load(original["project"])
    if old_project["experiment"]["country"] != country:
        raise ValueError("Original project country mismatch")
    if (old_project["profiles"]["hipergator"]["dssat_executable"] !=
            final_profile["dssat_executable"]):
        raise ValueError("First paired and final campaigns used different executables")
    base = Path(output_root) / info["key"]
    if base.exists() or (Path(results_root) / info["key"]).exists():
        raise FileExistsError("Partial campaign output already exists")
    ready = base / "production"
    (ready / "calendars").mkdir(parents=True)
    sites = ready / "calibrated.gpkg"
    detail = make_sites(Path(original["calibrated"]), sites, assignments, scores, catalog, country)
    # Preserve the exact first-campaign GGCMI/climate/management configuration.
    # Only the final regional disease input, explicit assignment provenance,
    # and the new work destination are changed. Both DISES arms are generated.
    old_project["profiles"]["hipergator"]["disease_parameters"] = str(disease_input)
    old_project["profiles"]["hipergator"]["work_root"] = str(base / "work")
    old_project["experiment"]["regional_parameter_assignment"] = regional
    for path in (base / "project.json", ready / "calendars/project.json"):
        path.write_text(json.dumps(old_project, indent=2) + "\n", encoding="utf-8")
    audit = {
        "status": "ok", "country": country, "policy": RULE, "original": original,
        "baseline_master": str(baseline_master),
        "baseline_master_sha256": info["old_master_sha256"],
        "original_paired_master_sha256": info["original_paired_master_sha256"],
        "final_project": str(final_project_path),
        "final_project_sha256": info["final_project_sha256"],
        "final_disease_parameters": str(disease_input),
        "disease_input_sha256": info["disease_input_sha256"],
        "parameter_set": info["parameter_set"],
        "changed_sites": str(sites), "changed_sites_sha256": sha256(sites),
        "source_calibrated_sha256": sha256(original["calibrated"]),
        "source_project_sha256": sha256(original["project"]),
        "source_manifest_sha256": sha256(manifest),
        "old_master_sha256": info["old_master_sha256"],
        "assignment_sha256": lock["assignments_sha256"],
        "score_sha256": info["score_sha256"],
        "harvest_area_raster": harvest[8:],
        "new_project_sha256": sha256(base / "project.json"),
        "stability_note": "GM_STAB=-99 on changed cells: annual winner stability not recalculated; production does not use it",
        **detail,
    }
    (base / "partial_sites_audit.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(f"PREPARED {country}: {detail['changed_cells']} changed cells -> {sites}")
    return audit


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("state", type=Path)
    p.add_argument("lock", type=Path)
    p.add_argument("assignments", type=Path)
    p.add_argument("country", choices=("Brazil",))
    p.add_argument("output_root", type=Path)
    p.add_argument("results_root", type=Path)
    p.add_argument("catalog", type=Path)
    a = p.parse_args()
    prepare(a.state, a.lock, a.assignments, a.country, a.output_root, a.results_root, a.catalog)


if __name__ == "__main__":
    main()
