# Data dictionary

## Spatial tables

All CSV files use UTF-8. Compact public tables use commas; original master CSVs are gzip compressed and **semicolon-delimited**. Read maturity-group identifiers as strings so `000`, `00` and `0` remain distinct.

| Column | Meaning |
| --- | --- |
| COUNTRY | Study country name; retain the exact spelling for joins |
| CELL_ID | Stable study/weather-grid cell identifier; only unique together with COUNTRY |
| WEATHER_SOURCE_CELL_ID | Identifier of the weather series actually used |
| LATITUDE, LONGITUDE | Coordinates of the simulation support (degrees) |
| HARVEST_AREA_HA | Fixed rainfed soybean harvested area of the country–cell fragment (ha) |
| GM | Executed DSSAT generic maturity-group label; string |
| GM_SOURCE | Source record for the executed maturity-group assignment |

`soybean_study_mask.csv` contains the retained coordinates, area weights and weather identifiers. `selected_maturity_groups.csv` adds the executed group and its source record. `excluded_country_cells.csv` records spatial selection decisions and candidate labels; use the selected-maturity-group table for assignments to retained cells.

## Original paired masters

Each row contains the two outcomes from matched disease-free and disease-affected simulations.

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
| TOTAL_SPRAYS | Spray counter recorded in the paired yield simulations; DSV50/DSV80 demand is calculated separately by the application workflow |
| DISEASE_ONSET_DAE | Reported disease onset in days after emergence; nonfinite/negative values are treated as unreported in the yield analysis |

The raw files preserve all original records. Yield analysis applies the validity filter in [methods.md](methods.md#yield-processing). Checksums identify the original compressed file bytes.

## Processing outputs

The processing code derives disease-free yield (Y0), disease-affected yield (YASR), absolute loss (LOSS = Y0 - YASR) and relative loss (R = 100 × LOSS / Y0), applies the common validity filter and produces cell, climate-member, country and global aggregates.

`country_parameter_sets.csv` associates each country with its regional disease parameter set, using the input hashes in the country master manifests. The disease-validation CSVs contain per-curve calibration and validation diagnostics.
