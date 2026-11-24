# Sources, attribution and licensing

| Material | Source / access | Snapshot note |
| --- | --- | --- |
| DISMO–DSSAT integration | https://github.com/gutluca/dssat-csm-os/tree/dismo-integration | User-designated project branch; production commit/build mapping still to recover |
| DSSAT CSM | https://github.com/DSSAT/dssat-csm-os | Upstream BSD-3-Clause; preserve its notices in any source/binary redistribution |
| SPAM 2020 V2r2 rainfed soybean harvested area | https://doi.org/10.7910/DVN/SWPENT ; https://www.mapspam.info/ | Original file: `spam2020_V2r2_global_H_SOYB_R.tif`; original raster not included. Record the exact retrieved version, checksum and its applicable terms before archival redistribution of derivatives |
| Climate weather inputs | ISIMIP CMIP6 bias-adjusted weather, W5E5 reference | Five GCMs listed in `study_design.json`; exact weather conversion/version/checksum catalogue remains to be exported |
| Historical crop calendar | GGCMI/ISIMIP GFDL-ESM4 histsoc, rainfed soybean, 1991–2014 | `ggcmi-crop-calendar_gfdl-esm4_histsoc_soy-noirr_annual_1991_2014.nc` |
| Future crop calendars | GGCMI/ISIMIP IPSL-CM6A-LR, matching SSP126/370/585, rainfed soybean, 2015–2100 | Reference calendar shared across weather GCMs; exact source record/DOI and checksum to register |
| Soils | Enhanced Global High-Resolution Soil Profile Database (eGHR), DSSAT-format profiles | Original raster and country `.SOL` files not included; exact version/terms and executed soil mapping pending |
| CO₂ | Meinshausen et al. (2020), https://doi.org/10.5194/gmd-13-3571-2020 | Author supplement, sheets T4/T6/T11, World CO₂ column B; original workbook/ZIP checksums included |
| Disease validation | Historical calibration/validation exports | Per-curve summaries included; primary references, raw observations and any ordinal-to-percent conversion must be traced before claiming observation-level reproducibility |

The licence in the repository root applies to original custom workflow code owned by this project. It does not relicense DSSAT, Pythia or third-party data. Source terms continue to apply to the derived spatial/input tables. No blanket Creative Commons licence is assigned to the entire mixed-source dataset in this development snapshot. The final data deposit should state per-component licences and preserve provider attribution.

## CO₂ reference (APA)

Meinshausen, M., Nicholls, Z. R. J., Lewis, J., Gidden, M. J., Vogel, E., Freund, M., Beyerle, U., Gessner, C., Nauels, A., Bauer, N., Canadell, J. G., Daniel, J. S., John, J. G., Krummel, P. B., Luderer, G., Meinshausen, N., Montzka, S. A., Rayner, P. J., Reimann, S., … Wang, R. H. J. (2020). The shared socio-economic pathway (SSP) greenhouse gas concentrations and their extensions to 2500. *Geoscientific Model Development, 13*, 3571–3605. https://doi.org/10.5194/gmd-13-3571-2020

For DSSAT, cite both the version actually used and the model references recommended by the upstream project: https://dssat.net/about/references/ . Repository URLs supplement scientific model citations; they do not replace them.
