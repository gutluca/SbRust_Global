#!/bin/bash
set -euo pipefail
module purge
module load gcc/11.4.0
module load python/3.11
source /blue/wpavan/gustavo.luca/SBrust_world/venvs/pythia/bin/activate
python /blue/wpavan/gustavo.luca/SBrust_world/releases/onset_dsv_co2_20261123_v1/scripts/campaign.py status
squeue -h -u "$USER" -o "%i|%j|%a|%q|%C|%T|%R"
