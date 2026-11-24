#!/usr/bin/env python3
"""Replay the isolated DSV50 pilot on one DSSAT Weather.OUT/DISMO.OUT pair.

This is a research diagnostic, not a replacement for the existing model or a
global campaign launcher. Weather.OUT supplies daily rain (PRED); DISMO.OUT
supplies the crop window (DAE > 0 and healthy LAI >= 0.1). A full 32 days of
weather before the first eligible day is required for the 30-day rain window
and its three-day moving mean. Dates and coverage are checked strictly.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import math
from pathlib import Path

from dsv50_warning_pilot import Day, warning_schedule


def _day(year: str, doy: str) -> date:
    year_number = int(year)
    ordinal = int(doy)
    stamp = date(year_number, 1, 1) + timedelta(days=ordinal - 1)
    if ordinal < 1 or stamp.year != year_number:
        raise ValueError(f"Invalid year/day-of-year: {year}/{doy}")
    return stamp


def read_weather(path: Path) -> list[tuple[date, float]]:
    is_wth = path.suffix.upper() == ".WTH"
    columns = None
    result: list[tuple[date, float]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("@") and (
            (is_wth and "DATE" in raw) or (not is_wth and "YEAR" in raw)
        ):
            columns = raw.lstrip("@").split()
            required = {"DATE", "RAIN"} if is_wth else {"YEAR", "DOY", "PRED"}
            if not required <= set(columns):
                raise ValueError(f"Weather file lacks {required}: {path}")
            continue
        fields = raw.split()
        if not fields or not fields[0].isdigit() or columns is None:
            continue
        if is_wth:
            token = fields[columns.index("DATE")]
            if len(token) == 7:
                year, doy = token[:4], token[4:]
            elif len(token) == 5:
                yy = int(token[:2])
                year, doy = str(1900 + yy if yy >= 80 else 2000 + yy), token[2:]
            else:
                raise ValueError(f"Unexpected WTH date {token!r}")
            stamp = _day(year, doy)
            rain_index = columns.index("RAIN")
        else:
            stamp = _day(fields[columns.index("YEAR")], fields[columns.index("DOY")])
            rain_index = columns.index("PRED")
        if len(fields) <= rain_index:
            raise ValueError(f"Short weather daily row: {raw}")
        if result and stamp != result[-1][0] + timedelta(days=1):
            raise ValueError(f"Weather dates are not daily and consecutive at {stamp}")
        result.append((stamp, float(fields[rain_index])))
    if not result:
        raise ValueError(f"No daily weather in {path}")
    return result


def read_crop(path: Path) -> tuple[dict[date, bool], int]:
    columns = None
    result: dict[date, bool] = {}
    old_sprays = 0
    previous = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("@YEAR"):
            columns = raw.lstrip("@").split()
            if not {"YEAR", "DOY", "DAE", "LAIH", "NSPRAYS"} <= set(columns):
                raise ValueError(f"DISMO.OUT lacks required columns: {path}")
            continue
        fields = raw.split()
        if not fields or not fields[0].isdigit() or columns is None:
            continue
        stamp = _day(fields[columns.index("YEAR")], fields[columns.index("DOY")])
        if previous and stamp != previous + timedelta(days=1):
            raise ValueError(f"DISMO.OUT dates are not daily and consecutive at {stamp}")
        previous = stamp
        dae = int(fields[columns.index("DAE")])
        healthy_lai = float(fields[columns.index("LAIH")])
        if not math.isfinite(healthy_lai) or healthy_lai < 0:
            raise ValueError(f"Invalid healthy LAI at {stamp}: {healthy_lai}")
        result[stamp] = dae > 0 and healthy_lai >= 0.1
        old_sprays = int(float(fields[columns.index("NSPRAYS")]))
    if not result:
        raise ValueError(f"No daily crop output in {path}")
    return result, old_sprays


def replay(weather_path: Path, crop_path: Path) -> dict:
    weather = read_weather(weather_path)
    crop, old_sprays = read_crop(crop_path)
    weather_dates = {stamp for stamp, _ in weather}
    missing = sorted(set(crop) - weather_dates)
    if missing:
        raise ValueError(f"Weather.OUT misses {len(missing)} crop days; first {missing[0]}")
    eligible_indices = [i for i, (stamp, _) in enumerate(weather) if crop.get(stamp, False)]
    if eligible_indices and eligible_indices[0] < 32:
        raise ValueError(
            f"Only {eligible_indices[0]} prior weather days before first eligible day; "
            "DSV50 requires at least 32"
        )
    scheduled = warning_schedule(
        [Day(rain, crop.get(stamp, False)) for stamp, rain in weather]
    )
    return {
        "weather": str(weather_path),
        "crop": str(crop_path),
        "weather_days": len(weather),
        "eligible_crop_days": len(eligible_indices),
        "existing_dismo_sprays": old_sprays,
        "dsv50_sprays_capped_at_6": len(scheduled.spray_day_indices),
        "dsv50_sprays_without_cap": scheduled.uncapped_spray_count,
        "recommendations_censored_by_cap": scheduled.censored_recommendations,
        "spray_dates": [weather[i][0].isoformat() for i in scheduled.spray_day_indices],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("weather_out", type=Path)
    parser.add_argument("dismo_out", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.weather_out, args.dismo_out), indent=2))


if __name__ == "__main__":
    main()
