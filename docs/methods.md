# Study methods

## Spatial support and experiment

Rainfed soybean harvested area from SPAM 2020 V2r2 is aggregated from the native 5 arcmin raster to 0.5° country–cell fragments. The retained study mask covers 103 countries, 16,612 country–cell fragments and 15,765 distinct coordinate pairs, representing 117.85 million ha. Harvested area is fixed across periods and scenarios. The coordinates, weather identifiers and area weights are in [soybean_study_mask.csv](../data/spatial/soybean_study_mask.csv); the spatial selection records are in [excluded_country_cells.csv](../data/spatial/excluded_country_cells.csv).

The experiment compares 1981–2014 with 2020–2039 and 2040–2069 under SSP1-2.6, SSP3-7.0 and SSP5-8.5. Five weather GCMs are used: GFDL-ESM4, IPSL-CM6A-LR, MPI-ESM1-2-HR, MRI-ESM2-0 and UKESM1-0-LL. Each country–cell has 4,600 raw paired records: `34×5×5 + 50×3×5×5`. The experiment identifiers and counts are recorded in [study_design.json](../provenance/study_design.json).

## Maturity groups and calendars

Thirteen DSSAT generic maturity groups (000, 00, 0 and 1–10) are evaluated without disease against the 1991–2014 GGCMI reference calendars. Candidate performance is evaluated using maturity-date and growing-period errors and valid-season checks. The final assignment is supplied in [selected_maturity_groups.csv](../data/spatial/selected_maturity_groups.csv), with calibration tables in [data/calibration](../data/calibration) and selection code in [workflow/maturity](../workflow/maturity). The global selection settings and country selection settings are retained in [maturity_extreme_policy.json](../data/calibration/maturity_extreme_policy.json), [gm_policy_Brazil.json](../provenance/gm_policy_Brazil.json) and [gm_policy_Bolivia.json](../provenance/gm_policy_Bolivia.json).

The assigned group is fixed across historical and future simulations, GCMs, sowing alternatives and disease arms. The historical sowing reference is the median of the 1991–2014 GFDL-ESM4 calendar. Future annual sowing dates follow the IPSL-CM6A-LR calendar for the matching SSP, with a common calendar reference across the five weather GCMs. P01–P05 represent −30, −15, 0, +15 and +30 days around the reference sowing date.

## Paired crop and disease simulations

Rainfed CROPGRO-Soybean simulations use standardized management, disabled nutrient limitations and initial soil water at 25% of plant-available water. Disease-free and disease-affected simulations share soil, weather, cultivar and management inputs. The regional disease parameter files and country assignments are supplied in [data/calibration](../data/calibration).

Annual global atmospheric CO₂ concentrations follow [Meinshausen et al. (2020)](https://doi.org/10.5194/gmd-13-3571-2020) for SSP1-2.6, SSP3-7.0 and SSP5-8.5. Within an SSP/year, the same concentration is applied to both disease arms and all five GCMs using DSSAT `CO2048.WDA` (`CO2=M`). Each annual concentration is effective from 1 January. Concentrations, DSSAT inputs and source checksums are supplied in [data/co2](../data/co2).

## Yield processing

The raw country masters retain the original paired records. The analysis excludes a paired row when either yield is nonfinite, disease-free yield is ≤0, disease-affected yield is negative or exceeds disease-free yield, or a stored loss field is nonfinite or negative. Valid zero losses and zero disease-affected yields are retained. This filter retains 74,542,108 of the 76,415,200 raw records. Absolute loss is `Y0−YASR`; relative loss is `100×(Y0−YASR)/Y0`.

Years are averaged within each country–cell, period, SSP, GCM and sowing alternative. All five sowing alternatives are required for a GCM mean. Future and historical means are paired within the same GCM, with all five GCMs required for ensemble comparisons. National and global aggregates use fixed harvested-area weights. Directional agreement of at least four of five GCMs defines a robust change. Country fragments are retained for national accounting; shared coordinates are combined for spatial summaries.

The processing implementation is in [workflow/analysis](../workflow/analysis). Column definitions and units are in the [data dictionary](data_dictionary.md).
