# Model code and production executable

## Source

`source/dismo_source_20261022.tar.gz` is the frozen DSSAT–DISMO source captured for the regional model package. Its original licence and notices are inside the archive. The original compilation script is `compile_regional_model_20261022.slurm`; it documents the HiPerGator build procedure and requires adapting paths before use elsewhere.

The source archive SHA-256 is `7cbbdf210fddb1afef7ecb239d71aa518b9e7d7005a8885b73887e5ee6c476ad`. File-level source provenance is in `../provenance/model_source_archive.json`. The exact mapping between that source snapshot, the Git commit and the production build still needs to be recovered.

Project references:

- DISMO integration: https://github.com/gutluca/dssat-csm-os/tree/dismo-integration
- DSSAT upstream: https://github.com/DSSAT/dssat-csm-os

## Production executable

[bin/dscsm048](bin/dscsm048) ([direct download](https://github.com/gutluca/SbRust_Global/releases/download/v0.1.0-data/dscsm048-linux-x86_64)) is the exact Linux x86-64 ELF executable retrieved from the production project. Its SHA-256 matches the hash recorded in the corrected global campaign:

```text
811a53c4d85d2f47ecf7815d9c46bc75fbc5fe5dbd2dd26aae3fce606f2ba41e
```

The binary is 15,415,456 bytes. Machine-readable identity and binary metadata are in [executable.json](executable.json). A Git checkout preserves its executable bit; after a manual download, use `chmod +x model/bin/dscsm048`. This is a Linux executable; it does not run natively on Windows.

DSSAT also needs its runtime/standard/genotype tables, model profile, simulation inputs and the study's soil/weather/configuration files. Those are separate from the executable. The production runtime and site/configuration bundle still needs export; see [external_inputs.md](../docs/external_inputs.md).

The model's original [licence](DSSAT_LICENSE.txt) and [notice](DSSAT_NOTICE.md) accompany the binary and source snapshot. Source and binary are recorded separately: the exact build-commit mapping and byte-identical rebuild of this executable have not been independently established.

## Build the included source snapshot

A Fortran compiler and CMake are required. For a new build directory:

```bash
mkdir -p .rebuild/source
tar -xzf model/source/dismo_source_20261022.tar.gz -C .rebuild/source
cmake -S .rebuild/source -B .rebuild/build -DCMAKE_BUILD_TYPE=RELEASE -DCMAKE_Fortran_COMPILER=gfortran
cmake --build .rebuild/build --parallel 4
```

This rebuilds the included source snapshot; it is not a claim of bitwise equivalence to the production binary. The original cluster build script is retained for provenance.
