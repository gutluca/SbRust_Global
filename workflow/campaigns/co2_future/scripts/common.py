"""Contracts shared by the CO2-only future rerun. No scientific parameter fitting."""
from __future__ import annotations
import copy
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re

VERSION = 'co2_future_global_20261119_v2'
BASE = Path('/blue/wpavan/gustavo.luca/SBrust_world')
PACK = BASE / 'releases' / VERSION
GEN = BASE / 'generated/world' / VERSION
RESULTS = BASE / 'results/world' / VERSION
STATE = GEN / 'state.json'
EXE_SHA = '811a53c4d85d2f47ecf7815d9c46bc75fbc5fe5dbd2dd26aae3fce606f2ba41e'
TEMPLATE_SHA = '519c7bdbf7a175893ba7b20b02547e84d1bb5ce28d47c76d0edc76f93be487fe'
OLD_STATE = BASE / 'generated/world/global_disease_only_20261022_v1/state.json'
GCMS = ('GFDL-ESM4', 'IPSL-CM6A-LR', 'MPI-ESM1-2-HR', 'MRI-ESM2-0', 'UKESM1-0-LL')
SSPS = ('ssp126', 'ssp370', 'ssp585')
PLANTS = ('P01', 'P02', 'P03', 'P04', 'P05')
BAD_NODES = ('c0706a-s23', 'c0706a-s27')
EXCLUDED_NODES = ','.join(BAD_NODES)
PILOT_COUNTRIES = ('Brazil', 'Bolivia', 'United States')
PILOT_YEARS = (2020,2069)
PILOT_GCMS = GCMS
PILOT_CASES = len(PILOT_COUNTRIES)*len(PILOT_YEARS)*len(PILOT_GCMS)*len(SSPS)
PILOT_CONFIGS = 2*PILOT_CASES
STAGES = ('prepare', 'annual_first', 'annual_rest', 'master')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.partial')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def paths(state, country):
    base = Path(state['output']) / country['key']
    return base, base / 'production', Path(state['results']) / country['key']


def matrix():
    return {(year, ssp, gcm) for year in range(2020, 2070) for ssp in SSPS for gcm in GCMS}


def pilot_matrix():
    return {(country,year,ssp,gcm) for country in PILOT_COUNTRIES for year in PILOT_YEARS
            for ssp in SSPS for gcm in PILOT_GCMS}


def population(pack, country=None):
    with (Path(pack) / 'inputs/population.csv').open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    result = {}
    for row in rows:
        if country is not None and row['COUNTRY'] != country:
            continue
        key = row['COUNTRY'], row['CELL_ID']
        if key in result or not math.isfinite(float(row['HARVEST_AREA_HA'])) or float(row['HARVEST_AREA_HA']) <= 0:
            raise ValueError('Invalid/duplicate frozen population')
        result[key] = row
    if not result:
        raise ValueError('Empty frozen population')
    return result


def replace(value, mapping):
    if isinstance(value, dict):
        return {k: replace(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [replace(v, mapping) for v in value]
    if isinstance(value, str):
        for old, new in mapping.items():
            value = value.replace(old, new)
    return value


def safe_name(text):
    return re.sub(r'[^A-Za-z0-9_-]+', '_', text).strip('_')


def assert_allowed_node(hostname):
    short = hostname.split('.')[0]
    if short in BAD_NODES:
        raise RuntimeError('Job allocated on excluded storage-failure node: ' + short)


if os.environ.get('SLURM_JOB_ID'):
    import socket
    assert_allowed_node(socket.gethostname())


def clone_config(source, zones, state, country, target_root, disease_input, disease_sha, template_dir):
    """Preserve the executed weather, calendar, soil and management expressions.

    Cultivar/site runs are reconstructed from the SAME arm/planting prototypes
    using existing selected GM-zone files. No cultivar coefficients change.
    """
    result = copy.deepcopy(source)
    meta = result['pipeline']
    year, ssp, gcm = int(meta['simulation_year']), meta['scenario'], meta['gcm']
    if (year, ssp, gcm) not in matrix():
        raise ValueError('Not a future production combination')
    if source['default_setup']['nyers'] != 1 or not meta.get('annual'):
        raise ValueError('Expected annual NYERS=1 source')
    if tuple(meta['planting_labels']) != PLANTS:
        raise ValueError('Five original planting alternatives required')
    if Path(source['dssat']['executable']).resolve() != Path(state['executable']).resolve():
        raise ValueError('Executed model differs from frozen binary')
    old_input = str(meta['disease_parameters_snapshot'])
    includes = result['default_setup']['include']
    if old_input not in includes:
        raise ValueError('Missing original parameter snapshot in source config')
    if any(Path(p).name.upper().startswith('CO2') for p in includes):
        raise ValueError('Unexpected existing CO2 override')
    result = replace(result, {old_input: str(disease_input)})
    prototypes = {}
    prototype_contracts = {}
    for run in source['runs']:
        arm = 'disease' if run['name'].startswith('disease_') else 'control' if run['name'].startswith('control_') else None
        if arm is None or run['dises'] != ('Y' if arm == 'disease' else 'N'):
            raise ValueError('Source is not an ordinary paired production config')
        identity = arm, run['planting_label']
        old_sites = run.get('sites', source['default_setup'].get('sites', ''))
        if not old_sites.startswith('xy_from_vector::'):
            raise ValueError('Source sites are not vector expressions')
        normalized = replace(copy.deepcopy(run), {old_sites[len('xy_from_vector::'):]: '<SELECTED_ZONE>'})
        for field in ('name', 'sites', 'ingeno', 'cname', 'zone_id'):
            normalized.pop(field, None)
        if identity in prototype_contracts and normalized != prototype_contracts[identity]:
            raise ValueError('Source management differs among GM zones; explicit preservation required')
        prototype_contracts[identity] = normalized
        prototypes.setdefault(identity, run)
    if set(prototypes) != {(a, p) for a in ('disease', 'control') for p in PLANTS}:
        raise ValueError('Incomplete paired planting prototypes')
    runs = []
    for zone in zones:
        for plant in PLANTS:
            for arm in ('disease', 'control'):
                original = prototypes[arm, plant]
                old_sites = original.get('sites', source['default_setup'].get('sites', ''))
                if not old_sites.startswith('xy_from_vector::'):
                    raise ValueError('Source sites are not vector expressions')
                raw_sites = old_sites[len('xy_from_vector::'):]
                run = replace(copy.deepcopy(original), {raw_sites: str(zone['path'])})
                run.update(name=f'{arm}_{plant}_GM_{zone["gm"]}', sites='xy_from_vector::' + str(zone['path']),
                           ingeno=zone['ingeno'], cname=zone['cname'], zone_id='GM_' + zone['gm'])
                runs.append(run)
    result['runs'] = runs
    result['name'] = f'co2v2_{country["key"]}_{meta["period"]}_{ssp}_{gcm}_y{year}'
    result['workDir'] = str(Path(state['output']) / 'work' / result['name'])
    result['templateDir'] = str(template_dir)
    meta = result['pipeline']
    forcing = load(Path(state['pack']) / 'inputs/co2/provenance.json')['files'][ssp]
    meta.update(disease_parameters_snapshot=str(disease_input), disease_parameters_source=str(disease_input),
                disease_parameters_sha256=disease_sha, treatments_per_site=10, disease_treatments_per_site=5,
                country_summary_dir=str(Path(state['results']) / country['key'] / 'annual/country_summaries'),
                co2_forcing={'scenario': ssp, 'path': str(Path(state['pack']) / forcing['relative_path']),
                             'sha256': forcing['sha256'], 'annual_ppm': forcing['annual_ppm'],
                             'unit': 'ppm', 'implementation': 'annual step from January 1', 'switch': 'M'},
                co2_rerun={'version': VERSION, 'no_gm_recalibration': True, 'expected_points': sum(z['count'] for z in zones)})
    # These are literal copies of the original executed forcing and calendar.
    for key in ('weatherDir', 'ghr_root', 'dssat'):
        if result[key] != source[key]:
            raise ValueError('Unauthorized forcing/model change: ' + key)
    if result['pipeline']['calendar'] != source['pipeline']['calendar']:
        raise ValueError('Calendar changed')
    for key, value in source['default_setup'].items():
        if key != 'include' and result['default_setup'][key] != value:
            raise ValueError('Management/default expression changed: ' + key)
    return result
