#!/usr/bin/env python3
"""Research pilot for Beruski et al. (2020) DSV50 rainfall warnings.

This is deliberately separate from DISMO production. It needs daily rainfall
and an independently determined crop eligibility flag. It does not infer
fungicide efficacy, and it does not force agreement with simulated yield loss.

Conventions requiring field-data validation: the 30 prior days exclude today;
the 3-day moving mean is compared with the action threshold; a spray protects
the next 14 complete days; at most 6 sprays are *reported*. The cap is a new
study protocol, not a threshold specified in the article.
"""

from collections import deque
from dataclasses import dataclass
import math
from typing import Sequence


THRESHOLDS = (50.0, 45.0, 35.0, 30.0)
RESIDUAL_DAYS = 14
MAX_SPRAYS = 6


@dataclass(frozen=True)
class Day:
    rain_mm: float
    eligible: bool


@dataclass(frozen=True)
class WarningResult:
    spray_day_indices: tuple[int, ...]
    uncapped_spray_count: int
    censored_recommendations: int
    eligible_risk_days: int
    daily_dsv: tuple[float | None, ...]
    rolling_dsv: tuple[float | None, ...]


def partial_dsv(rains: Sequence[float]) -> float:
    if len(rains) != 15:
        raise ValueError("A partial DSV needs exactly 15 prior days")
    wet = [value for value in rains if value > 0.5]
    return -2.1433 + 0.3622 * sum(wet) + 2.573 * len(wet)


def warning_schedule(days: Sequence[Day]) -> WarningResult:
    rain_history = deque(maxlen=30)
    dsv_history = deque(maxlen=3)
    spray_indices = []
    daily = []
    rolling = []
    eligible_risk = 0
    uncapped_count = 0
    last_spray = None
    for index, day in enumerate(days):
        if not math.isfinite(day.rain_mm) or day.rain_mm < 0:
            raise ValueError(f"Invalid rain at day {index}: {day.rain_mm}")
        value = None
        three_day = None
        if len(rain_history) == 30:
            prior = tuple(rain_history)
            value = (0.7 * partial_dsv(prior[15:])
                     + 0.3 * partial_dsv(prior[:15]))
            dsv_history.append(value)
            if len(dsv_history) == 3:
                three_day = sum(dsv_history) / 3.0
        daily.append(value)
        rolling.append(three_day)
        if day.eligible and three_day is not None:
            threshold = THRESHOLDS[min(uncapped_count, len(THRESHOLDS) - 1)]
            if three_day >= threshold:
                eligible_risk += 1
                if last_spray is None or index - last_spray > RESIDUAL_DAYS:
                    if uncapped_count < MAX_SPRAYS:
                        spray_indices.append(index)
                    uncapped_count += 1
                    last_spray = index
        rain_history.append(day.rain_mm)
    return WarningResult(tuple(spray_indices), uncapped_count,
                         max(uncapped_count - MAX_SPRAYS, 0),
                         eligible_risk, tuple(daily), tuple(rolling))
