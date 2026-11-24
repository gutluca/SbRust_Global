#!/bin/bash
#SBATCH --exclude=c0706a-s23,c0706a-s27
#SBATCH --job-name=co2v2-controller
#SBATCH --output=/blue/wpavan/gustavo.luca/SBrust_world/logs/co2v2-controller-%j.out
#SBATCH --error=/blue/wpavan/gustavo.luca/SBrust_world/logs/co2v2-controller-%j.err
set -euo pipefail
module purge
module load gcc/11.4.0
module load python/3.11
source /blue/wpavan/gustavo.luca/SBrust_world/venvs/pythia/bin/activate
BASE=/blue/wpavan/gustavo.luca/SBrust_world
python "$BASE/releases/co2_future_global_20261119_v2/scripts/controller.py" \
  "$BASE/generated/world/co2_future_global_20261119_v2/state.json"
