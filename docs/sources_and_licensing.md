# Sources and licensing

## Model and input sources

| Material | Source | Study use |
| --- | --- | --- |
| DISMO–DSSAT integration | [gutluca/dssat-csm-os, dismo-integration](https://github.com/gutluca/dssat-csm-os/tree/dismo-integration) | Coupled crop–disease model |
| DSSAT CSM | [DSSAT/dssat-csm-os](https://github.com/DSSAT/dssat-csm-os) | CROPGRO-Soybean model and runtime |
| SPAM 2020 V2r2 | [Source dataset](https://doi.org/10.7910/DVN/SWPENT) | Rainfed soybean harvested area, `spam2020_V2r2_global_H_SOYB_R.tif`; the derived study mask is in `data/spatial` |
| GGCMI/ISIMIP crop calendars | [Calendar documentation](https://www.isimip.org/gettingstarted/input-data-bias-adjustment/details/115/) and [data catalogue](https://data.isimip.org/search/tree/ISIMIP3b/InputData/socioeconomic/crop_calendar/) | Annual rainfed soybean sowing and maturity references; study filenames listed below |
| Daily climate | [ISIMIP data catalogue](https://data.isimip.org/) | Historical and SSP126/370/585 weather for the five GCMs in `provenance/study_design.json` |
| eGHR soil profiles | [Source dataset](https://doi.org/10.7910/DVN/1PEEY0) and [Han et al. (2019)](https://doi.org/10.1016/j.envsoft.2019.05.012) | Layered DSSAT soil profiles |
| Atmospheric CO₂ | [Meinshausen et al. (2020)](https://doi.org/10.5194/gmd-13-3571-2020) | Annual global SSP concentrations; values, generated DSSAT inputs and supplement checksums in `data/co2` |

## Crop calendar files

The following GGCMI/ISIMIP NetCDF files are supplied unchanged in [data/calendars](../data/calendars):

```text
ggcmi-crop-calendar_gfdl-esm4_histsoc_soy-noirr_annual_1991_2014.nc
ggcmi-crop-calendar_ipsl-cm6a-lr_ssp126_soy-noirr_annual_2015_2100.nc
ggcmi-crop-calendar_ipsl-cm6a-lr_ssp370_soy-noirr_annual_2015_2100.nc
ggcmi-crop-calendar_ipsl-cm6a-lr_ssp585_soy-noirr_annual_2015_2100.nc
```

Maturity-group calibration uses only the historical 1991–2014 file. The three future files provide SSP-specific annual sowing dates; the calibrated maturity group remains fixed. Within each SSP, the calendar reference is shared across all five weather GCMs. The source filenames identify the study's calendar series when consulting the provider catalogue. Input records and production directory conventions are in [external_inputs.json](../provenance/external_inputs.json).

## Licensing

The root [LICENSE](../LICENSE) applies to this project's original custom workflow code. DSSAT retains its accompanying [licence](../model/DSSAT_LICENSE.txt) and [notice](../model/DSSAT_NOTICE.md). Third-party model components and input datasets retain their respective source terms and attribution requirements.

## CO₂ reference

Meinshausen, M., Nicholls, Z. R. J., Lewis, J., Gidden, M. J., Vogel, E., Freund, M., Beyerle, U., Gessner, C., Nauels, A., Bauer, N., Canadell, J. G., Daniel, J. S., John, J. G., Krummel, P. B., Luderer, G., Meinshausen, N., Montzka, S. A., Rayner, P. J., Reimann, S., … Wang, R. H. J. (2020). The shared socio-economic pathway (SSP) greenhouse gas concentrations and their extensions to 2500. *Geoscientific Model Development, 13*, 3571–3605. https://doi.org/10.5194/gmd-13-3571-2020

DSSAT's recommended model references are listed at [dssat.net/about/references](https://dssat.net/about/references/).
