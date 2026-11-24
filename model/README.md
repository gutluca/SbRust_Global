# Model source and executable

## Production executable

[bin/dscsm048](bin/dscsm048) ([direct download](https://github.com/gutluca/SbRust_Global/releases/download/v0.1.0-data/dscsm048-linux-x86_64)) is the production Linux x86-64 ELF executable. Its SHA-256 matches the value recorded in the global simulation campaign:

```text
811a53c4d85d2f47ecf7815d9c46bc75fbc5fe5dbd2dd26aae3fce606f2ba41e
```

The file is 15,415,456 bytes. Binary identity and platform metadata are in [executable.json](executable.json). A Git checkout preserves its executable bit; after a manual download, use `chmod +x model/bin/dscsm048` on Linux.

DSSAT runs use runtime/standard/genotype tables, a model profile and the experiment's soil, weather and management inputs. See [reproduction.md](../docs/reproduction.md) for the workflow and input requirements.

## Archived source

[source/dismo_source_20261022.tar.gz](source/dismo_source_20261022.tar.gz) contains the DSSAT–DISMO source snapshot. The original HiPerGator compilation script is [compile_regional_model_20261022.slurm](compile_regional_model_20261022.slurm). Source and executable identities are recorded separately in the provenance files.

Source archive SHA-256:

```text
7cbbdf210fddb1afef7ecb239d71aa518b9e7d7005a8885b73887e5ee6c476ad
```

File-level source records are in [model_source_archive.json](../provenance/model_source_archive.json). Project repositories:

- [DISMO integration](https://github.com/gutluca/dssat-csm-os/tree/dismo-integration)
- [DSSAT upstream](https://github.com/DSSAT/dssat-csm-os)

## Build the archived source

With CMake and a Fortran compiler, run from the repository root:

```bash
mkdir -p .rebuild/source
tar -xzf model/source/dismo_source_20261022.tar.gz -C .rebuild/source
cmake -S .rebuild/source -B .rebuild/build -DCMAKE_BUILD_TYPE=RELEASE -DCMAKE_Fortran_COMPILER=gfortran
cmake --build .rebuild/build --parallel 4
```

This produces a new build of the archived source. Use the supplied production executable and its recorded checksum when reproducing that binary's identity.

The original [licence](DSSAT_LICENSE.txt) and [notice](DSSAT_NOTICE.md) accompany the source and executable.
