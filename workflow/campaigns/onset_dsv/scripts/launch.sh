#!/bin/bash
set -euo pipefail
module purge
module load gcc/11.4.0
module load python/3.11
source /blue/wpavan/gustavo.luca/SBrust_world/venvs/pythia/bin/activate
cd /blue/wpavan/gustavo.luca/SBrust_world/releases/onset_dsv_co2_20261123_v1
sha256sum -c SHA256SUMS.txt
python -m unittest discover -s tests -q
python scripts/campaign.py launch
