#!/bin/bash
#SBATCH --exclude=c0706a-s23,c0706a-s27
#SBATCH --mem=16G
#SBATCH --output=/blue/wpavan/gustavo.luca/SBrust_world/logs/co2v2-annual-%A_%a.out
#SBATCH --error=/blue/wpavan/gustavo.luca/SBrust_world/logs/co2v2-annual-%A_%a.err
set -euo pipefail
module purge
module load gcc/11.4.0
module load python/3.11
source /blue/wpavan/gustavo.luca/SBrust_world/venvs/pythia/bin/activate
python /blue/wpavan/gustavo.luca/SBrust_world/releases/co2_future_global_20261119_v2/scripts/worker.py \
  "$@" --index "$SLURM_ARRAY_TASK_ID"
