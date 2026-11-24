#!/usr/bin/env python3
"""Create a NEW country project with one explicit calibrated DISMO input."""
import argparse
import copy
import csv
import hashlib
import json
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assign(project, country, output, executable, registry):
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    with (registry / "country_assignment.csv").open(encoding="utf-8", newline="") as f:
        records = list(csv.DictReader(f, delimiter=";"))
    names = [r["country"] for r in records]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate country assignments")
    selected = [r for r in records if r["country"] == country]
    if len(selected) != 1:
        raise ValueError(f"Country must have one explicit assignment: {country}")
    selected = selected[0]
    if selected["parameter_set"] not in {"Brazil", "UnitedStates"}:
        raise ValueError("Unknown parameter set")
    input_file = registry / selected["parameter_set"] / "disease_parameters.txt"
    if not input_file.is_file() or not executable.is_file():
        raise FileNotFoundError("Executable or selected input missing")
    result = copy.deepcopy(json.loads(project.read_text(encoding="utf-8")))
    result["experiment"]["country"] = country
    result["profiles"]["hipergator"]["disease_parameters"] = str(input_file.resolve()).replace("\\", "/")
    result["profiles"]["hipergator"]["dssat_executable"] = str(executable.resolve()).replace("\\", "/")
    result["experiment"]["regional_parameter_assignment"] = {
        **selected, "input_sha256": sha(input_file), "executable_sha256": sha(executable),
        "registry_sha256": sha(registry / "country_assignment.csv")}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("project", type=Path)
    p.add_argument("country")
    p.add_argument("output", type=Path)
    p.add_argument("executable", type=Path)
    p.add_argument("--registry", type=Path, default=Path(__file__).resolve().parents[1]/"config/regional_inputs_20261022")
    args = p.parse_args()
    result = assign(args.project, args.country, args.output, args.executable, args.registry)
    print(json.dumps(result["experiment"]["regional_parameter_assignment"], indent=2))
