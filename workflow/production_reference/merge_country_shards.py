#!/usr/bin/env python3
"""Merge validated annual shards into internal, wide country CSVs.

These merged files retain the complete P01--P05 audit schema and are inputs to
``export_analysis_table.py``.  They are pipeline internals, not the two public
long-format country summaries.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


def safe_country(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def open_csv(path: Path):
    return gzip.open(path, "rt", newline="", encoding="utf-8") if path.suffix == ".gz" else path.open("r", newline="", encoding="utf-8")


def read_manifest(path: Path) -> list[Path]:
    configs = [Path(line.strip()) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not configs:
        raise ValueError("Manifest has no configurations")
    return configs


def merge(kind: str, shard_dirs: list[Path], country: str, output: Path, keep_ids: set[str] | None = None) -> int:
    filename = f"summary_{kind}_{safe_country(country)}.csv.gz"
    header: list[str] | None = None
    rows = 0
    fd, temporary_name = tempfile.mkstemp(prefix=f".{filename}.", suffix=".partial", dir=output.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with gzip.open(temporary, "wt", newline="", encoding="utf-8") as target:
            writer: csv.writer | None = None
            for shard in shard_dirs:
                source_path = shard / filename
                if not source_path.is_file():
                    raise ValueError(f"Missing {filename} in shard {shard}")
                with open_csv(source_path) as source:
                    reader = csv.reader(source)
                    this_header = next(reader, None)
                    if not this_header:
                        raise ValueError(f"Empty CSV shard: {source_path}")
                    if header is None:
                        header, writer = this_header, csv.writer(target)
                        writer.writerow(header)
                    elif this_header != header:
                        raise ValueError(f"Incompatible CSV header in {source_path}")
                    assert writer is not None
                    cell_column = this_header.index("CELL_ID") if keep_ids is not None else None
                    for row in reader:
                        if keep_ids is not None and row[cell_column] not in keep_ids:
                            continue
                        writer.writerow(row)
                        rows += 1
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("results_root", type=Path, help="Persistent root containing tasks/<config-name>/ shards")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--coverage-reference", type=Path)
    parser.add_argument("--minimum-area", type=float, default=.95)
    parser.add_argument("--coverage-exception", type=Path)
    args = parser.parse_args()

    configs = read_manifest(args.manifest)
    config_data: dict[str, tuple[Path, dict]] = {}
    countries: set[str] = set()
    for config_path in configs:
        resolved = config_path.resolve()
        config = json.loads(resolved.read_text(encoding="utf-8"))
        if config.get("schema_version") != 2 or config.get("pipeline", {}).get("disease_design") != "single_calibrated_parameter_set":
            raise SystemExit(f"Configuration is not from the current single-disease pipeline: {resolved}")
        name = str(config.get("name") or resolved.stem)
        if name in config_data:
            raise SystemExit(f"Duplicate configuration name in manifest: {name}")
        config_data[name] = (resolved, config)
        countries.add(str(config.get("pipeline", {}).get("country") or ""))
    if len(countries) != 1 or not next(iter(countries)):
        raise SystemExit("A scratch-results merge requires one non-empty country per manifest")
    country = next(iter(countries))
    exception = None
    if args.coverage_exception:
        from country_coverage_exception import load_exception
        if args.coverage_reference is None or args.minimum_area != .95:
            raise ValueError("Exception requires the original coverage reference and default 95% gate")
        args.minimum_area, exception = load_exception(
            args.coverage_exception, country, args.manifest, args.results_root)

    tasks_root = args.results_root / "tasks"
    if not tasks_root.is_dir():
        raise SystemExit(f"No staged task shards at {tasks_root}")
    shard_dirs: list[Path] = []
    for name, (config_path, config) in config_data.items():
        shard = tasks_root / name
        audit_path = shard / "audit.json"
        if not audit_path.is_file():
            raise SystemExit(f"Missing completed shard or audit: {shard}")
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        if audit.get("schema_version") != 2 or audit.get("status") != "ok" or audit.get("config_name") != name:
            raise SystemExit(f"Invalid audit in {audit_path}")
        if audit.get("disease_design") != "single_calibrated_parameter_set":
            raise SystemExit(f"Obsolete disease design in {audit_path}")
        if audit.get("source_config_sha256") != sha256(config_path):
            raise SystemExit(f"Configuration changed after task execution: {config_path}")
        if int(audit.get("result_rows", {}).get("disease", -1)) != int(audit.get("site_count", -2)):
            raise SystemExit(f"Disease compact-row audit failed in {audit_path}")
        if int(audit.get("result_rows", {}).get("control", -1)) != int(audit.get("site_count", -2)):
            raise SystemExit(f"Control compact-row audit failed in {audit_path}")
        shard_dirs.append(shard)

    output_dir = args.output_dir or args.results_root / "_pipeline" / "merged_wide" / safe_country(country)
    output_dir.mkdir(parents=True, exist_ok=True)
    final_disease = output_dir / f"summary_disease_{safe_country(country)}.csv.gz"
    final_control = output_dir / f"summary_control_{safe_country(country)}.csv.gz"
    final_audit = output_dir / "merge_audit.json"
    if not args.overwrite and any(path.exists() for path in (final_disease, final_control, final_audit)):
        raise SystemExit(f"Final country outputs already exist in {output_dir}; use --overwrite only after review")

    keep_ids = None
    coverage = None
    if args.coverage_reference is not None:
        from coverage_policy import coverage_report, write_json
        # Fixed population for EVERY year/GCM/scenario and both disease arms.
        # Per-task 95% alone would allow different excluded pixels each year.
        for shard in shard_dirs:
            pair = []
            for kind in ("disease", "control"):
                with open_csv(shard / f"summary_{kind}_{safe_country(country)}.csv.gz") as source:
                    reader = csv.DictReader(source)
                    cells = [row["CELL_ID"] for row in reader]
                expected_count = json.loads((shard / "audit.json").read_text())["site_count"]
                if len(cells) != len(set(cells)) or len(cells) != expected_count:
                    raise ValueError(f"Duplicate/missing point rows in {shard}, {kind}")
                pair.append(set(cells))
            if pair[0] != pair[1]:
                raise ValueError(f"Disease/control population differs in {shard}")
            keep_ids = pair[0] if keep_ids is None else keep_ids & pair[0]
        coverage = coverage_report(args.coverage_reference, keep_ids or set(), args.minimum_area)
        if exception is not None:
            coverage["coverage_exception"] = exception
            coverage["default_95pct_passed"] = coverage["retained_area_pct"] >= 95.
        write_json(output_dir / "country_coverage.json", coverage)
        if coverage["status"] != "ok":
            raise SystemExit(f"Common population covers only {coverage['retained_area_pct']:.4f}% of original SPAM area")
    disease_rows = merge("disease", shard_dirs, country, final_disease, keep_ids)
    control_rows = merge("control", shard_dirs, country, final_control, keep_ids)
    total_warnings = sum(int(json.loads((shard / "audit.json").read_text(encoding="utf-8"))["files"]["WARNING.OUT"]["observed"]) for shard in shard_dirs)
    report = {
        "schema_version": 3,
        "status": "ok",
        "country": country,
        "manifest": str(args.manifest.resolve()),
        "results_root": str(args.results_root.resolve()),
        "configuration_count": len(shard_dirs),
        "disease_design": "single_calibrated_parameter_set",
        "rows": {"disease": disease_rows, "control": control_rows},
        "warning_files_across_tasks": total_warnings,
        "role": "internal_wide_source_for_long_format_export",
        "outputs": {"disease": final_disease.name, "control": final_control.name},
    }
    if coverage is not None:
        report["coverage"] = coverage
    final_audit.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"Merged {len(shard_dirs)} validated configurations for {country}: "
        f"disease={disease_rows} rows; control={control_rows} rows -> {output_dir}"
    )


if __name__ == "__main__":
    main()
