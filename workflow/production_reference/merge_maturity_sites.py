#!/usr/bin/env python3
"""Join calibrated GM batches, requiring exact point and score coverage."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
from pathlib import Path

import fiona


def cell_id(feature: dict) -> str:
    return str(dict(feature["properties"])["CellID"])


def read_manifest(path: Path) -> list[Path]:
    paths = [Path(line.strip()) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("Chunk manifest is empty or repeats a chunk")
    return paths


def merge(
    base_sites: Path,
    chunk_manifest: Path,
    selected_dir: Path,
    score_dir: Path,
    output_sites: Path,
    output_scores: Path,
    minimum_area: float | None = None,
    maximum_failures: int = 0,
) -> dict:
    if not 0 <= maximum_failures < 24:
        raise ValueError("maximum_failures must be between 0 and 23")
    audit_path = output_sites.with_name(output_sites.stem + "_audit.json")
    if any(path.exists() for path in (output_sites, output_scores, audit_path)):
        raise FileExistsError("Refusing to overwrite merged GM output or audit")
    if output_scores.suffix != ".gz":
        raise ValueError("Output scores must be a .csv.gz file")
    chunks = read_manifest(chunk_manifest)
    with fiona.open(base_sites) as source:
        base_features = list(source)
        base_schema, base_crs = source.schema.copy(), source.crs
    if not base_features:
        raise ValueError("Base sites are empty")
    base_by_id = {cell_id(feature): feature for feature in base_features}
    if len(base_by_id) != len(base_features):
        raise ValueError("Base sites contain duplicate CellID values")

    selected_by_id: dict[str, dict] = {}
    scores_by_id: dict[str, list[dict[str, str]]] = {}
    selected_schema = None
    score_header = None
    seen_chunks: set[str] = set()
    excluded_reasons = {}
    seen_base_ids = set()
    for chunk in chunks:
        stem = chunk.stem
        if stem in seen_chunks:
            raise ValueError(f"Repeated chunk stem: {stem}")
        seen_chunks.add(stem)
        selected_path = selected_dir / f"{stem}.gpkg"
        score_path = score_dir / f"{stem}.csv.gz"
        select_audit = selected_dir / f"{stem}_audit.json"
        with fiona.open(chunk) as source:
            chunk_ids = [cell_id(feature) for feature in source]
        if len(set(chunk_ids)) != len(chunk_ids) or seen_base_ids.intersection(chunk_ids) or not set(chunk_ids) <= set(base_by_id):
            raise ValueError(f"Invalid or overlapping base chunk: {chunk}")
        seen_base_ids.update(chunk_ids)
        if minimum_area is not None and select_audit.is_file():
            empty_report = json.loads(select_audit.read_text(encoding="utf-8"))
            if empty_report.get("status") == "empty":
                with fiona.open(chunk) as source:
                    for feature in source:
                        excluded_reasons[cell_id(feature)] = "no_complete_GM_matrix"
                continue
        if not all(path.is_file() for path in (chunk, selected_path, score_path, select_audit)):
            raise FileNotFoundError(f"Incomplete selected GM batch: {stem}")
        with fiona.open(chunk) as source:
            base_chunk = {cell_id(feature): feature for feature in source}
        with fiona.open(selected_path) as source:
            if selected_schema is None:
                selected_schema, selected_crs = source.schema.copy(), source.crs
            elif source.schema != selected_schema or source.crs != selected_crs:
                raise ValueError(f"Incompatible selected-site schema or CRS: {selected_path}")
            selected_chunk = {cell_id(feature): feature for feature in source}
        if not set(selected_chunk) <= set(base_chunk) or (minimum_area is None and set(base_chunk) != set(selected_chunk)):
            raise ValueError(f"Selected sites do not match the base batch: {stem}")
        report = json.loads(select_audit.read_text(encoding="utf-8"))
        if (
            report.get("status") != "ok"
            or int(report.get("site_count", -1)) != len(selected_chunk)
            or int(report.get("candidate_count", -1)) != 13
            or int(report.get("cases_per_gm_pixel", -1)) != 24
            or int(report.get("gcm_count", -1)) != 1
        ):
            raise ValueError(f"Invalid selection audit: {select_audit}")
        for missing_id in set(base_chunk) - set(selected_chunk):
            excluded_reasons[missing_id] = "incomplete_GM_outputs"
        accepted_chunk = set()
        for this_id, feature in selected_chunk.items():
            if this_id in selected_by_id or this_id not in base_by_id:
                raise ValueError(f"Duplicate or unknown selected CellID: {this_id}")
            props = dict(feature["properties"])
            failures = int(props.get("GM_FAIL") if props.get("GM_FAIL") is not None else -1)
            cases = int(props.get("GM_CASE") or 0)
            if failures < 0 or failures > cases:
                raise ValueError(f"Invalid GM failure count for CellID {this_id}")
            if failures > maximum_failures:
                if minimum_area is None:
                    raise ValueError(f"GM calibration failed for CellID {this_id}")
                excluded_reasons[this_id] = (
                    f"best_GM_valid_in_fewer_than_{24 - maximum_failures}_of_24_historical_cases"
                )
                continue
            if props.get("GM_METHOD") != "GGCMI_HIST_MAT_V2":
                raise ValueError(f"Unexpected GM method for CellID {this_id}")
            if cases != 24 or int(props.get("GM_GCM") or 0) != 1:
                raise ValueError(f"Incomplete historical GM cases for CellID {this_id}")
            expected_ok = int(failures == 0)
            if int(props.get("GM_OK") if props.get("GM_OK") is not None else -1) != expected_ok:
                raise ValueError(f"GM_OK/GM_FAIL inconsistency for CellID {this_id}")
            if dict(feature["geometry"]) != dict(base_chunk[this_id]["geometry"]):
                raise ValueError(f"Selected geometry changed for CellID {this_id}")
            for field in base_schema["properties"]:
                if props[field] != dict(base_chunk[this_id]["properties"])[field]:
                    raise ValueError(f"Base field {field} changed for CellID {this_id}")
            selected_by_id[this_id] = feature
            accepted_chunk.add(this_id)
        with gzip.open(score_path, "rt", newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                raise ValueError(f"Empty score table: {score_path}")
            if score_header is None:
                score_header = reader.fieldnames
            elif reader.fieldnames != score_header:
                raise ValueError(f"Incompatible score fields: {score_path}")
            for row in reader:
                this_id = row["CELL_ID"]
                if this_id not in selected_chunk:
                    raise ValueError(f"Score outside selected chunk: {this_id}")
                if this_id not in accepted_chunk:
                    continue
                scores_by_id.setdefault(this_id, []).append(row)

    if seen_base_ids != set(base_by_id):
        raise ValueError("Chunk manifest does not partition the original country reference")
    if set(scores_by_id) != set(selected_by_id) or (minimum_area is None and set(selected_by_id) != set(base_by_id)):
        raise ValueError("Merged GM batches do not cover every base CellID exactly once")
    coverage = None
    if minimum_area is not None:
        from coverage_policy import coverage_report, write_json
        coverage = coverage_report(base_sites, set(selected_by_id), minimum_area, excluded_reasons)
        coverage["gm_calibration_policy"] = {
            "historical_cases": 24,
            "minimum_valid_cases": 24 - maximum_failures,
            "maximum_failed_cases": maximum_failures,
            "winner_rule": "minimum failed cases, then maturity/growing-period error criteria",
        }
        write_json(output_sites.with_name(output_sites.stem + "_coverage.json"), coverage)
        if coverage["status"] != "ok":
            raise ValueError(f"Insufficient GM area coverage: {coverage['retained_area_pct']:.4f}%")
    assert score_header is not None and selected_schema is not None
    candidate_count = None
    for this_id, rows in scores_by_id.items():
        groups = [row["GM"] for row in rows]
        chosen = [row for row in rows if row["SELECTED"] == "1"]
        if len(groups) != len(set(groups)) or len(chosen) != 1:
            raise ValueError(f"Duplicate candidate or invalid winner count for CellID {this_id}")
        if chosen[0]["GM"] != dict(selected_by_id[this_id]["properties"])["GM_WIN"]:
            raise ValueError(f"Score winner differs from selected GPKG for CellID {this_id}")
        candidate_count = len(rows) if candidate_count is None else candidate_count
        if len(rows) != candidate_count:
            raise ValueError(f"Inconsistent candidate count for CellID {this_id}")
    if candidate_count != 13:
        raise ValueError(f"Expected all 13 generic GM candidates, found {candidate_count}")

    output_sites.parent.mkdir(parents=True, exist_ok=True)
    output_scores.parent.mkdir(parents=True, exist_ok=True)
    with fiona.open(output_sites, "w", driver="GPKG", schema=selected_schema, crs=selected_crs, layer="sites") as target, gzip.open(
        output_scores, "wt", newline="", encoding="utf-8"
    ) as score_target:
        writer = csv.DictWriter(score_target, fieldnames=score_header)
        writer.writeheader()
        for base_feature in base_features:
            this_id = cell_id(base_feature)
            if this_id not in selected_by_id:
                continue
            target.write(selected_by_id[this_id])
            writer.writerows(scores_by_id[this_id])
    audit = {
        "status": "ok", "base_sites": str(base_sites.resolve()),
        "chunk_manifest": str(chunk_manifest.resolve()),
        "chunk_count": len(chunks), "site_count": len(selected_by_id),
        "candidates_per_site": candidate_count,
        "score_rows": sum(len(rows) for rows in scores_by_id.values()),
        "calibrated_sites": str(output_sites.resolve()),
        "scores": str(output_scores.resolve()),
        "gm_calibration_policy": {
            "historical_cases": 24,
            "minimum_valid_cases": 24 - maximum_failures,
            "maximum_failed_cases": maximum_failures,
        },
    }
    if coverage is not None:
        audit["coverage"] = coverage
    audit_path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_sites", type=Path)
    parser.add_argument("chunk_manifest", type=Path)
    parser.add_argument("selected_dir", type=Path)
    parser.add_argument("score_dir", type=Path)
    parser.add_argument("output_sites", type=Path)
    parser.add_argument("output_scores", type=Path)
    parser.add_argument("--minimum-area", type=float)
    parser.add_argument("--maximum-failures", type=int, default=0)
    args = parser.parse_args()
    report = merge(
        args.base_sites, args.chunk_manifest, args.selected_dir,
        args.score_dir, args.output_sites, args.output_scores,
        args.minimum_area, args.maximum_failures,
    )
    print(f"Merged {report['site_count']} calibrated sites from {report['chunk_count']} batches; scores={report['score_rows']}")


if __name__ == "__main__":
    main()
