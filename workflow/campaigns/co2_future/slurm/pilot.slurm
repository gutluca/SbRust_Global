#!/bin/bash
#SBATCH --exclude=c0706a-s23,c0706a-s27
#SBATCH --job-name=co2v2-pilot
#SBATCH --output=/blue/wpavan/gustavo.luca/SBrust_world/logs/co2v2-pilot-%A_%a.out
#SBATCH --error=/blue/wpavan/gustavo.luca/SBrust_world/logs/co2v2-pilot-%A_%a.err
set -euo pipefail
module purge
module load gcc/11.4.0
module load python/3.11
source /blue/wpavan/gustavo.luca/SBrust_world/venvs/pythia/bin/activate
BASE=/blue/wpavan/gustavo.luca/SBrust_world
PACK="$BASE/releases/co2_future_global_20261119_v2"
GEN="$BASE/generated/world/co2_future_global_20261119_v2"
python "$PACK/scripts/worker.py" "$GEN/state.json" "$GEN/pilot_manifest.txt" \
  "$BASE/results/world/co2_future_global_20261119_v2/pilot" --index "$SLURM_ARRAY_TASK_ID" --pilot
