# SbRust_Global

Code, model inputs and raw paired outputs for global soybean rust simulations with DISMO integrated into DSSAT. This README identifies the files used at each methodological step and provides commands to reanalyse the published outputs.

The study covers 103 countries, 16,612 country–cell fragments and 15,765 distinct locations, representing 117.85 million ha of rainfed soybean. It compares 1981–2014 with 2020–2039 and 2040–2069 under SSP1-2.6, SSP3-7.0 and SSP5-8.5. Five climate models and five sowing-date alternatives are used; harvested area and the assigned maturity group remain fixed across comparisons.

## Start here: reproduce the output analysis

Use Python 3.12 to clone the repository and install the processing dependencies:

```bash
git clone https://github.com/gutluca/SbRust_Global.git
cd SbRust_Global
python3 -m pip install -r requirements.txt
```

The complete Albania master is included as a real-data example:

```bash
python3 workflow/analysis/run_analysis.py --data-root examples/data --countries Albania --output results/example --workers 1
python3 workflow/analysis/check_example.py --output results/example
```

Expected: 17 country–cells, 78,200 raw records and 78,005 retained records. To download and process all 103 countries:

```bash
python3 workflow/download_data.py --output data/raw
python3 workflow/analysis/run_analysis.py --data-root data/raw --output results/global --workers 2
```

For a subset, add `--countries Albania` or `--countries Brazil "United States"` to both commands. Use a new output directory for each analysis. The downloader verifies original file sizes and SHA-256 checksums against [master_files.json](provenance/master_files.json).

## Files and methodological steps

### 1. Define the soybean simulation sites

[soybean_study_mask.csv](data/spatial/soybean_study_mask.csv) supplies the retained country, cell identifier, coordinates, weather-series identifier and rainfed harvested-area weight. [selected_maturity_groups.csv](data/spatial/selected_maturity_groups.csv) adds the executed maturity group and its source record. Use `COUNTRY` and `CELL_ID` together as the spatial key.

Spatial preparation uses [build_sites.py](workflow/production_reference/build_sites.py) and [index_weather.py](workflow/production_reference/index_weather.py). SPAM rainfed soybean area is aggregated from the native 5 arcmin raster onto 0.5° country–cell fragments. Soil and weather assignments are shared within each disease pair.

### 2. Calibrate maturity groups and prepare sowing dates

The four original GGCMI NetCDFs are in [data/calendars](data/calendars):

| Calendar | Role |
| --- | --- |
| GFDL-ESM4, historical 1991–2014 | Maturity-group calibration; the 1991–2014 median supplies the baseline sowing reference |
| IPSL-CM6A-LR, SSP1-2.6, 2015–2100 | Annual future sowing reference for SSP1-2.6 |
| IPSL-CM6A-LR, SSP3-7.0, 2015–2100 | Annual future sowing reference for SSP3-7.0 |
| IPSL-CM6A-LR, SSP5-8.5, 2015–2100 | Annual future sowing reference for SSP5-8.5 |

Thirteen generic DSSAT groups (`000`, `00`, `0`, `1`–`10`) are evaluated without disease against the historical calendar, using maturity-date and growing-period errors and valid-season checks. The final assignments are fixed for subsequent simulations. Candidate definitions, available score tables and selection settings are retained in [data/calibration](data/calibration), [workflow/maturity](workflow/maturity) and the associated [provenance records](provenance).

The sequence is [prepare_country_calendars.py](workflow/production_reference/prepare_country_calendars.py), [generate_maturity_ensemble.py](workflow/production_reference/generate_maturity_ensemble.py), [collate_maturity_ensemble_member.py](workflow/production_reference/collate_maturity_ensemble_member.py) and [select_maturity_ensemble.py](workflow/production_reference/select_maturity_ensemble.py). The [project configuration](workflow/config/project.hipergator.wpavan.annual-calendar.json) records the input structure and management settings.

Within each future SSP/year, the same IPSL calendar reference is applied across all five weather GCMs. P01–P05 represent −30, −15, 0, +15 and +30 days around the reference sowing date. The calibrated maturity group remains fixed.

### 3. Run the paired crop and disease simulations

Rainfed CROPGRO-Soybean runs share weather, soil, cultivar and management between disease-free and disease-affected treatments. Nutrient limitations are disabled and initial soil water is set at 25% of plant-available water. Regional disease parameters and their country assignments are in [data/calibration](data/calibration).

Annual future CO₂ follows the global SSP trajectories of [Meinshausen et al. (2020)](https://doi.org/10.5194/gmd-13-3571-2020). Use the matching `data/co2/<ssp>/CO2048.WDA` with `CO2=M`. Each annual value applies from 1 January and is shared by both disease arms and all weather GCMs within that SSP/year.

The production sequence is:

1. Generate Pythia configurations with [generate_configs.py](workflow/production_reference/generate_configs.py), using the calibrated sites and [SBG00001.SBX](workflow/templates/SBG00001.SBX).
2. Prepare task-local inputs with [prepare_scratch_config.py](workflow/production_reference/prepare_scratch_config.py) and [materialize_weather_window.py](workflow/production_reference/materialize_weather_window.py), then execute DSSAT through Pythia.
3. Collate annual outputs with [collate_annual_task.py](workflow/production_reference/collate_annual_task.py).
4. Assemble country masters with [build_master_summary.py](workflow/production_reference/build_master_summary.py) and check them with [validate_final_summaries.py](workflow/production_reference/validate_final_summaries.py).

The SSP-specific CO₂ campaign is in [workflow/campaigns/co2_future](workflow/campaigns/co2_future). Production launchers retain HiPerGator paths and Slurm conventions; configure paths for the target installation. Model execution uses Linux, Pythia, its geospatial dependencies, daily DSSAT weather files, eGHR soil profiles and site/management configurations. Input sources and filename conventions are recorded in [external_inputs.json](provenance/external_inputs.json). These model inputs are separate from the numerical dependencies needed to process the raw masters.

To assemble the `scripts/`, `templates/` and `config/` layout expected by the model launchers, run `python3 workflow/stage_engine.py --output .model_engine` and use that directory for the engine path in the model configuration.

### 4. Identify the model and runtime tables

The production executable is [model/bin/dscsm048](model/bin/dscsm048), also available as a [direct Linux download](https://github.com/gutluca/SbRust_Global/releases/download/v0.1.0-data/dscsm048-linux-x86_64). Its SHA-256 is:

```text
811a53c4d85d2f47ecf7815d9c46bc75fbc5fe5dbd2dd26aae3fce606f2ba41e
```

[model/runtime](model/runtime) exposes soybean genotype and standard tables from the archived source, including all 13 generic maturity candidates. The source archive and original compilation script are in [model](model). With CMake and a Fortran compiler, build the archived source using:

```bash
mkdir -p .rebuild/source
tar -xzf model/source/dismo_source_20261022.tar.gz -C .rebuild/source
cmake -S .rebuild/source -B .rebuild/build -DCMAKE_BUILD_TYPE=RELEASE -DCMAKE_Fortran_COMPILER=gfortran
cmake --build .rebuild/build --parallel 4
```

This creates a new build of the archived source. The supplied executable and its checksum identify the production binary.

### 5. Calculate application demand

[workflow/campaigns/onset_dsv](workflow/campaigns/onset_dsv) contains healthy-crop execution and DSV50/DSV80 application calculations. Scientific rules are in [beruski_rules.py](workflow/campaigns/onset_dsv/scripts/beruski_rules.py), onset handling in [onset_dsv.py](workflow/campaigns/onset_dsv/scripts/onset_dsv.py) and healthy-crop replay in [world_onset_runtime.py](workflow/campaigns/onset_dsv/scripts/world_onset_runtime.py). `TOTAL_SPRAYS` in the paired yield masters is the counter from those yield simulations; DSV50/DSV80 demand is calculated separately by this workflow.

### 6. Process the paired yields

The [data release](https://github.com/gutluca/SbRust_Global/releases/tag/v0.1.0-data) contains 103 original gzip CSVs, approximately 2.07 GB and 76,415,200 raw paired records. Both disease arms occur in each row. Files are UTF-8 and semicolon-delimited; read maturity-group labels as strings to preserve `000`, `00` and `0`. The complete column dictionary is [data/master_columns.csv](data/master_columns.csv).

The analysis excludes a paired row if either yield is nonfinite, disease-free yield is ≤0, disease-affected yield is negative or exceeds disease-free yield, or a stored loss field is nonfinite or negative. Valid zero losses and zero disease-affected yields are retained. Absolute loss is `Y0 − YASR`; relative loss is `100 × (Y0 − YASR) / Y0`. The global analysis retains 74,542,108 records and preserves the raw inputs unchanged.

Years are averaged within each country–cell, period, SSP, GCM and sowing alternative. All five sowing alternatives are required for a GCM mean. Future and historical means are paired within the same GCM, with all five GCMs required for ensemble comparisons. National and global summaries use fixed harvested-area weights; agreement among at least four of five GCMs defines a robust direction. Country fragments are retained for national accounting, and shared coordinates are combined for spatial summaries. The commands at the start of this README execute this processing.

## Sources, citation and file checks

| Component | Reference |
| --- | --- |
| DISMO integration | [gutluca/dssat-csm-os, dismo-integration](https://github.com/gutluca/dssat-csm-os/tree/dismo-integration) |
| DSSAT code | [DSSAT/dssat-csm-os](https://github.com/DSSAT/dssat-csm-os) |
| SPAM 2020 V2r2 rainfed soybean area | [Source dataset](https://doi.org/10.7910/DVN/SWPENT) |
| GGCMI/ISIMIP calendars | [Calendar documentation and references](https://www.isimip.org/gettingstarted/input-data-bias-adjustment/details/115/) |
| Climate forcing | [ISIMIP data catalogue](https://data.isimip.org/); GFDL-ESM4, IPSL-CM6A-LR, MPI-ESM1-2-HR, MRI-ESM2-0 and UKESM1-0-LL |
| eGHR soils | [Source dataset](https://doi.org/10.7910/DVN/1PEEY0); [Han et al. (2019)](https://doi.org/10.1016/j.envsoft.2019.05.012) |
| Annual SSP CO₂ | [Meinshausen et al. (2020)](https://doi.org/10.5194/gmd-13-3571-2020) |

Use [CITATION.cff](CITATION.cff) for the repository citation and retain the model and input-data attributions. The root [LICENSE](LICENSE) covers original custom workflow code. DSSAT's [licence](model/DSSAT_LICENSE.txt) and [notice](model/DSSAT_NOTICE.txt) accompany its source and binary. Third-party datasets retain their source terms.

Checks run only when requested, locally or manually from the Actions tab:

```bash
python3 workflow/verify_repository.py
```

After an intentional change to research assets, `python3 workflow/update_checksums.py` refreshes their inventory. Git records normal documentation and code edits.
