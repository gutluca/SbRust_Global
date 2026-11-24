#!/bin/bash
#SBATCH --exclude=c0706a-s23,c0706a-s27
#SBATCH --mem=16G
#SBATCH --output=/blue/wpavan/gustavo.luca/SBrust_world/logs/appsco2-%A_%a.out
#SBATCH --error=/blue/wpavan/gustavo.luca/SBrust_world/logs/appsco2-%A_%a.err
set -euo pipefail
module purge
module load gcc/11.4.0
module load python/3.11
source /blue/wpavan/gustavo.luca/SBrust_world/venvs/pythia/bin/activate
exec python /blue/wpavan/gustavo.luca/SBrust_world/releases/onset_dsv_co2_20261123_v1/scripts/campaign.py job "$@" "${SLURM_ARRAY_TASK_ID:-0}"
