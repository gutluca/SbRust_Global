#!/usr/bin/env python3
"""Prepare new per-country project JSONs from a previous global campaign."""
import argparse
import json
from pathlib import Path
from assign_country_input import assign

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('state',type=Path)
p.add_argument('output',type=Path)
p.add_argument('executable',type=Path)
p.add_argument('--registry',type=Path,default=Path(__file__).resolve().parents[1]/'config/regional_inputs_20261022')
a=p.parse_args()
if a.output.exists():raise FileExistsError(a.output)
state=json.loads(a.state.read_text())
countries=[c for c in state['countries'] if c['status'] in {'completed','reused'}]
# Validate all names and inputs BEFORE producing any country project.
import csv
with (a.registry/'country_assignment.csv').open(encoding='utf-8',newline='') as f:
    mapping=list(csv.DictReader(f,delimiter=';'))
for c in countries:
    matches=[r for r in mapping if r['country']==c['country']]
    if len(matches)!=1:raise ValueError('Missing/duplicate country: '+c['country'])
    if not (a.registry/matches[0]['parameter_set']/'disease_parameters.txt').is_file():raise FileNotFoundError(c['country'])
if not a.executable.is_file():raise FileNotFoundError(a.executable)
a.output.mkdir(parents=True)
template=a.output/'template.json'
template.write_text(json.dumps(state['project'],indent=2))
manifest=[]
for c in countries:
    output=a.output/c['key']/'project.json'
    cfg=assign(template,c['country'],output,a.executable,a.registry)
    manifest.append({'country':c['country'],'project':str(output.resolve()),**cfg['experiment']['regional_parameter_assignment']})
(a.output/'input_assignments.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
print(f'Prepared {len(manifest)} NEW country projects. No simulations submitted.')
