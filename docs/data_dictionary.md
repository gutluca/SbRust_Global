# Data dictionary

## Spatial tables

All CSV files use UTF-8. Compact public tables use commas; original master CSVs are gzip compressed and **semicolon-delimited**. Read maturity-group identifiers as strings so `000`, `00` and `0` remain distinct.

| Column | Meaning |
| --- | --- |
| COUNTRY | Study country name; retain the exact spelling for joins |
| CELL_ID | Stable study/weather-grid cell identifier; only unique together with COUNTRY |
| WEATHER_SOURCE_CELL_ID | Identifier of the weather series actually used |
| LATITUDE, LONGITUDE | Coordinates recorded by the simulation support (degrees); do not assume they recover the original native soil-selection point |
| HARVEST_AREA_HA | Fixed rainfed soybean harvested area of the country–cell fragment (ha) |
| GM | Executed DSSAT generic maturity-group label; string |
| GM_SOURCE | Final global selection or executed Brazil/Bolivia revision |

`soybean_study_mask.csv` contains coordinates, area and weather identifiers, without assigning its own raster geometry. `selected_maturity_groups.csv` adds the executed group and source. `excluded_country_cells.csv` preserves original policy/review labels and old/new candidate labels; its candidate choices are not the executed assignment for retained cells.

## Original paired masters

Each row contains the two outcomes from matched disease arms. A separate master per disease arm is unnecessary and would make pairing harder to verify.

| Column | Meaning / unit |
| --- | --- |
| SIMULATION_ID | Compound experiment identifier |
| COUNTRY, CELL_ID | Country–cell fragment key |
| WEATHER_SOURCE_CELL_ID | Actual weather-series identifier |
| WEATHER_BORROWED | Borrowed-weather indicator (0/1) |
| WEATHER_DISTANCE_KM | Weather-donor distance (km) |
| LATITUDE, LONGITUDE, HARVEST_AREA_HA | Spatial support and fixed area |
| MATURITY_GROUP | Executed generic group; read as string |
| PERIOD | 1981-2014, 2020-2039 or 2040-2069 |
| SIM_YEAR | Experiment's sowing year |
| SCENARIO | historical, ssp126, ssp370 or ssp585 |
| GCM | Climate-model name |
| PLANTING_LABEL | P01/P02/P03/P04/P05 = −30/−15/0/+15/+30 days |
| PLANT_DATE, MATURITY_DATE | Recorded crop dates |
| YIELD_NO_DISEASE_KG_HA | Disease-free yield (kg ha⁻¹) |
| YIELD_WITH_DISEASE_KG_HA | Disease-affected yield (kg ha⁻¹) |
| LOSS_KG_HA | Difference between matched yields (kg ha⁻¹) |
| LOSS_PCT | Loss relative to disease-free yield (%) |
| TOTAL_SPRAYS | Legacy paired-master output; not the corrected DSV application campaign |
| DISEASE_ONSET_DAE | Reported disease onset in days after emergence; nonfinite/negative values are treated as unreported in the yield analysis |

Raw records can fail the documented analysis filter; raw files retain those values. Missingness and invalidity must not be silently replaced by zero. Checksums refer to the original compressed file bytes.

## Processing outputs

The raw masters are preserved unchanged. The processing code derives disease-free yield (Y0), disease-affected yield (YASR), absolute loss (LOSS = Y0 - YASR) and relative loss (R = 100 × LOSS / Y0), applies the common validity filter and produces cell, climate-member, country and global aggregates. Those derived tables are not supplied as raw simulation outputs.

`country_parameter_sets.csv` is derived from disease-input hashes in the final country master manifests. The two disease parameter sets are regional transfer regimes. The disease-validation CSVs are historical per-curve diagnostics; they are not the original observed/simulated daily curves. Full observation-level provenance remains to be assembled.
