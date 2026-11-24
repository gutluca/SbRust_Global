# Reproduction

## Read the raw outputs

Extract `SbRust_Global_paired_masters_20261123.tar` into a data directory. It contains `countries/<country>/summary_master_<country>.csv.gz`, the raw-file manifest and a data dictionary. The CSV separator is `;`. Read maturity-group labels as strings to preserve `000`, `00` and `0`. Keep the original compressed files unchanged: their hashes identify the exact simulation outputs.

## Process the paired outputs

```bash
python3 -m pip install -r requirements.txt
python3 workflow/verify_repository.py
python3 workflow/analysis/run_analysis.py --data-root DATA_ROOT --output results/analysis --workers 2
```

`DATA_ROOT` contains the extracted `countries/` directory. The default run processes all 103 countries; add `--countries Albania` for a small real-data check. The output directory must be new. The program verifies each input checksum, applies the documented row filter and computes country/cell/ensemble aggregates. It does not modify the raw masters or launch DSSAT.

The preserved numerical reference is `workflow/analysis/reference/analyze_20261123.py`. The portable `engine.py` changes paths, constructs expected experiment keys from the design and removes a descriptive old-campaign comparison. Filtering, formulas, completeness rules and area weighting are retained. Checks of 8,519,200 raw records from Brazil, Bolivia and Albania reproduced 22 reference tables exactly; the receipt is in `provenance/analysis_portability_verification.json`.

## Reconstruct simulations

The processing stages are:

1. `workflow/production_reference/build_sites.py`: build country–cell sites from harvested area, boundaries and weather indexing.
2. Calendar and maturity scripts in the same folder: generate historical candidate runs, collate candidate scores and select a fixed group. Final Brazil/Bolivia revisions are retained in `workflow/maturity/final_revisions`.
3. `workflow/campaigns/co2_future`: prepare and execute the future paired simulations with the corrected CO₂ inputs.
4. `workflow/campaigns/onset_dsv`: construct healthy-crop trajectories and calculate DSV application demand.
5. Annual collation, merging and validation scripts: produce the paired country masters.

These production scripts retain the original HiPerGator layout. They document the executed implementation; their launchers are not a fresh-checkout installer. The exact production engine, site/soil mappings, input configurations, runtime tables and Python/Pythia environment still need exporting from the original project. `workflow/archive/export_hpg_reproducibility.py` prepares that bounded, read-only export. Input weather, calendars, soils and spatial rasters must be obtained under their source terms.

The model source and production binary identification are in `model/`. The binary must match the recorded SHA-256; another local Windows build is not the production Linux executable.

## Package raw results

```bash
python3 workflow/archive/package_masters.py --data-root DATA_ROOT --output /path/to/archive/SbRust_Global_paired_masters_20261123.tar
```

This verifies all listed source files and packages the original gzip CSVs without filtering or recompressing them. An existing archive is not overwritten.
