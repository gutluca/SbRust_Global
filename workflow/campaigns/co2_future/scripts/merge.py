"""Use the existing native collators, then retain historical rows verbatim."""
from __future__ import annotations
import argparse
from collections import Counter
import csv
import gzip
import hashlib
import heapq
import itertools
from pathlib import Path
import subprocess
import sys

import common as c


def row_key(row, indices):
    return (int(row[indices['CELL_ID']]), int(row[indices['PERIOD']].split('-')[0]),
            int(row[indices['SIM_YEAR']]), row[indices['SCENARIO']],
            row[indices['GCM']], row[indices['PLANTING_LABEL']])


def combine(old, future, destination, expected_country, expected_gms):
    if Path(destination).exists():
        raise FileExistsError(destination)
    baseline_digest = hashlib.sha256()
    n_baseline = n_future = 0
    with gzip.open(old, 'rt', newline='', encoding='utf-8') as src, gzip.open(future, 'rt', newline='', encoding='utf-8') as new:
        old_header, new_header = src.readline(), new.readline()
        if next(csv.reader([old_header], delimiter=';')) != next(csv.reader([new_header], delimiter=';')):
            raise ValueError('Canonical master schemas differ')
        fields = next(csv.reader([new_header], delimiter=';'))
        ix = {name: i for i, name in enumerate(fields)}
        def records(stream, historical):
            previous = None
            for line in stream:
                row = next(csv.reader([line], delimiter=';'))
                if row[ix['CELL_ID']] not in expected_gms:
                    if not historical:
                        raise ValueError('New future contains excluded/unknown point')
                    continue
                if historical and row[ix['SCENARIO']] != 'historical':
                    continue
                key = row_key(row, ix)
                if previous is not None and key <= previous:
                    raise ValueError('Source master has duplicate or out-of-order rows')
                previous = key
                yield key, historical, row, line
        merged = heapq.merge(records(src, True), records(new, False), key=lambda item: item[0])
        temp = Path(str(destination) + '.partial')
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        points = set()
        with gzip.open(temp, 'wt', newline='', encoding='utf-8', compresslevel=3) as dst:
            dst.write(new_header)
            for cell, group in itertools.groupby(merged, key=lambda item: item[2][ix['CELL_ID']]):
                if cell in points:
                    raise ValueError('Non-contiguous point')
                points.add(cell)
                seen = set()
                historical_count = future_count = 0
                for key, historical, row, line in group:
                    if key in seen:
                        raise ValueError('Duplicate combined simulation')
                    seen.add(key)
                    if row[ix['COUNTRY']] != expected_country or row[ix['MATURITY_GROUP']] != expected_gms[cell]:
                        raise ValueError('Country or fixed maturity group changed')
                    year, ssp = int(row[ix['SIM_YEAR']]), row[ix['SCENARIO']]
                    valid = (historical and ssp == 'historical' and 1981 <= year <= 2014) or (
                        not historical and ssp in c.SSPS and 2020 <= year <= 2069)
                    if not valid or row[ix['GCM']] not in c.GCMS or row[ix['PLANTING_LABEL']] not in c.PLANTS:
                        raise ValueError('Unexpected combined simulation identity')
                    expected_id = '|'.join(row[ix[name]] for name in ('COUNTRY', 'CELL_ID', 'PERIOD', 'SIM_YEAR', 'SCENARIO', 'GCM', 'PLANTING_LABEL'))
                    if row[ix['SIMULATION_ID']] != expected_id:
                        raise ValueError('Canonical simulation ID differs')
                    dst.write(line)
                    if historical:
                        historical_count += 1
                        n_baseline += 1
                        baseline_digest.update(line.encode('utf-8'))
                    else:
                        future_count += 1
                        n_future += 1
                if historical_count != 850 or future_count != 3750 or len(seen) != 4600:
                    raise ValueError(f'Incomplete combined matrix for {cell}: {historical_count} historical, {future_count} future')
        if points != set(expected_gms):
            raise ValueError('Combined population differs')
    temp.rename(destination)
    return {'status': 'ok', 'points': len(points), 'historical_rows_preserved': n_baseline,
            'corrected_future_rows': n_future, 'historical_rows_sha256_uncompressed': baseline_digest.hexdigest(),
            'historical_rows_copied_verbatim': True, 'all_selected_gms_unchanged': True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('state', type=Path)
    p.add_argument('key')
    a = p.parse_args()
    state = c.load(a.state)
    country = next(x for x in state['countries'] if x['key'] == a.key)
    base, ready, result = c.paths(state, country)
    annual = result / 'annual'
    manifest = ready / 'annual_configs/manifest.txt'
    paths = [Path(p) for p in manifest.read_text().splitlines() if p.strip()]
    for path in paths:
        shard = annual / 'tasks' / path.stem
        audit = c.load(shard / 'audit.json')
        co2 = c.load(shard / 'co2_audit.json')
        if audit.get('status') != 'ok' or audit['source_config_sha256'] != c.sha(path) or co2.get('status') != 'ok':
            raise ValueError('Unaudited task cannot enter future master')
        forcing = c.load(path)['pipeline']['co2_forcing']
        if co2['forcing_sha256'] != forcing['sha256'] or co2['scenario'] != forcing['scenario']:
            raise ValueError('Published CO2 audit differs from forcing')
    engine = Path(state['engine']) / 'scripts'
    def call(name, *args):
        subprocess.run([sys.executable, str(engine / name), *map(str, args)], check=True)
    call('merge_country_shards.py', manifest, annual)
    stem = c.safe_name(country['country'])
    wide = annual / '_pipeline/merged_wide' / stem
    pairs = annual / '_pipeline/paired_long' / stem
    pairs.mkdir(parents=True, exist_ok=False)
    for kind in ('disease', 'control'):
        call('export_analysis_table.py', wide / f'summary_{kind}_{stem}.csv.gz',
             pairs / f'summary_{kind}_{stem}.csv.gz', '--kind', kind,
             '--audit', result / f'export_{kind}_audit.json',
             '--temporary-root', Path(__import__('os').environ.get('TMPDIR') or __import__('os').environ['SLURM_TMPDIR']))
    subprocess.run([sys.executable, str(Path(state['pack']) / 'scripts/native_finalize.py'), str(engine), 'validate',
                    str(pairs / f'summary_disease_{stem}.csv.gz'), str(pairs / f'summary_control_{stem}.csv.gz'),
                    str(result / 'paired_validation.json')], check=True)
    future = result / f'summary_future_{stem}.csv.gz'
    subprocess.run([sys.executable, str(Path(state['pack']) / 'scripts/native_finalize.py'), str(engine), 'master',
                    str(pairs / f'summary_disease_{stem}.csv.gz'), str(pairs / f'summary_control_{stem}.csv.gz'),
                    str(future), str(base / 'project.json'), str(result / 'future_master_audit.json')], check=True)
    if c.sha(country['source_master']) != country['source_master_sha256']:
        raise ValueError('Historical source master changed')
    with (ready / 'gm_frozen.csv').open(newline='', encoding='utf-8') as stream:
        gms = {r['CELL_ID']: r['GM'] for r in csv.DictReader(stream)}
    destination = result / f'summary_master_{stem}.csv.gz'
    audit = combine(Path(country['source_master']), future, destination, country['country'], gms)
    audit.update(source_master=country['source_master'], source_master_sha256=country['source_master_sha256'],
                 corrected_future_master=str(future), corrected_future_sha256=c.sha(future),
                 output=str(destination), output_sha256=c.sha(destination))
    c.save(result / 'complete.json', audit)
    print('COUNTRY_COMPLETE', country['country'], destination, audit['output_sha256'], flush=True)


if __name__ == '__main__':
    main()
