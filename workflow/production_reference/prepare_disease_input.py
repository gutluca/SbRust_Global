#!/usr/bin/env python3
"""Snapshot one calibrated DISMO input without changing any parameter value.

``disease_parameters.txt`` is a calibrated model input.  In particular, NDS
is no longer an experiment-level inoculum scenario: its calibrated value is
the daily ceiling used by DISMO.  This module therefore treats the complete
file as immutable bytes and verifies the copy with SHA-256.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_disease_input(source: Path, output_dir: Path) -> Path:
    """Copy the calibrated input byte-for-byte and return the absolute path."""
    if source.name != "disease_parameters.txt":
        raise ValueError("The DISMO input must be named disease_parameters.txt")
    if not source.is_file():
        raise ValueError(f"DISMO input is not readable: {source}")
    source_bytes = source.read_bytes()
    if not source_bytes:
        raise ValueError(f"DISMO input is empty: {source}")

    target = output_dir / "disease_parameters.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source_bytes)
    if sha256(target) != sha256(source):
        raise RuntimeError(f"DISMO input snapshot differs from its source: {target}")
    return target.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    target = prepare_disease_input(args.source, args.output_dir)
    print(f"{target}  sha256={sha256(target)}")


if __name__ == "__main__":
    main()
