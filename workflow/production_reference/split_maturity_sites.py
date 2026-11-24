#!/usr/bin/env python3
"""Split a country's base-site GPKG into immutable GM-calibration batches."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import fiona


def split_sites(source_path: Path, output_dir: Path, chunk_size: int) -> dict:
    if chunk_size < 1:
        raise ValueError("--chunk-size must be positive")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"Refusing to mix batches with existing files: {output_dir}")

    with fiona.open(source_path) as source:
        schema, crs = source.schema.copy(), source.crs
        features = list(source)
    if not features:
        raise ValueError("Base-site GeoPackage contains no points")
    required = {"CellID", "HAREA", "COUNTRY"}
    if schema["geometry"] != "Point" or not required <= set(schema["properties"]):
        raise ValueError(f"Base sites must be points with {sorted(required)}")
    cell_ids: set[str] = set()
    countries: set[str] = set()
    for feature in features:
        props = dict(feature["properties"])
        cell_id = str(props["CellID"])
        if cell_id in cell_ids:
            raise ValueError(f"Duplicate CellID in base sites: {cell_id}")
        if float(props["HAREA"]) <= 0:
            raise ValueError(f"Non-positive harvested area for CellID {cell_id}")
        cell_ids.add(cell_id)
        countries.add(str(props["COUNTRY"]))
    if len(countries) != 1 or not next(iter(countries)):
        raise ValueError(f"Expected one non-empty country, got {sorted(countries)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[Path] = []
    for offset in range(0, len(features), chunk_size):
        target = output_dir / f"chunk_{len(chunks):04d}.gpkg"
        with fiona.open(target, "w", driver="GPKG", schema=schema, crs=crs, layer="sites") as sink:
            for feature in features[offset : offset + chunk_size]:
                sink.write(feature)
        chunks.append(target.resolve())
    manifest = output_dir / "manifest.txt"
    manifest.write_text("\n".join(path.as_posix() for path in chunks) + "\n", encoding="utf-8")
    audit = {
        "status": "ok", "country": next(iter(countries)),
        "base_sites": str(source_path.resolve()), "site_count": len(features),
        "chunk_size": chunk_size, "chunk_count": len(chunks),
        "manifest": str(manifest.resolve()),
    }
    (output_dir / "split_audit.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_sites", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--chunk-size", type=int, default=25)
    args = parser.parse_args()
    audit = split_sites(args.base_sites, args.output_dir, args.chunk_size)
    print(f"Split {audit['site_count']} {audit['country']} sites into {audit['chunk_count']} batches of at most {audit['chunk_size']}: {audit['manifest']}")


if __name__ == "__main__":
    main()
