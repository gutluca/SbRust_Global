# SbRust_Global

Methodology, input data, model code and raw outputs for global soybean rust simulations with DISMO integrated into DSSAT.

This repository supports reanalysis of the original paired outputs and documents the inputs needed to rerun the crop and disease model. A complete real-data example and the verified production Linux executable are included.

## Repository contents

| Folder | Contents |
| --- | --- |
| [data/spatial](data/spatial) | Retained soybean study mask and executed maturity groups |
| [data/calibration](data/calibration) | Disease parameters, country assignments, maturity-group candidate scores and final selection decisions |
| [data/co2](data/co2) | Annual SSP-specific CO₂ concentrations, DSSAT input files and source provenance |
| [model](model) | Frozen model source, build script and identification of the production executable |
| [workflow](workflow) | Spatial preprocessing, maturity calibration/selection, paired simulations, DSV application calculations and output processing |
| [docs](docs) | Methods, data dictionary, source information and reproduction instructions |
| [provenance](provenance) | Experiment design, raw-file manifests, SHA-256 checksums and verification records |

## Experiment

The study covers 103 countries, 16,612 country–cell fragments and 15,765 distinct coordinate pairs. Rainfed soybean harvested area is held fixed across experiments. The simulations use five GCMs, five sowing-date alternatives, a 1981–2014 baseline and future periods of 2020–2039 and 2040–2069 under SSP1-2.6, SSP3-7.0 and SSP5-8.5.

One maturity group is fixed per country–cell. Annual future CO₂ follows the matching SSP concentrations from [Meinshausen et al. (2020)](https://doi.org/10.5194/gmd-13-3571-2020).

See [methods.md](docs/methods.md) for spatial selection, calibration policies, calendars, paired disease runs and processing rules.

## Raw results

The final country masters contain 76,415,200 paired records. Every row includes the matched disease-free and disease-affected soybean yields; both arms are supplied in the same files. The original compressed CSVs are retained without applying the analysis filter or changing their contents.

The **[data release](https://github.com/gutluca/SbRust_Global/releases/tag/v0.1.0-data)** distributes the 103 original gzip CSVs as individual country downloads (2.07 GB in total). [master_files.json](provenance/master_files.json) lists each file, row count, size and SHA-256 checksum. The [data dictionary](docs/data_dictionary.md) documents the semicolon-delimited format. The download command below retrieves and verifies the complete dataset; individual countries can also be selected.

## Code and executable

- **DISMO–DSSAT integration:** [gutluca/dssat-csm-os, dismo-integration](https://github.com/gutluca/dssat-csm-os/tree/dismo-integration).
- **DSSAT upstream:** [DSSAT/dssat-csm-os](https://github.com/DSSAT/dssat-csm-os).
- **Frozen source included here:** [model/source](model/source).
- **Production executable:** [model/bin/dscsm048](model/bin/dscsm048), Linux x86-64, verified against the campaign's SHA-256. Build/source provenance and runtime requirements are described in [model/README.md](model/README.md).

## Quick start: real-data example

```bash
python3 -m pip install -r requirements.txt
python3 workflow/verify_repository.py
python3 workflow/analysis/run_analysis.py --data-root examples/data --countries Albania --output results/example --workers 1
python3 workflow/analysis/check_example.py --output results/example
```

The [example](examples/README.md) contains the complete unmodified Albania master: 17 country–cells and 78,200 raw records. The documented filter retains 78,005 and excludes 195. Python 3.12 and pinned numerical packages are used for verification; automated checks run on pushes and pull requests.

## Process all 103 countries

```bash
python3 workflow/download_data.py --output data/raw
python3 workflow/analysis/run_analysis.py --data-root data/raw --output results/global --workers 2
```

Existing output directories are not overwritten. The raw files are retained unchanged; validity filtering is applied only during processing.

To download a subset, add `--countries Brazil "United States"` to the download command, then pass the same selection to `run_analysis.py`.

## External inputs

Weather and soil data are required to rerun DSSAT, but are not needed to reanalyse the raw masters. Their provider records and the original calendar filenames are documented in [external_inputs.md](docs/external_inputs.md) and [external_inputs.json](provenance/external_inputs.json). The exact processed-weather catalogue, site-to-soil mappings and executed runtime/configuration environment still need export from the original production project. The included binary is therefore one component of the model installation, not a complete simulation-input bundle.

See [reproduction.md](docs/reproduction.md) for the workflow and [release_status.md](docs/release_status.md) for the exact scope of this snapshot. A final archival code/data DOI will be added when the study release is frozen.

Original model and input terms apply separately from the licence for the custom workflow. Source attribution and access information are recorded in [sources_and_licensing.md](docs/sources_and_licensing.md).
