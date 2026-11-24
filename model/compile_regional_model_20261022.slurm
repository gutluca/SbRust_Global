#!/bin/bash
#SBATCH --job-name=sbrust-build-regional
#SBATCH --account=wpavan
#SBATCH --qos=wpavan
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=01:00:00
#SBATCH --output=/blue/wpavan/gustavo.luca/SBrust_world/logs/%x-%j.out
#SBATCH --error=/blue/wpavan/gustavo.luca/SBrust_world/logs/%x-%j.err
set -euo pipefail
ROOT=/blue/wpavan/gustavo.luca/SBrust_world
RELEASE=${1:?release directory required}
module purge
module load gcc/11.4.0
module load cmake
(cd "$RELEASE" && sha256sum -c SHA256SUMS.txt)
SRC="$ROOT/source/dismo-regional-${SLURM_JOB_ID}"
BUILD="$ROOT/build/dismo-regional-${SLURM_JOB_ID}"
APP="$ROOT/apps/dismo-regional-${SLURM_JOB_ID}"
test ! -e "$SRC"
test ! -e "$BUILD"
test ! -e "$APP"
mkdir -p "$SRC" "$BUILD" "$APP"
# DSSAT also searches beside argv[0] for these runtime tables. Installing
# only the ELF into an isolated directory makes otherwise valid runs stop 99.
# Reuse the previous production tables; do not copy its calibrated inputs.
RUNTIME="$ROOT/apps/dssat-dismo-4.8"
for NAME in DETAIL.CDE MODEL.ERR; do
  test -f "$RUNTIME/$NAME"
  install -m 0644 "$RUNTIME/$NAME" "$APP/$NAME"
done
sha256sum "$APP/DETAIL.CDE" "$APP/MODEL.ERR" > "$APP/RUNTIME_SHA256SUMS.txt"
tar -xzf "$RELEASE/dismo_source_20261022.tar.gz" -C "$SRC" --no-same-owner --no-same-permissions
cmake -S "$SRC" -B "$BUILD" -DCMAKE_BUILD_TYPE=RELEASE -DCMAKE_Fortran_COMPILER="$(command -v gfortran)"
cmake --build "$BUILD" --parallel "${SLURM_CPUS_PER_TASK}"
test -x "$BUILD/bin/dscsm048"
install -m 0755 "$BUILD/bin/dscsm048" "$APP/dscsm048"
file "$APP/dscsm048"
sha256sum "$SRC/Plant/Generic-Pest/DISMO.for" "$APP/dscsm048" > "$APP/SHA256SUMS.txt"
printf 'SOURCE=%q\nBUILD=%q\nEXE=%q\nRELEASE=%q\n' "$SRC" "$BUILD" "$APP/dscsm048" "$RELEASE" > "$RELEASE/build_result.env"
cat "$APP/SHA256SUMS.txt"
echo 'BUILD COMPLETE - executable compiled; no simulations submitted'
