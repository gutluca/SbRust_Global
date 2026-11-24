#!/usr/bin/env python3
"""Index DSSAT WTH files by the coordinates in their header.

The resulting CSV is the authoritative CellID map.  It avoids assuming that
the numeric WTH filename follows a particular global-grid formula.
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path


def coordinates_from_wth(path: Path) -> tuple[float, float]:
    """Read only the WTH header, never the daily weather records.

    A global WTH collection contains many years of daily records per file.
    Reading the whole file merely to find its latitude/longitude turns site
    indexing into a multi-hour filesystem operation.
    """
    with path.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            if line.lstrip().upper().startswith("@ INSI"):
                for _ in range(4):
                    values = next(source, "")
                    fields = values.split()
                    if len(fields) >= 3:
                        return float(fields[1]), float(fields[2])
    raise ValueError("missing '@ INSI LAT LONG' header")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("weather_dir", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument("--recursive", action="store_true")
    args = parser.parse_args()
    files = args.weather_dir.rglob("*.WTH") if args.recursive else args.weather_dir.glob("*.WTH")
    files = sorted(files)
    if not files:
        raise SystemExit(f"No .WTH files found in {args.weather_dir}")
    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    invalid, duplicate, count, keys = 0, 0, 0, set()
    with args.output_csv.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=["CellID", "LATITUDE", "LONGITUDE", "WTH_FILE"])
        writer.writeheader()
        for position, wth in enumerate(files, start=1):
            if not re.fullmatch(r"\d+", wth.stem):
                invalid += 1
                continue
            try:
                lat, lon = coordinates_from_wth(wth)
            except (OSError, ValueError) as exc:
                print(f"SKIP {wth}: {exc}")
                invalid += 1
                continue
            key = (round(lat, 5), round(lon, 5))
            if key in keys:
                duplicate += 1
                continue
            keys.add(key)
            writer.writerow({"CellID": int(wth.stem), "LATITUDE": f"{lat:.5f}", "LONGITUDE": f"{lon:.5f}", "WTH_FILE": wth.name})
            count += 1
            if position % 1_000 == 0:
                target.flush()
                print(f"Indexed {count} of {len(files)} WTH files...", flush=True)
    print(f"Indexed {count} WTH files -> {args.output_csv}; skipped={invalid}, duplicate coordinates={duplicate}")


if __name__ == "__main__":
    main()
