#!/usr/bin/env python3
"""Replace Pythia weather symlinks with a year-windowed WTH cache in scratch.

DSSAT experiment dates have a two-digit year, while the project WTH files span
many decades.  Leaving the complete WTH in a run directory can therefore make
DSSAT select an earlier century with the same two-digit year.  This utility is
run *after* ``pythia --setup`` and before ``pythia --run-dssat``.  It keeps
only the small date window needed by the task, so every two-digit year in the
weather file is unique and the canonical Orange/Blue inputs remain untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path


# ISIMIP WTH collections use two valid DSSAT date encodings. Historical and
# SSP370 files carry YYYYDDD, while SSP126 and SSP585 use YYDDD (80..99 map
# to 1980..1999; 00..79 map to 2000..2079). The canonical source files are
# never changed; short records are normalised only in each scratch copy.
DATE = re.compile(r"^\s*(?P<date>\d{7}|\d{5})\b")


def record_year(match: re.Match[str]) -> int:
    """Return the Gregorian year represented by a DSSAT WTH date token."""
    token = match.group("date")
    if len(token) == 7:
        return int(token[:4])
    yy = int(token[:2])
    return 1900 + yy if yy >= 80 else 2000 + yy


def weather_span(path: Path) -> tuple[int, int] | None:
    """Read WTH date bounds with the same date formats as the task runner.

    Short-date SSP collections can end at 2100 (token 00DDD). Anchor the
    trailing two-digit year to the first year, assuming a span below 100
    years. This is an inventory check; materialize_source still verifies
    the actual years needed by every task before DSSAT runs.
    """
    with path.open("rb") as source:
        head = source.read(8192).decode("utf-8", errors="replace")
        source.seek(0, 2)
        offset = max(0, source.tell() - 8192)
        source.seek(offset)
        if offset:
            source.readline()  # Discard a possible partial record.
        tail = source.read().decode("utf-8", errors="replace")
    first_match = next((m for line in head.splitlines() if (m := DATE.match(line))), None)
    last_match = next((m for line in reversed(tail.splitlines()) if (m := DATE.match(line))), None)
    if first_match is None or last_match is None:
        return None
    first = record_year(first_match)
    token = last_match.group("date")
    last = record_year(last_match)
    if len(token) == 5:
        last = (first // 100) * 100 + int(token[:2])
        if last < first:
            last += 100
    return first, last


def required_window(config: dict) -> tuple[int, int]:
    pipeline = config.get("pipeline", {})
    purpose = str(pipeline.get("purpose") or "")
    if purpose == "inverse_maturity_ensemble_member":
        years = [int(value) for value in pipeline.get("calibration_years", [])]
        if not years:
            raise ValueError("Inverse-calibration configuration has no pipeline.calibration_years")
        return min(years) - 1, max(years) + 1
    if pipeline.get("annual"):
        year = pipeline.get("simulation_year")
        if year is None:
            raise ValueError("Annual configuration has no pipeline.simulation_year")
        return int(year) - 1, int(year) + 1
    raise ValueError("Weather windowing is restricted to annual production or historical GM-calibration configurations")


def cache_name(source: Path) -> str:
    digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:16]
    return f"{source.stem}_{digest}.WTH"


def expanded_weather_line(line: str, match: re.Match[str], year: int) -> str:
    """Expand an ambiguous YYDDD input date to YYYYDDD in the scratch copy."""
    token = match.group("date")
    if len(token) == 7:
        return line
    start, end = match.span("date")
    return f"{line[:start]}{year:04d}{token[2:]}{line[end:]}"


def expanded_weather_header(line: str) -> str:
    """Write headers consistent with a seven-digit DSSAT scratch WTH.

    DSSAT 4.8's ``MAKEFILEW`` enables its YYYYDDD reader only after seeing
    ``$WEATHER``. GGCMI SSP126/585 WTH files instead begin with ``*WEATHER``;
    expanding their records without changing that marker still makes DSSAT
    read just the first five columns and shift 2036 to 1936. This operates
    solely on the scratch cache and never mutates the canonical source WTH.
    """
    if line.startswith("*WEATHER"):
        return f"$WEATHER{line[len('*WEATHER'):]}"
    if line.startswith("@DATE"):
        return f"@  DATE{line[len('@DATE'):]}"
    return line


def materialize_source(source: Path, target: Path, first_year: int, last_year: int) -> tuple[int, list[int]]:
    """Write WTH headers plus only daily records in an inclusive year range.

    Short ``YYDDD`` source records are expanded to ``YYYYDDD`` in the scratch
    file and marked as ``$WEATHER`` so DSSAT activates the matching reader.
    Both transformations are required: with a five-digit reader, ``2036001``
    is read as ``20360`` and is consequently interpreted as 1936.
    """
    header: list[str] = []
    daily: list[str] = []
    observed: set[int] = set()
    seen_daily = False
    short_dates = False
    with source.open("r", encoding="utf-8", errors="replace") as input_file:
        for line in input_file:
            match = DATE.match(line)
            if match:
                seen_daily = True
                short_dates |= len(match.group("date")) == 5
                year = record_year(match)
                if first_year <= year <= last_year:
                    daily.append(expanded_weather_line(line, match, year))
                    observed.add(year)
            elif not seen_daily:
                header.append(line)
    expected = set(range(first_year, last_year + 1))
    missing = sorted(expected - observed)
    if missing:
        raise ValueError(f"WTH {source} lacks required year(s) {missing} for the task weather window")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as output_file:
        output_file.writelines(
            expanded_weather_header(line) if short_dates else line
            for line in header
        )
        output_file.writelines(daily)
    temporary.replace(target)
    return len(daily), sorted(observed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path, help="Task-local Pythia configuration already used for --setup")
    parser.add_argument("work_dir", type=Path, help="Pythia workDir created by --setup")
    parser.add_argument("cache_dir", type=Path, help="Node-local cache directory for shortened WTH files")
    parser.add_argument("--audit", type=Path, required=True)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    first_year, last_year = required_window(config)
    run_names = {str(run.get("name") or "") for run in config.get("runs", [])}
    if not run_names or "" in run_names:
        raise SystemExit("Configuration has no valid run names")
    if not args.work_dir.is_dir():
        raise SystemExit(f"Pythia setup workDir is missing: {args.work_dir}")

    links: list[Path] = []
    for run_name in sorted(run_names):
        run_dir = args.work_dir / run_name
        if not run_dir.is_dir():
            raise SystemExit(f"Pythia did not create run directory: {run_dir}")
        links.extend(path for path in run_dir.rglob("*.WTH") if path.name.upper() == "WTH0.WTH")
    if not links:
        raise SystemExit("Pythia setup produced no WTH0.WTH files to window")

    sources: dict[Path, Path] = {}
    records: dict[str, dict[str, object]] = {}
    links_per_source: Counter[str] = Counter()
    for link in links:
        if not link.is_symlink():
            raise SystemExit(f"Expected Pythia weather symlink, found a regular file: {link}")
        source = link.resolve(strict=True)
        if source.suffix.upper() != ".WTH":
            raise SystemExit(f"Pythia weather link does not resolve to a WTH file: {link} -> {source}")
        cached = sources.get(source)
        if cached is None:
            cached = args.cache_dir / cache_name(source)
            if cached.exists():
                raise SystemExit(f"Refusing to reuse an existing scratch weather cache file: {cached}")
            count, years = materialize_source(source, cached, first_year, last_year)
            sources[source] = cached
            records[str(source)] = {"cache": str(cached), "daily_records": count, "years": years}
        link.unlink()
        os.symlink(cached, link)
        links_per_source[str(source)] += 1

    args.audit.parent.mkdir(parents=True, exist_ok=True)
    args.audit.write_text(json.dumps({
        "schema_version": 1,
        "window": {"first_year": first_year, "last_year": last_year},
        "weather_sources": records,
        "wth_links_replaced": len(links),
        "links_per_source": dict(links_per_source),
    }, indent=2) + "\n", encoding="utf-8")
    print(
        f"Weather window materialized: {len(sources)} source WTH file(s), "
        f"{len(links)} run links, years {first_year}..{last_year}"
    )


if __name__ == "__main__":
    main()
