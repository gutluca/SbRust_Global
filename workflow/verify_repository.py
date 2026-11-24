"""Verify compact public inputs, critical hashes and the repository inventory."""
from __future__ import annotations
import csv
import hashlib
import json
from pathlib import Path
from checksum_inventory import verify_inventory


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    repo=Path(__file__).resolve().parents[1]
    design=json.loads((repo/'provenance/study_design.json').read_text(encoding='utf-8'))
    with (repo/'data/spatial/selected_maturity_groups.csv').open(encoding='utf-8',newline='') as stream:
        gm=list(csv.DictReader(stream))
    assert len(gm)==design['country_cell_fragments']==16612
    assert len({r['COUNTRY'] for r in gm})==103
    assert len({(r['COUNTRY'],r['CELL_ID']) for r in gm})==len(gm)
    assert len({(float(r['LATITUDE']),float(r['LONGITUDE'])) for r in gm})==15765
    assert abs(sum(float(r['HARVEST_AREA_HA']) for r in gm)-design['harvested_area_ha'])<1e-4
    assert all(r['GM'] in ['000','00','0']+[str(g) for g in range(1,11)] for r in gm)
    assert all(int(r['GM'])<=7 for r in gm if r['COUNTRY']=='Brazil' and float(r['LATITUDE'])<=-20)
    assert all(int(r['GM'])<=8 for r in gm if r['COUNTRY']=='Bolivia')
    masters=json.loads((repo/'provenance/master_files.json').read_text(encoding='utf-8'))
    assert len(masters)==103 and sum(a['rows'] for a in masters)==76415200
    assert all(a['rows']==4600*a['points'] for a in masters)
    co2=json.loads((repo/'data/co2/provenance.json').read_text(encoding='utf-8'))
    for entry in co2['files'].values():
        path=repo/'data/co2'/Path(entry['relative_path']).relative_to('inputs/co2')
        assert sha(path)==entry['sha256'],path
    with (repo/'data/calibration/country_parameter_sets.csv').open(encoding='utf-8',newline='') as stream:
        assignments=list(csv.DictReader(stream))
    for a in assignments:
        assert sha(repo/f"data/calibration/{a['parameter_set']}/disease_parameters.txt")==a['disease_input_sha256']
    executable=json.loads((repo/'model/executable.json').read_text(encoding='utf-8'))
    binary=repo/executable['repository_path']
    assert binary.stat().st_size==executable['bytes'] and sha(binary)==executable['sha256']
    with binary.open('rb') as stream:
        assert stream.read(6)==b'\x7fELF\x02\x01', 'Expected the production Linux ELF64 executable'
    verify_inventory(repo)
    print(f"PASS: 103 countries; {len(gm):,} country-cells; {design['raw_paired_rows']:,} paired rows; critical parameter/CO2 hashes verified.")


if __name__=='__main__':main()
