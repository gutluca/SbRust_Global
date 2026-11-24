#!/usr/bin/env python3
"""Replace complete changed-cell blocks in an immutable country master.

Both inputs must be canonical, cell-sorted summary_master_*.csv.gz files.
Unchanged lines are copied verbatim from the old gzip stream; changed lines
come from the newly simulated paired disease/control master.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path


FIELDS = [
    "SIMULATION_ID", "COUNTRY", "CELL_ID", "WEATHER_SOURCE_CELL_ID",
    "WEATHER_BORROWED", "WEATHER_DISTANCE_KM", "LATITUDE", "LONGITUDE",
    "HARVEST_AREA_HA", "MATURITY_GROUP", "PERIOD", "SIM_YEAR", "SCENARIO",
    "GCM", "PLANTING_LABEL", "PLANT_DATE", "MATURITY_DATE",
    "YIELD_WITH_DISEASE_KG_HA", "YIELD_NO_DISEASE_KG_HA", "LOSS_KG_HA",
    "LOSS_PCT", "TOTAL_SPRAYS", "DISEASE_ONSET_DAE",
]
PAIRED = ("SIMULATION_ID", "COUNTRY", "CELL_ID", "WEATHER_SOURCE_CELL_ID",
          "WEATHER_BORROWED", "PERIOD", "SIM_YEAR", "SCENARIO", "GCM",
          "PLANTING_LABEL", "PLANT_DATE")
NUMERIC_PAIRED = ("WEATHER_DISTANCE_KM", "LATITUDE", "LONGITUDE", "HARVEST_AREA_HA")


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_assignments(path, country):
    rows = {}
    with Path(path).open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row["COUNTRY"] == country:
                cell = row["CELL_ID"]
                if cell in rows:
                    raise ValueError(f"Duplicate assignment CELL_ID {cell}")
                rows[cell] = row
    return rows


def grouped_master(path, country, expected_per_cell):
    with gzip.open(path, "rt", newline="", encoding="utf-8") as stream:
        header = stream.readline()
        if not header or next(csv.reader([header], delimiter=";")) != FIELDS:
            raise ValueError(f"Unexpected master schema: {path}")
        current = None
        block = []
        previous_cell = -1
        for number, line in enumerate(stream, 2):
            fields = next(csv.reader([line], delimiter=";"))
            if len(fields) != len(FIELDS):
                raise ValueError(f"Malformed master row {number}: {path}")
            row = dict(zip(FIELDS, fields))
            if row["COUNTRY"] != country:
                raise ValueError(f"Country mismatch at row {number}: {path}")
            cell = row["CELL_ID"]
            if not cell.isdecimal():
                raise ValueError(f"Invalid CELL_ID at row {number}")
            if current is None:
                current = cell
            if cell != current:
                if int(current) <= previous_cell:
                    raise ValueError("Master CELL_ID blocks are not increasing")
                previous_cell = int(current)
                yield current, block
                current, block = cell, []
            block.append((line, row))
        if current is not None:
            if int(current) <= previous_cell:
                raise ValueError("Master CELL_ID blocks are not increasing")
            yield current, block


def check_block(cell, block, gm, expected_per_cell):
    if len(block) != expected_per_cell:
        raise ValueError(f"Cell {cell}: {len(block)}/{expected_per_cell} master rows")
    seen = set()
    for _, row in block:
        key = row["SIMULATION_ID"]
        if key in seen or row["CELL_ID"] != cell or row["MATURITY_GROUP"] != gm:
            raise ValueError(f"Cell {cell}: duplicate simulation or wrong GM")
        seen.add(key)
        expected_id = "|".join(row[f] for f in (
            "COUNTRY", "CELL_ID", "PERIOD", "SIM_YEAR", "SCENARIO", "GCM", "PLANTING_LABEL"))
        if key != expected_id:
            raise ValueError(f"Cell {cell}: malformed SIMULATION_ID {key}")


def check_replacement(cell, old_block, new_block, expected_per_cell):
    for (_, old), (_, new) in zip(old_block, new_block):
        for field in PAIRED:
            if old[field] != new[field]:
                raise ValueError(f"Cell {cell}: changed {field}: {old[field]!r} != {new[field]!r}")
        for field in NUMERIC_PAIRED:
            if not math.isclose(float(old[field]), float(new[field]), rel_tol=1e-9, abs_tol=1e-4):
                raise ValueError(f"Cell {cell}: changed {field}")


def merge(old_master, new_partial, assignments_csv, lock_json, country, output, audit_path):
    old_master, new_partial = Path(old_master), Path(new_partial)
    output, audit_path = Path(output), Path(audit_path)
    if output.exists() or audit_path.exists():
        raise FileExistsError("Refusing to overwrite a hybrid master or audit")
    lock = json.loads(Path(lock_json).read_text(encoding="utf-8"))
    info = lock["countries"][country]
    if sha256(old_master) != info["old_master_sha256"]:
        raise ValueError("Old country master differs from frozen baseline")
    if sha256(assignments_csv) != lock["assignments_sha256"]:
        raise ValueError("Frozen assignments changed")
    assignments = read_assignments(assignments_csv, country)
    changed = {cell for cell, row in assignments.items() if row["OLD_GM"] != row["NEW_GM"]}
    if len(assignments) != info["expected_cells"] or len(changed) != info["changed_cells"]:
        raise ValueError("Unexpected assignment population")
    per_cell = lock["expected_rows_per_cell"]
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".hybrid_", suffix=".partial.gz", dir=output.parent)
    os.close(fd)
    temp = Path(temp_name)
    old_count = new_count = cells = 0
    seen = set()
    try:
        old_groups = grouped_master(old_master, country, per_cell)
        new_groups = iter(grouped_master(new_partial, country, per_cell))
        new_item = next(new_groups, None)
        with gzip.open(temp, "wt", newline="", encoding="utf-8") as target:
            target.write(";".join(FIELDS) + "\r\n")
            for cell, old_block in old_groups:
                if cell not in assignments or cell in seen:
                    raise ValueError(f"Unexpected or duplicate original cell {cell}")
                seen.add(cell)
                cells += 1
                decision = assignments[cell]
                check_block(cell, old_block, decision["OLD_GM"], per_cell)
                if cell in changed:
                    if new_item is None or new_item[0] != cell:
                        raise ValueError(f"Missing new paired master block for {cell}")
                    new_block = new_item[1]
                    check_block(cell, new_block, decision["NEW_GM"], per_cell)
                    check_replacement(cell, old_block, new_block, per_cell)
                    for line, _ in new_block:
                        target.write(line)
                    new_count += per_cell
                    new_item = next(new_groups, None)
                else:
                    for line, _ in old_block:
                        target.write(line)
                    old_count += per_cell
        if new_item is not None or seen != set(assignments) or old_count + new_count != len(assignments)*per_cell:
            raise ValueError("Incomplete or extra cells in hybrid master")
        # Consume the output once so a truncated gzip cannot be published.
        with gzip.open(temp, "rt", encoding="utf-8") as check:
            if next(check).rstrip("\r\n").split(";") != FIELDS:
                raise ValueError("Hybrid header failed round-trip")
            output_rows = sum(1 for _ in check)
        if output_rows != old_count + new_count:
            raise ValueError("Hybrid gzip row count failed round-trip")
        temp.replace(output)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    audit = {
        "status": "ok", "country": country, "policy": lock["version"],
        "old_master": str(old_master.resolve()), "old_master_sha256": info["old_master_sha256"],
        "new_partial_master": str(new_partial.resolve()), "new_partial_sha256": sha256(new_partial),
        "assignments_sha256": lock["assignments_sha256"],
        "hybrid_master": str(output.resolve()), "hybrid_sha256": sha256(output),
        "cells": cells, "unchanged_cells": cells-len(changed), "replaced_cells": len(changed),
        "unchanged_rows": old_count, "new_rows": new_count,
        "total_rows": old_count+new_count,
        "unchanged_rows_copied_verbatim": True,
        "new_rows_paired_to_old_calendar_weather_planting_and_area": True,
    }
    audit_path.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(f"HYBRID COMPLETE {country}: {cells} cells, {new_count} new rows, {old_count} reused rows -> {output}")
    return audit


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("old_master", type=Path)
    p.add_argument("new_partial_master", type=Path)
    p.add_argument("assignments_csv", type=Path)
    p.add_argument("lock_json", type=Path)
    p.add_argument("country", choices=("Brazil", "Bolivia"))
    p.add_argument("output", type=Path)
    p.add_argument("audit", type=Path)
    a = p.parse_args()
    merge(a.old_master, a.new_partial_master, a.assignments_csv, a.lock_json,
          a.country, a.output, a.audit)


if __name__ == "__main__":
    main()
