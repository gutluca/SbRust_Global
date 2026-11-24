# Reproduction

## Process the raw outputs

From the repository root, install the processing dependencies and verify the shared assets:

```bash
python3 -m pip install -r requirements.txt
python3 workflow/verify_repository.py
```

Run the complete Albania example:

```bash
python3 workflow/analysis/run_analysis.py --data-root examples/data --countries Albania --output results/example --workers 1
python3 workflow/analysis/check_example.py --output results/example
```

The example contains 17 country–cells and 78,200 raw paired records; the analysis retains 78,005 records. To process all 103 countries:

```bash
python3 workflow/download_data.py --output data/raw
python3 workflow/analysis/run_analysis.py --data-root data/raw --output results/global --workers 2
```

Add `--countries Albania` to both commands to process a subset. The downloader verifies each original compressed file against [master_files.json](../provenance/master_files.json). The processing code applies the [yield filter and aggregation rules](methods.md#yield-processing). Each run uses a new output directory and preserves the raw inputs.

Original masters are semicolon-delimited gzip CSVs under `countries/<country>/summary_master_<country>.csv.gz`. Read maturity-group labels as strings to preserve `000`, `00` and `0`. See the [data dictionary](data_dictionary.md) for column definitions.

The numerical verification compared 8,519,200 raw records from Brazil, Bolivia and Albania with the reference implementation and reproduced 22 tables exactly. The receipt is in [analysis_portability_verification.json](../provenance/analysis_portability_verification.json).

## Production workflow

| Stage | Implementation |
| --- | --- |
| Spatial preprocessing and calendar preparation | [workflow/production_reference](../workflow/production_reference) |
| Maturity-group selection | [workflow/maturity](../workflow/maturity), with settings and scores in `data/calibration` and `provenance` |
| Paired future simulations with SSP-specific CO₂ | [workflow/campaigns/co2_future](../workflow/campaigns/co2_future) |
| Healthy-crop trajectories and DSV application calculations | [workflow/campaigns/onset_dsv](../workflow/campaigns/onset_dsv) |
| Master-file processing | [workflow/analysis](../workflow/analysis) |

Production launchers use the original HiPerGator directory conventions; adapt paths and scheduler settings to the target installation. DSSAT runs require daily weather, GGCMI crop calendars, eGHR soil profiles, site and management configurations, and DSSAT runtime/standard/genotype tables. Input sources and study filenames are listed in [sources_and_licensing.md](sources_and_licensing.md) and [external_inputs.json](../provenance/external_inputs.json). The Python/Pythia environment used by the production launchers is separate from the pinned dependencies for raw-output processing.

The [model documentation](../model/README.md) identifies the production Linux executable and provides build instructions for the archived source. [export_hpg_reproducibility.py](../workflow/archive/export_hpg_reproducibility.py) packages production site/configuration and runtime files from the original study directory.

## File integrity

README, documentation and code can be edited normally; Git records their versions. The checksum inventory protects frozen research data, example data, model artifacts and provenance records. After an intentional change to those assets, run `python3 workflow/update_checksums.py`, review the manifest changes and commit them together. Downloaded files under `data/raw/` are verified by the downloader.
