"""Freeze actual selected GMs and clone only future paired configurations."""
from __future__ import annotations
import argparse
from collections import Counter
import copy
import csv
import gzip
import json
import math
from pathlib import Path
import shutil
import sys

import common as c
from contracts import compare_science


def master_metadata(path, keep, country):
    """Streaming check of every retained cell block; no yield selection."""
    result, counts = {}, Counter()
    with gzip.open(path, 'rt', newline='', encoding='utf-8-sig') as stream:
        reader = csv.reader(stream, delimiter=';')
        head = next(reader)
        pos = {name: head.index(name) for name in ('COUNTRY', 'CELL_ID', 'MATURITY_GROUP', 'WEATHER_SOURCE_CELL_ID',
                                                  'LATITUDE', 'LONGITUDE', 'HARVEST_AREA_HA')}
        for row in reader:
            cell = row[pos['CELL_ID']]
            if cell not in keep:
                continue
            if row[pos['COUNTRY']] != country:
                raise ValueError('Mixed source master country')
            meta = tuple(row[pos[k]] for k in ('MATURITY_GROUP', 'WEATHER_SOURCE_CELL_ID',
                                              'LATITUDE', 'LONGITUDE', 'HARVEST_AREA_HA'))
            if cell in result and result[cell] != meta:
                raise ValueError('Source master point identity/GM changes within cell ' + cell)
            result[cell] = meta
            counts[cell] += 1
    if set(result) != set(keep) or any(n != 4600 for n in counts.values()):
        raise ValueError('Source retained master is not a complete 4600-row/cell matrix')
    return result


def frozen_sites(original, overlays, metadata, keep, target, catalog):
    import fiona
    choices, schema, crs = {}, None, None
    inputs = []
    for path in [Path(original), *map(Path, overlays)]:
        if not path.is_file():
            raise FileNotFoundError('Existing selected-GM file missing: ' + str(path))
        inputs.append({'path': str(path), 'sha256': c.sha(path)})
        with fiona.open(path) as source:
            this_schema = source.schema.copy()
            if schema is None:
                schema, crs = copy.deepcopy(this_schema), source.crs
            else:
                if source.crs != crs:
                    raise ValueError('GM source CRS differs')
                schema['properties'].update(this_schema['properties'])
            for f in source:
                props = dict(f['properties'])
                cell = str(props['CellID'])
                if cell not in keep:
                    continue
                gm, weather, lat, lon, area = metadata[cell]
                if str(props['GM_WIN']) != gm:
                    continue
                xy = f['geometry']['coordinates']
                for a, b, tol in ((xy[0], lon, 1e-4), (xy[1], lat, 1e-4), (props['HAREA'], area, 0.01),
                                  (keep[cell]['HARVEST_AREA_HA'], area, 0.01)):
                    if not math.isclose(float(a), float(b), abs_tol=tol, rel_tol=1e-8):
                        raise ValueError('Source geometry/area mismatch at ' + cell)
                if str(props.get('WTH_CELLID', props['CellID'])) != weather:
                    raise ValueError('Weather borrowing differs at ' + cell)
                expected = catalog[gm]
                if props['INGENO'] != expected['ingeno'] or props['CNAME'] != expected['cname']:
                    raise ValueError('Selected GM/cultivar coefficients mismatch at ' + cell)
                choices[cell] = {'type': 'Feature', 'geometry': dict(f['geometry']), 'properties': props}
    if set(choices) != set(keep):
        raise ValueError('No existing selected-GM feature matches actual master at ' + str(sorted(set(keep) - set(choices), key=int)[:10]))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(target)
    with fiona.open(target, 'w', driver='GPKG', layer='sites', schema=schema, crs=crs) as sink:
        for cell in sorted(choices, key=int):
            feature = choices[cell]
            props = feature['properties']
            feature['properties'] = {key: props.get(key) for key in schema['properties']}
            sink.write(feature)
    return choices, schema, crs, inputs


def write_zones(features, schema, crs, folder, catalog):
    import fiona
    folder.mkdir(parents=True, exist_ok=False)
    grouped = {}
    for feature in features.values():
        gm = str(feature['properties']['GM_WIN'])
        grouped.setdefault(gm, []).append(feature)
    zones = []
    for gm, records in sorted(grouped.items()):
        path = folder / ('GM_' + gm + '.gpkg')
        with fiona.open(path, 'w', driver='GPKG', layer='sites', schema=schema, crs=crs) as sink:
            sink.writerecords(records)
        zones.append({'gm': gm, 'path': str(path), 'count': len(records), **catalog[gm]})
    return zones


def prepare_country(state_path, key):
    state = c.load(state_path)
    country = next(x for x in state['countries'] if x['key'] == key)
    base, ready, result = c.paths(state, country)
    marker = ready / 'reuse_audit.json'
    if marker.exists():
        receipt = c.load(marker)
        if receipt['status'] != 'ok' or receipt['source_master_sha256'] != country['source_master_sha256']:
            raise ValueError('Existing preparation receipt does not match')
        for path, expected in receipt['config_sha256'].items():
            if c.sha(path) != expected:
                raise ValueError('Prepared config changed')
        for path, expected in receipt['sites_sha256'].items():
            if c.sha(path) != expected:
                raise ValueError('Frozen selected sites changed')
        print('PREPARED_REUSE_VERIFIED', country['country'], flush=True)
        return receipt
    if ready.exists():
        raise FileExistsError('Partial preparation preserved; inspect before retry: ' + str(ready))
    ready.mkdir(parents=True)
    master = Path(country['source_master'])
    if c.sha(master) != country['source_master_sha256']:
        raise ValueError('Current master differs from the manuscript source: ' + country['country'])
    frozen = c.population(state['pack'], country['country'])
    keep = {cell: row for (_, cell), row in frozen.items()}
    metadata = master_metadata(master, keep, country['country'])
    catalog = {x['gm']: x for x in c.load(Path(state['pack']) / 'inputs/generic_maturity_candidates.json')['cultivars']}
    features, schema, crs, site_inputs = frozen_sites(country['original']['calibrated'], country['gm_overlays'],
        metadata, keep, ready / 'calibrated.gpkg', catalog)
    zones = write_zones(features, schema, crs, ready / 'zone_sites', catalog)
    print('GMS_FROZEN', country['country'], len(features), Counter(m[0] for m in metadata.values()), flush=True)
    original_project = c.load(country['original']['project'])
    final_project = c.load(country['final_project'])
    # Historical period definitions remain for labelling the final combined master.
    # Per-year EXECUTED configurations below are the calendar authority. Project
    # metadata can differ between releases without changing those inputs.
    original_project['profiles']['hipergator']['disease_parameters'] = country['disease_input']
    original_project['profiles']['hipergator']['work_root'] = str(Path(state['output']) / 'work')
    original_project['experiment']['regional_parameter_assignment'] = final_project['experiment']['regional_parameter_assignment']
    c.save(base / 'project.json', original_project)
    config_dir = ready / 'annual_configs'
    config_dir.mkdir()
    source_paths = [Path(x.strip()) for x in Path(country['original']['manifest']).read_text().splitlines() if x.strip()]
    if len(source_paths) != 920:
        raise ValueError('Original 920-configuration matrix missing')
    reuse_receipt = Path(state['previous_output']) / key / 'production/reuse_audit.json'
    previous_reuse = c.load(reuse_receipt) if reuse_receipt.is_file() else {}
    hashes = previous_reuse.get('original_config_sha256', {})
    calendar_hashes = previous_reuse.get('original_calendar_sha256', {})
    checked_inputs = {}
    executed={}
    for line in Path(country['executed_manifest']).read_text().splitlines():
        if not line.strip(): continue
        executed_path=Path(line.strip()); configuration=c.load(executed_path)
        info=configuration['pipeline']
        if info['scenario']=='historical': continue
        identity=int(info['simulation_year']),info['scenario'],info['gcm']
        if identity in executed: raise ValueError('Duplicate executed reference combination')
        executed[identity]=(executed_path,configuration)
    if set(executed)!=c.matrix(): raise ValueError('Executed reference future matrix incomplete')
    executed_proofs={}
    observed, new_paths, origins = set(), [], {}
    for source_path in source_paths:
        source = c.load(source_path)
        meta = source['pipeline']
        if meta['scenario'] == 'historical':
            continue
        identity = int(meta['simulation_year']), meta['scenario'], meta['gcm']
        if identity in observed or identity not in c.matrix():
            raise ValueError('Invalid/duplicate future configuration identity')
        observed.add(identity)
        old_hash = c.sha(source_path)
        if str(source_path) in hashes:
            if hashes[str(source_path)] != old_hash:
                raise ValueError('Original configuration changed after completed campaign')
        else:
            audit = c.load(Path(country['original']['annual']) / 'tasks' / source['name'] / 'audit.json')
            if audit.get('status') != 'ok' or audit.get('source_config_sha256') != old_hash:
                raise ValueError('Original executed configuration audit mismatch')
        calendar_path = Path(meta['calendar']['source'])
        if str(calendar_path) not in checked_inputs:
            previous_calendar = calendar_hashes.get(str(calendar_path))
            if previous_calendar is None:
                original_audit = c.load(Path(country['original']['annual']) / 'tasks' / source['name'] / 'audit.json')
                previous_calendar = original_audit['calendar']['sha256']
            current_calendar = c.sha(calendar_path)
            if current_calendar != previous_calendar:
                raise ValueError('Executed GGCMI calendar changed: ' + str(calendar_path))
            checked_inputs[str(calendar_path)] = current_calendar
        library = meta.get('cultivar_library')
        if library and (c.sha(library) != meta['cultivar_library_sha256']):
            raise ValueError('Executed cultivar library changed')
        template = Path(source['templateDir']) / 'SBG00001.SBX'
        if c.sha(template) != c.TEMPLATE_SHA:
            raise ValueError('Original rendered template authority changed')
        new = c.clone_config(source, zones, state, country, ready, country['disease_input'],
                             country['disease_input_sha256'], Path(state['engine']) / 'templates')
        executed_path,reference=executed[identity]
        if reference['pipeline']['disease_parameters_sha256']!=country['disease_input_sha256']:
            raise ValueError('Regional disease parameter set differs from the last executed production config')
        executed_hash=c.sha(executed_path)
        recorded=previous_reuse.get('config_sha256',{}).get(str(executed_path))
        if recorded is None:
            prior_audit=c.load(Path(country['executed_annual'])/'tasks'/reference['name']/'audit.json')
            if prior_audit.get('status')!='ok': raise ValueError('Executed reference is not successful')
            recorded=prior_audit['source_config_sha256']
        if recorded!=executed_hash: raise ValueError('Executed regional/GM reference config changed')
        compare_science(reference,new,allow_missing_control=True)
        compare_science(source,new)
        executed_proofs[str(executed_path)]=executed_hash
        new['pipeline']['co2_rerun'].update(source_config=str(source_path), source_config_sha256=old_hash,
                                          source_master=str(master), source_master_sha256=country['source_master_sha256'])
        path = config_dir / (new['name'] + '.json')
        c.save(path, new)
        new_paths.append(path)
        origins[str(path)] = old_hash
    if observed != c.matrix() or len(new_paths) != 750:
        raise ValueError('Future matrix is not the complete 750 configurations')
    (config_dir / 'manifest.txt').write_text(''.join(str(p) + '\n' for p in new_paths), encoding='utf-8')
    with (ready / 'gm_frozen.csv').open('x', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['COUNTRY', 'CELL_ID', 'GM', 'WEATHER_SOURCE_CELL_ID', 'LATITUDE', 'LONGITUDE', 'HARVEST_AREA_HA'])
        writer.writerows((country['country'], cell, *metadata[cell]) for cell in sorted(metadata, key=int))
    receipt = {'status': 'ok', 'source_master': str(master), 'source_master_sha256': country['source_master_sha256'],
        'points': len(features), 'future_configurations': 750, 'historical_configs_rerun': 0, 'gm_recalibrations': 0,
        'config_sha256': {str(p): c.sha(p) for p in new_paths},
        'sites_sha256': {str(z['path']): c.sha(z['path']) for z in zones}, 'source_selected_sites': site_inputs,
        'executed_calendar_sha256': checked_inputs,
        'executed_reference_config_sha256': executed_proofs,
        'scientific_contract': 'all 750 future configs match original paired inputs AND last executed regional/GM campaign; only SSP CO2 and output locations change',
        'config_source_sha256': origins, 'population_sha256': c.sha(Path(state['pack']) / 'inputs/population.csv')}
    c.save(marker, receipt)
    print('PREPARATION_COMPLETE', country['country'], len(features), 'fixed GMs; 750 future configs', flush=True)
    return receipt


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('state', type=Path)
    p.add_argument('key',nargs='?')
    p.add_argument('--index',type=int)
    a = p.parse_args()
    key = c.load(a.state)['countries'][a.index]['key'] if a.index is not None else a.key
    if key is None: p.error('A key or --index is required')
    prepare_country(a.state, key)


if __name__ == '__main__':
    main()
