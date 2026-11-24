"""Freeze the existing science and prepare corrected/reproduction pilot pairs."""
from __future__ import annotations
import argparse
import copy
import csv
import gzip
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sys

import common as c
from prepare import prepare_country, write_zones
from co2_runtime import dated_co2


def check_parameters(path):
    target = None
    columns = None
    waiting = False
    selected = None
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if not fields or line.lstrip().startswith('!'):
            continue
        if fields[0] == '@TARGET_DISEASE':
            waiting = True
        elif waiting:
            target = fields[0]
            waiting = False
        elif fields[0] == '@VAR#':
            columns = [s.lstrip('@') for s in fields]
        elif columns and len(fields) > 1 and fields[1] == target:
            selected = dict(zip(columns, fields))
    if not selected or float(selected['YMAX']) <= 0:
        raise ValueError('Disease production input is missing or uses YMAX=0')


def make_pilot(state):
    import fiona
    paths = []
    pilot_sites = {}
    catalog = {x['gm']: x for x in c.load(Path(state['pack']) / 'inputs/generic_maturity_candidates.json')['cultivars']}
    for country in state['countries']:
        if country['country'] not in c.PILOT_COUNTRIES:
            continue
        prepare_country(Path(state['output']) / 'state.json', country['key'])
        base, ready, _ = c.paths(state, country)
        with fiona.open(ready / 'calibrated.gpkg') as source:
            features = [{'type': 'Feature', 'geometry': dict(f['geometry']), 'properties': dict(f['properties'])} for f in source]
            schema, crs = source.schema.copy(), source.crs
        # Deterministic geographical spread; a technology test, not national sampling.
        features.sort(key=lambda f: (float(f['geometry']['coordinates'][1]), int(f['properties']['CellID'])))
        chosen = {str(features[i]['properties']['CellID']): features[i] for i in sorted({0, len(features) // 2, len(features) - 1})}
        pilot = base / 'pilot'
        pilot.mkdir()
        zones = write_zones(chosen, schema, crs, pilot / 'zone_sites', catalog)
        pilot_sites.update({str(z['path']): c.sha(z['path']) for z in zones})
        originals = [Path(p) for p in (ready / 'annual_configs/manifest.txt').read_text().splitlines()]
        for path in originals:
            cfg = c.load(path)
            meta = cfg['pipeline']
            if meta['simulation_year'] not in c.PILOT_YEARS or meta['gcm'] not in c.PILOT_GCMS:
                continue
            new = copy.deepcopy(cfg)
            prototypes = {(('disease' if r['name'].startswith('disease_') else 'control'), r['planting_label']): r for r in cfg['runs']}
            runs = []
            for zone in zones:
                for plant in c.PLANTS:
                    for arm in ('disease', 'control'):
                        proto = prototypes[arm, plant]
                        old_sites = proto['sites'][len('xy_from_vector::'):]
                        run = c.replace(copy.deepcopy(proto), {old_sites: str(zone['path'])})
                        run.update(name=f'{arm}_{plant}_GM_{zone["gm"]}', sites='xy_from_vector::' + str(zone['path']),
                                   ingeno=zone['ingeno'], cname=zone['cname'], zone_id='GM_' + zone['gm'])
                        runs.append(run)
            new['runs'] = runs
            new['name'] = 'pilot_' + cfg['name']
            new['workDir'] = str(Path(state['output']) / 'pilot_work' / new['name'])
            new['pipeline']['co2_rerun']['expected_points'] = len(chosen)
            new['pipeline']['co2_rerun']['pilot'] = True
            target = pilot / (new['name'] + '.json')
            c.save(target, new)
            paths.append(target)
            replay = copy.deepcopy(new)
            replay['name'] = 'replay_' + new['name']
            replay['workDir'] = str(Path(state['output']) / 'pilot_work' / replay['name'])
            replay['pipeline']['co2_forcing'] = dict(state['reference_co2'], scenario=meta['scenario'], switch='M')
            replay['pipeline']['co2_rerun']['reference_replay'] = True
            replay['pipeline']['co2_rerun']['expected_master'] = str(pilot / 'reference_master.csv.gz')
            target = pilot / (replay['name'] + '.json')
            c.save(target,replay)
            paths.append(target)
        # Published output is the numerical authority; do not regenerate a
        # reference from the new implementation itself.
        expected = pilot / 'reference_master.csv.gz'
        selected = 0
        with gzip.open(country['source_master'],'rt',newline='',encoding='utf-8') as source, gzip.open(expected,'wt',newline='',encoding='utf-8') as destination:
            reader=csv.DictReader(source,delimiter=';')
            writer=csv.DictWriter(destination,fieldnames=reader.fieldnames,delimiter=';');writer.writeheader()
            for row in reader:
                if (row['CELL_ID'] in chosen and row['SCENARIO'] in c.SSPS and
                    int(row['SIM_YEAR']) in c.PILOT_YEARS and row['GCM'] in c.PILOT_GCMS):
                    writer.writerow(row);selected+=1
        if selected != len(chosen)*len(c.PILOT_YEARS)*len(c.PILOT_GCMS)*len(c.SSPS)*5:
            raise ValueError('Published numerical reference matrix incomplete')
        pilot_sites[str(expected)]=c.sha(expected)
    if len(paths) != c.PILOT_CONFIGS:
        raise ValueError('Corrected/replay pilot matrix incomplete')
    manifest = Path(state['output']) / 'pilot_manifest.txt'
    manifest.write_text(''.join(str(p) + '\n' for p in paths), encoding='utf-8')
    c.save(Path(state['output']) / 'pilot_lock.json', {'config_sha256': {str(p): c.sha(p) for p in paths},
                                                      'sites_sha256': pilot_sites})


def main():
    if not __import__('os').environ.get('SLURM_JOB_ID'):
        raise RuntimeError('Bootstrap must run as a Slurm compute job')
    if c.GEN.exists() or c.RESULTS.exists():
        raise FileExistsError('New campaign output already exists; inspect journal, do not duplicate')
    package_manifest = c.load(c.PACK / 'package_manifest.json')
    for record in package_manifest['files']:
        if c.sha(c.PACK / record['path']) != record['sha256']:
            raise ValueError('Installed package checksum mismatch: ' + record['path'])
    old = c.load(c.OLD_STATE)
    if c.sha(c.OLD_STATE) != 'e6f82aada93dd3f0b60c3f9a778d803025eb8e04e47b422a54216feaf494fd63':
        raise ValueError('Completed source state changed since the publication provenance lock')
    if old.get('version') != 5 or old.get('status') != 'finished' or len(old['countries']) != 103:
        raise ValueError('Expected the finished 103-country regional campaign')
    if any(x['status'] != 'completed' for x in old['countries']):
        raise ValueError('Source countries must all be completed')
    expected_sources = c.load(c.PACK / 'inputs/current_sources.json')
    retained = c.population(c.PACK)
    if len(retained) != 16612 or set(x[0] for x in retained) != set(expected_sources):
        raise ValueError('Frozen publication population differs')
    profile = old['project']['profiles']['hipergator']
    executable = Path(profile['dssat_executable'])
    if c.sha(executable) != c.EXE_SHA:
        raise ValueError('Approved model binary differs')
    c.GEN.mkdir(parents=True)
    c.RESULTS.mkdir(parents=True)
    engine = c.GEN / 'engine'
    engine.mkdir()
    # Snapshot the actual previous release; never edit its code or inputs.
    for folder in ('scripts', 'templates', 'config'):
        shutil.copytree(Path(old['code']) / folder, engine / folder,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    if c.sha(engine / 'templates/SBG00001.SBX') != c.TEMPLATE_SHA:
        raise ValueError('Previous release template changed')
    state = {k: copy.deepcopy(old[k]) for k in ('root', 'index', 'budget_cpus', 'max_active_countries',
                                               'verified_account_cpu_limits', 'coverage_policy', 'project')}
    state.update(version=7, scope='future paired rerun with SSP atmospheric CO2; GMs fixed; historical rows preserved',
        status='prepared', code=str(c.PACK), pack=str(c.PACK), output=str(c.GEN), results=str(c.RESULTS), engine=str(engine),
        previous_state=str(c.OLD_STATE), previous_state_sha256=c.sha(c.OLD_STATE), previous_output=old['output'],
        executable=str(executable), executable_sha256=c.EXE_SHA, stages=list(c.STAGES), expected_configs=750,
        countries=[], frozen_files=[], created_utc=datetime.now(timezone.utc).isoformat(), controllers=[], turn=0)
    shared_co2 = c.BASE / 'data/dssat48/StandardData/CO2048.WDA'
    state['shared_default_co2'] = {'path': str(shared_co2), 'sha256_before': c.sha(shared_co2),
                                  'modified_by_this_campaign': False}
    if c.sha(shared_co2) != '2b7478e4a7f8707d243eb3aaf1165b5b557b93936a435d26fe3e797fcc841749':
        raise ValueError('Original CO2 default differs from the previously audited campaign input')
    reference = c.GEN / 'reference_co2/CO2048.WDA'
    reference.parent.mkdir()
    shutil.copy2(shared_co2,reference)
    state['reference_co2']={'path':str(reference),'sha256':c.sha(reference),
                            'dated_ppm':dated_co2(reference),'implementation':'original observed M series, including original terminal-value hold'}
    state['excluded_nodes']=list(c.BAD_NODES)
    budgets = state['budget_cpus']
    if not 4 <= budgets['wpavan'] <= 89 or not 0 <= budgets['wpavan-b'] <= 810:
        raise ValueError('Worker budgets differ from the verified 90/810 CPU limits')
    params = c.GEN / 'disease_parameters'
    params.mkdir()
    locked = [executable, reference, c.OLD_STATE, c.PACK / 'inputs/population.csv', c.PACK / 'inputs/co2/provenance.json']
    for field in ('dssat_profile', 'dssat_simulation_cde', 'spam_raster', 'soil_raster'):
        locked.append(Path(profile[field]))
    # Include the unchanged genotype library used by the original binary.
    locked.extend(p for p in (c.BASE / 'data/dssat48/Genotype').glob('SB*048.*') if p.is_file())
    for prior in old['countries']:
        country = {k: copy.deepcopy(prior[k]) for k in ('country', 'key', 'area_for_order_ha', 'original')}
        name, key = country['country'], country['key']
        latest_project_path = Path(old['output']) / key / 'project.json'
        latest = c.load(latest_project_path)
        for calendar_source in latest['experiment'].get('calendar', {}).get('sources', {}).values():
            locked.append(Path(calendar_source))
        assignment = latest['experiment']['regional_parameter_assignment']
        source_input = Path(latest['profiles']['hipergator']['disease_parameters'])
        expected_input = assignment['input_sha256']
        if c.sha(source_input) != expected_input:
            raise ValueError('Latest regional disease parameters changed: ' + name)
        check_parameters(source_input)
        frozen_input = params / expected_input / 'disease_parameters.txt'
        if not frozen_input.exists():
            frozen_input.parent.mkdir()
            shutil.copy2(source_input, frozen_input)
        source_dir = Path(old['results']) / key / 'annual/country_summaries'
        masters = list(source_dir.glob('*/summary_master_*.csv.gz'))
        overlays = []
        executed_root=Path(old['output'])/key
        executed_annual=Path(old['results'])/key/'annual'
        if name in ('Brazil', 'Bolivia'):
            campaign = 'br_gm_cap20s_partial_20261117_v3' if name == 'Brazil' else 'br_bo_gm_partial_20261117_v2'
            audit_path = c.BASE / 'results/world' / campaign / 'hybrid_masters' / name / 'hybrid_audit.json'
            receipt = c.load(audit_path)
            if receipt['status'] != 'ok' or receipt['hybrid_sha256'] != expected_sources[name]['sha256']:
                raise ValueError('Latest published hybrid version differs: ' + name)
            masters = [Path(receipt['hybrid_master'])]
            overlays = [str(c.BASE / 'generated/world' / campaign / key / 'production/calibrated.gpkg')]
            executed_root=c.BASE/'generated/world'/campaign/key
            executed_annual=c.BASE/'results/world'/campaign/key/'annual'
            locked.append(audit_path)
        if len(masters) != 1 or not masters[0].is_file():
            raise ValueError('Missing or ambiguous source master: ' + name)
        country.update(status='pending', stage=0, history={}, batches=[], site_count=expected_sources[name]['points'],
            source_master=str(masters[0]), source_master_sha256=expected_sources[name]['sha256'],
            gm_overlays=overlays, final_project=str(latest_project_path), disease_input=str(frozen_input),
            executed_manifest=str(executed_root/'production/annual_configs/manifest.txt'),
            executed_annual=str(executed_annual),
            disease_input_sha256=expected_input, parameter_set=assignment['parameter_set'])
        state['countries'].append(country)
        locked.extend((latest_project_path, frozen_input, Path(country['original']['manifest']), Path(country['original']['project']),
                       Path(country['executed_manifest'])))
    locked.extend(p for p in engine.rglob('*') if p.is_file())
    locked.extend(c.PACK / r['path'] for r in package_manifest['files'])
    state['frozen_files'] = [{'path': str(p), 'sha256': c.sha(p)} for p in sorted(set(locked))]
    c.save(c.STATE, state)
    make_pilot(state)
    c.save(c.GEN / 'bootstrap_complete.json', {'status': 'ok', 'pilot_configs': c.PILOT_CONFIGS, 'countries': 103,
        'country_cells': 16612, 'future_configs_per_country': 750, 'selected_gms_recalibrated': 0,
        'historical_simulations_rerun': 0, 'source_master_policy': 'current publication manifest: Brazil v3, Bolivia v2, other 101 final regional sources'})
    print('BOOTSTRAP_COMPLETE', c.STATE, c.PILOT_CONFIGS,'pilot configs; production gated by numerical replay and daily CO2', flush=True)


if __name__ == '__main__':
    main()
