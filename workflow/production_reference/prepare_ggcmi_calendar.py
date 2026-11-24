#!/usr/bin/env python3
"""Sample GGCMI crop-calendar NetCDF values onto Pythia point sites.

The NetCDF time coordinate is deliberately opened with decode_times=False because
GGCMI uses a non-standard "years since 1601" encoding.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import fiona
import numpy as np
import xarray as xr


def years_from_time(values: np.ndarray, units: str) -> np.ndarray:
    match = re.search(r"years? since\s+(\d{4})", units, flags=re.IGNORECASE)
    if not match:
        raise ValueError(f"Unsupported GGCMI time units: {units!r}")
    return int(match.group(1)) + np.rint(values).astype(int)


def aggregate(values: np.ndarray, method: str) -> np.ndarray:
    valid = np.where(np.isfinite(values), values, np.nan)
    present = np.isfinite(valid).any(axis=0)
    result = np.full(valid.shape[1], np.nan)
    if method == "median":
        result[present] = np.nanmedian(valid[:, present], axis=0)
    elif method == "mean":
        result[present] = np.nanmean(valid[:, present], axis=0)
    else:
        raise ValueError(f"Unsupported aggregation: {method}")
    return result


def circular_aggregate_doy(values: np.ndarray, method: str, period: int = 366) -> np.ndarray:
    """Aggregate day-of-year values without breaking dates around New Year.

    A linear median/mean treats, for example, days 360 and 5 as being roughly
    six months apart.  The GGCMI planting and maturity dates are cyclical, so
    they need a circular statistic.  ``period=366`` preserves the valid DSSAT
    / GGCMI day-of-year domain and keeps day 366 adjacent to day 1.
    """
    result = np.full(values.shape[1], np.nan, dtype=float)
    for site_index in range(values.shape[1]):
        sample = values[:, site_index]
        sample = sample[np.isfinite(sample)]
        if not len(sample):
            continue
        if method == "mean":
            angles = 2 * np.pi * (sample - 1) / period
            angle = np.arctan2(np.sin(angles).mean(), np.cos(angles).mean())
            result[site_index] = (angle % (2 * np.pi)) * period / (2 * np.pi) + 1
        elif method == "median":
            # A circular median is an observed date minimizing total circular
            # distance.  It is stable and does not invent a date in mid-year.
            candidates = np.unique(sample)
            distances = np.abs(candidates[:, None] - sample[None, :])
            distances = np.minimum(distances, period - distances)
            result[site_index] = candidates[np.argmin(distances.sum(axis=1))]
        else:
            raise ValueError(f"Unsupported aggregation: {method}")
    return result


def enrich_sites(
    sites: Path,
    calendar: Path,
    output: Path,
    start_year: int,
    end_year: int,
    stat: str = "median",
    *,
    require_maturity: bool = True,
) -> int:
    """Attach a GGCMI calendar (or one exact calendar year) to point sites.

    This is deliberately reusable by the scratch configuration preparer.  In
    annual mode ``start_year == end_year``: no climatological aggregation is
    performed, but the same strict coordinate and validity checks are applied.
    """
    if end_year < start_year:
        raise ValueError("end_year must be greater than or equal to start_year")
    with fiona.open(sites) as source:
        features = list(source)
        schema, crs = source.schema.copy(), source.crs
    if not features:
        raise ValueError("Sites file is empty")
    lons = xr.DataArray([feature["geometry"]["coordinates"][0] for feature in features], dims="site")
    lats = xr.DataArray([feature["geometry"]["coordinates"][1] for feature in features], dims="site")
    with xr.open_dataset(calendar, decode_times=False) as ds:
        needed = {"planting_day", "maturity_day", "growing_period"}
        missing = needed - set(ds.data_vars)
        if missing:
            raise ValueError(f"Calendar missing variables: {sorted(missing)}")
        years = years_from_time(ds["time"].values, str(ds["time"].attrs.get("units", "")))
        selected = (years >= start_year) & (years <= end_year)
        if set(range(start_year, end_year + 1)) - set(years):
            available = f"{int(years.min())}..{int(years.max())}" if len(years) else "none"
            raise ValueError(
                f"No GGCMI years in {start_year}..{end_year}; available years are {available} in {calendar}"
            )
        sampled = ds[["planting_day", "maturity_day", "growing_period"]].isel(time=selected).sel(
            lon=lons, lat=lats, method="nearest"
        )
        planting = sampled["planting_day"].transpose("time", "site").values.astype(float)
        maturity = sampled["maturity_day"].transpose("time", "site").values.astype(float)
        growing = sampled["growing_period"].transpose("time", "site").values.astype(float)
    planting[(planting < 1) | (planting > 366)] = np.nan
    maturity[(maturity < 1) | (maturity > 366)] = np.nan
    growing[growing <= 0] = np.nan
    pday = circular_aggregate_doy(planting, stat)
    mday = circular_aggregate_doy(maturity, stat)
    gdays = aggregate(growing, stat)
    output.parent.mkdir(parents=True, exist_ok=True)
    schema["properties"] = dict(schema["properties"])
    schema["properties"].update({"PLANT_DOY": "int", "MAT_DOY": "int", "GROW_DAYS": "float", "CAL_YR0": "int", "CAL_YRN": "int", "CAL_N": "int"})
    valid_count = 0
    with fiona.open(output, "w", driver="GPKG", schema=schema, crs=crs, layer="sites") as target:
        for index, feature in enumerate(features):
            if not np.isfinite(pday[index]) or (require_maturity and (
                not np.isfinite(gdays[index]) or not np.isfinite(mday[index])
            )):
                lon, lat = feature["geometry"]["coordinates"]
                raise ValueError(f"No valid GGCMI calendar at point ({lat}, {lon})")
            props = dict(feature["properties"])
            props.update({
                "PLANT_DOY": int(round(pday[index])),
                "MAT_DOY": int(round(mday[index])) if np.isfinite(mday[index]) else -99,
                "GROW_DAYS": float(gdays[index]) if np.isfinite(gdays[index]) else -99.0,
                "CAL_YR0": start_year,
                "CAL_YRN": end_year,
                "CAL_N": int(selected.sum()),
            })
            target.write({"type": "Feature", "geometry": feature["geometry"], "properties": props})
            valid_count += 1
    return valid_count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sites", type=Path, help="Point GeoPackage from build_sites.py")
    parser.add_argument("calendar", type=Path, help="GGCMI crop-calendar NetCDF")
    parser.add_argument("output", type=Path, help="Calendar-enriched Point GeoPackage")
    parser.add_argument("--start-year", type=int, default=1991)
    parser.add_argument("--end-year", type=int, default=2014)
    parser.add_argument("--stat", choices=["median", "mean"], default="median")
    args = parser.parse_args()
    valid_count = enrich_sites(
        args.sites,
        args.calendar,
        args.output,
        args.start_year,
        args.end_year,
        args.stat,
    )
    print(f"Calendar sampled for {valid_count} sites ({args.start_year}-{args.end_year}, {args.stat}) -> {args.output}")


if __name__ == "__main__":
    main()
