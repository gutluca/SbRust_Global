# External inputs and levels of reproduction

## Reanalyse the raw results

The country masters, fixed spatial weights, executed maturity groups and processing code are sufficient to recompute the yield/loss analyses. This route does not use the crop executable or external climate/soil data. The complete Albania master is included as an executable example; all 103 original country masters are distributed as individual release assets and can be retrieved with the automatic downloader.

## Rerun the crop and disease simulations

Weather and soil inputs are required to rerun DSSAT. They need not be copied wholesale into Git history, but the exact versions, access routes, processing and site assignments must be recoverable. The same applies to calendars, genotype files, disease parameters and management configurations.

| Input | Role and source | Exact study record |
| --- | --- | --- |
| Daily weather | Five CMIP6 GCMs under historical/SSP126/SSP370/SSP585 forcing; ISIMIP bias-adjustment framework | Production used DSSAT WTH series under the study's weather directory. The processed-series version, checksums and conversion provenance still need export; a generic CMIP6 download is not an exact replacement |
| Soils | eGHR layered DSSAT soil profiles; [source data record](https://doi.org/10.7910/DVN/1PEEY0) and [Han et al. (2019)](https://pmc.ncbi.nlm.nih.gov/articles/PMC6694752/) | Country `.SOL` files and the executed site-to-profile mapping still need export |
| Historical calendar | GFDL-ESM4, rainfed soybean, annual 1991–2014 | The study uses the historical file with this exact coverage; the current provider release extends further back in time |
| Future calendars | IPSL-CM6A-LR, rainfed soybean, matching SSP126/370/585, annual 2015–2100 | Each SSP/year calendar reference is shared across the five weather GCMs; original files/checksums still need export |
| Soybean area | [SPAM 2020 V2r2](https://doi.org/10.7910/DVN/SWPENT), rainfed soybean harvested area | Derived retained support and fixed area are included; original raster version/checksum needs registration |
| Atmospheric CO₂ | [Meinshausen et al. (2020)](https://doi.org/10.5194/gmd-13-3571-2020), World annual concentrations | Included, with original supplement/workbook hashes and generated DSSAT inputs |
| Model runtime and management | DSSAT standard/genotype tables, model profile, generated project/site configurations and Python/Pythia environment | Source snapshot and exact executable are included; the executed runtime/configuration bundle still needs export |

The provider's [calendar documentation](https://www.isimip.org/gettingstarted/input-data-bias-adjustment/details/115/) distinguishes the earlier historical series beginning in 1991 from the May 2026 update beginning in 1850. Record and verify the files actually used; do not silently switch to the latest available release. The listed soil DOI identifies the source family and does not establish byte identity with the study's local copy.

`provenance/external_inputs.json` records known study filenames and retrieval status. `workflow/archive/export_hpg_reproducibility.py` exports a bounded snapshot of the production engine, site/configuration files, runtime tables and available soil profiles. Large weather/raster source archives remain separate; their exact provenance and access records must be completed for a full simulation rerun.

## Archival record

For the final study release, archive the tested code and data versions with persistent identifiers, record input access conditions, and cite the archive records. The repository's release/tag and SHA-256 manifests identify the current shared snapshot; they are not a DOI. This follows the distinction between sharing a verifiable dataset and retaining the inputs needed to regenerate it in the [Nature Portfolio reporting policy](https://www.nature.com/nature/editorial-policies/reporting-standards).
