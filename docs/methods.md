# Methods represented by this snapshot

## Spatial support

The input soybean layer is rainfed harvested area, `spam2020_V2r2_global_H_SOYB_R.tif`, at native 5 arcmin resolution. Area is aggregated onto 0.5° country–cell fragments. The native soybean pixel with the greatest harvested area within a fragment supplies the representative point for soil selection. Weather is linked by the study's weather-cell identifier, with borrowing metadata retained in the raw master when applicable. Do not infer soil lookup coordinates from rounded map coordinates: recovering the exact soil assignment requires the frozen site files.

The published mask contains 16,612 retained fragments in 103 countries; its area is constant across experiments. There are 15,765 unique coordinate pairs because national fragments can share weather-grid coordinates. A common mask excludes 280 recorded country–cell cases. The exclusion table preserves the original reason labels, including policy-based decisions; they should not all be interpreted as demonstrated model failures. Raw SPAM input and the derived study mask are distinct products.

## Maturity groups and calendars

Thirteen DSSAT generic groups (000, 00, 0 and 1–10) were evaluated without disease against the 1991–2014 reference crop calendars. Candidate scores, validity checks and selection policy are part of the workflow; the final assignment is not simply an unconstrained minimum error at every point. The global soft-extreme policy allows specified near-optimal alternatives to the extreme groups. The policy thresholds are study choices, not universally validated agronomic constants.

The executed Brazil policy excludes GM10; permits GM9 only north of 10°S; prefers GM8 when its error is within 3 days of GM9; prefers GM7 within 2 days of GM8 at or south of 15°S; and caps the group at GM7 at or south of 20°S. This v3 revision changed 1,082 cells. Bolivia caps the group at GM8 and prefers GM7 within 2 days of GM8; its v2 revision changed 107 cells. Full decisions and candidate scores for these two countries are included. Their source-lock files retain the original campaign records: Brazil's entries in the Bolivia v2 source lock describe an earlier Brazil alternative, not the final Brazil map. Use `selected_maturity_groups.csv` as the executed global assignment.

A group's identity is fixed across historical/future scenarios and disease arms. The historical sowing reference is the median of the 1991–2014 GFDL-ESM4 calendar. Future annual sowing dates come from the IPSL-CM6A-LR calendar for the matching SSP; within an SSP/year that reference is common across all five weather GCMs. P01–P05 are −30, −15, 0, +15 and +30 days around the reference. The future design therefore includes changing calendar dates but does not simulate future cultivar replacement.

## Model and paired outputs

Rainfed CROPGRO-Soybean simulations use standardized management, disabled nutrient limitations and initial soil water at 25% of plant-available water. Disease-free and disease-affected runs share soil, weather, cultivar and management. The two regional disease parameter files and their actual country assignment are included; country assignment is a transfer assumption, not country-specific validation.

There are five GCMs, five sowing alternatives, 34 historical years and 50 future years under each of three SSPs. Thus each retained country–cell has `34×5×5 + 50×3×5×5 = 4,600` raw paired records. The corrected campaign preserved historical rows from the latest preceding final masters and reran future rows with SSP-specific CO₂.

## CO₂

Annual global atmospheric CO₂ concentrations follow Meinshausen et al. (2020), using the author's supplementary data tables for SSP1-2.6, SSP3-7.0 and SSP5-8.5. The same annual value is applied to both disease arms and all five GCMs within each SSP/year through the local DSSAT `CO2048.WDA` input (`CO2=M`). The future input is an annual step effective on 1 January; historical outputs are preserved. Input provenance and checksums are in `data/co2/provenance.json`; the campaign code checks the CO₂ read back from DSSAT output.

## Yield-quality filtering and aggregation

Raw masters are preserved unchanged. For yield/loss analysis, exclude the whole paired row if either yield is nonfinite, disease-free yield is ≤0, disease-affected yield is negative or exceeds disease-free yield, or either stored loss field is nonfinite/negative. Keep valid zero loss and zero disease-affected yield. This removes 1,873,092 rows and retains 74,542,108. Compute absolute loss as `Y0−YASR` and relative loss as `100×(Y0−YASR)/Y0`; verify these against the stored values.

Average years within each country–cell/period/SSP/GCM/sowing alternative. Require all five sowing alternatives to form a GCM mean. Pair each future GCM mean with the historical mean of that same GCM; require all five GCMs for ensemble comparisons. Weight national and global aggregates by fixed rainfed harvested area. A direction is robust if at least four of five GCMs agree. Merge shared coordinates for spatial maps while retaining country fragments for national accounting. No interpolation is used to invent simulated cells.

`SPRAYS` in the older paired yield masters is a legacy output and must not be interpreted as the corrected DSV50/DSV80 application-demand result.

## Applications: unfinished campaign

The corrected application campaign uses healthy-crop trajectories to define crop eligibility and applies the frozen DSV rules in a separate calculation. Its full code is retained under `workflow/campaigns/onset_dsv`. It is still in progress, so this snapshot includes no corrected application totals or emissions estimates. The older November application campaign is not substituted.

The 24 November controller-recovery helper is retained under `workflow/campaigns/onset_dsv/repairs`. It fixes submission routing and controller recovery; the scientific application rules were not changed by that repair. The executed release after completion still needs to be exported from HiPerGator.

Exact details of onset gating, daily leaf-area eligibility, reset/reapplication intervals and capped/uncapped counts must be reported with the final application archive. Do not describe a literal emergence-to-last-soybean-day window unless the final daily eligibility and onset rules support that wording.
