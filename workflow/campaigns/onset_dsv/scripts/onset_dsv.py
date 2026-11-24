"""Replay published DSV schedules with an explicitly approximate onset gate.

No DSSAT execution; original crop/weather cache and disease master are read-only.
Calendar assumption (user selected): plant date + 5 + printed onset DAE.
The printed severity onset is not spore arrival or first latent infection.
"""
import argparse
from array import array
from collections import Counter, deque
import csv
from datetime import date, timedelta
import gzip
from itertools import zip_longest
import json
import math
import os
from pathlib import Path
import struct

import analyze_beruski_trends as a
import ensemble3_global as e

RECORD = struct.Struct('<Ih')
COUNTRIES = ('Brazil', 'United States')
POLICIES = [(rule, residual) for rule in ('DSV50', 'DSV80') for residual in (14, 16)]
METHODS = ('ORIGINAL_DSV50', 'ORIGINAL_DSV80', 'ONSET_DSV50', 'ONSET_DSV80')
POLICY = dict(onset='first positive printed DISMO severity from original disease master',
    calendar='PLANT_DATE + 5 calendar days + DISEASE_ONSET_DAE; approximate, no DAE offset correction',
    eligibility='unchanged original YMAX0 daily-cache eligible mask',
    weather='unchanged prior30days weighted15_15 trailing3day mean',
    residual='block R complete days; earliest next = last + R + 1',
    no_onset='empty or -99 in verified original master: no gated alerts',
    cap='min(raw count,6); no efficacy simulated',
    interpretation='hypothetical warning-policy adoption, not demonstrated control')


def onset_value(raw):
    if raw.strip() in ('', '-99'):
        return -1
    value = float(raw)
    if not math.isfinite(value) or value < 0 or not value.is_integer() or value > 32766:
        raise ValueError(f'Invalid onset DAE: {raw!r}')
    return int(value)


def onset_date(plant, dae):
    return None if dae == -1 else plant + timedelta(days=5 + dae)


def export(master, sha, country, population, out):
    if out.exists():
        raise FileExistsError(out)
    if a.digest(master) != sha:
        raise ValueError('Original disease master checksum mismatch')
    cells = sorted(population['retained'][country], key=int)
    indices = {cell: i for i, cell in enumerate(cells)}
    onset = array('h', [-32768]) * (len(cells) * e.N)
    plants = array('I', [0]) * len(onset)
    stats = Counter()
    with gzip.open(master, 'rt', encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream, delimiter=';')
        head = next(reader); ix = {v: i for i, v in enumerate(head)}
        for row in reader:
            get = lambda key: row[ix[key]]
            if get('COUNTRY') != country:
                raise ValueError('Mixed country')
            cell = str(int(get('CELL_ID')))
            if cell not in indices:
                if cell not in population['excluded'][country]:
                    raise ValueError('Unknown point')
                continue
            year = int(get('SIM_YEAR'))
            group = a.group_index(get('PERIOD'), get('SCENARIO'), year)
            gcm, planting = get('GCM'), get('PLANTING_LABEL')
            if get('SIMULATION_ID') != e.identity(country, cell, group, year, gcm, planting):
                raise ValueError('Invalid simulation key')
            i = indices[cell] * e.N + e.slot(group, year, gcm, planting)
            if onset[i] != -32768:
                raise ValueError('Duplicate original simulation')
            onset[i] = onset_value(get('DISEASE_ONSET_DAE'))
            plants[i] = date.fromisoformat(get('PLANT_DATE')).toordinal()
            stats['rows'] += 1; stats['no_onset' if onset[i] == -1 else 'with_onset'] += 1
            if stats['rows'] % 1000000 == 0:
                print('EXPORTED', country, stats['rows'], flush=True)
    if -32768 in onset:
        raise ValueError('Incomplete original simulation matrix')
    out.mkdir(parents=True)
    path = out / 'onset.bin.gz'
    with gzip.open(path, 'wb', compresslevel=6) as stream:
        block = bytearray()
        for plant, dae in zip(plants, onset):
            block.extend(RECORD.pack(plant, dae))
            if len(block) >= 1048576:
                stream.write(block); block.clear()
        stream.write(block)
    a.save(out / 'metadata.json', dict(country=country, cells=cells, records=len(onset),
        record_format='<Ih plant ordinal, onset DAE (-1=no onset)', source=str(master),
        source_sha256=sha, sha256=a.digest(path), stats=dict(stats), policy=POLICY))


class Original:
    def __init__(self, folder):
        self.meta = a.load(folder / 'metadata.json')
        path = folder / 'onset.bin.gz'
        if a.digest(path) != self.meta['sha256']:
            raise ValueError('Packaged onset data changed')
        self.cells = {v: i for i, v in enumerate(self.meta['cells'])}
        with gzip.open(path, 'rb') as f:
            self.data = f.read()
        if len(self.data) != self.meta['records'] * RECORD.size:
            raise ValueError('Truncated onset data')

    def get(self, row):
        cell = str(int(row['cell_id'])); year = int(row['simulation_year'])
        group = a.group_index(row['period'], row['scenario'], year)
        expected = e.identity(row['country'], cell, group, year, row['gcm'], row['planting_label'])
        if row['simulation_id'] != expected or row['country'] != self.meta['country']:
            raise ValueError('Original/cache simulation key mismatch')
        i = self.cells[cell] * e.N + e.slot(group, year, row['gcm'], row['planting_label'])
        plant, dae = RECORD.unpack_from(self.data, i * RECORD.size)
        if date.fromordinal(plant).isoformat() != row['plant_date']:
            raise ValueError('Original/cache planting date mismatch')
        return dae, onset_date(date.fromordinal(plant), dae)


def dsv_values(rain):
    history = deque(maxlen=30); means = deque(maxlen=3); result = []
    for value in rain:
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('Invalid daily rain')
        score = None
        if len(history) == 30:
            values = tuple(history)
            def partial(seq):
                wet = [v for v in seq if v > .5]
                return -2.1433 + .3622 * sum(wet) + 2.573 * len(wet)
            means.append(.7 * partial(values[15:]) + .3 * partial(values[:15]))
            if len(means) == 3:
                score = sum(means) / 3
        result.append(score); history.append(value)
    return result


def schedule(scores, eligible, first=0):
    events = {p: [] for p in POLICIES}
    if len(scores) != len(eligible) or any(v not in (0, 1) for v in eligible):
        raise ValueError('Invalid eligible cache mask')
    if first is None:
        return events
    for i, (score, ok) in enumerate(zip(scores, eligible)):
        if i < first or not ok or score is None:
            continue
        for (rule, residual), days in events.items():
            if days and i - days[-1] <= residual:
                continue
            thresholds = (50, 45, 35, 30) if rule == 'DSV50' else (80, 60, 55, 50)
            if score >= thresholds[min(len(days), 3)]:
                days.append(i)
    return events


def recount(row, cache, original):
    if cache['simulation_id'] != row['simulation_id']:
        raise ValueError('Cache row alignment mismatch')
    start = date.fromisoformat(cache['start'])
    rain, eligible = cache['rain'], cache['eligible']
    if len(rain) != len(eligible) or not rain:
        raise ValueError('Truncated daily cache')
    scores = dsv_values(rain)
    old = schedule(scores, eligible)
    for (rule, residual), days in old.items():
        prefix = f'{rule.lower()}_r{residual}'
        if int(row[prefix + '_uncapped']) != len(days) or int(row[prefix + '_cap6']) != min(6, len(days)):
            raise ValueError(f'Original DSV count parity failed: {row["simulation_id"]}/{prefix}')
        dates = ';'.join((start + timedelta(days=i)).isoformat() for i in days)
        if row[prefix + '_dates'] != dates:
            raise ValueError('Original DSV date parity failed')
    dae, onset = original.get(row)
    first = None if onset is None else (onset - start).days
    new = schedule(scores, eligible, first)
    identifiers = ('simulation_id','country','cell_id','period','simulation_year','scenario','gcm',
                   'planting_label','plant_date','maturity_date','weather_source_cell_id',
                   'maturity_group','harvest_area_ha','latitude','longitude')
    result = {key: row[key] for key in identifiers}
    result.update(disease_onset_dae='' if dae == -1 else dae,
        onset_date_approx='' if onset is None else onset.isoformat(), emergence_lag_assumed_days=5,
        onset_status='not_recorded' if onset is None else 'after_window' if not any(
            ok and i >= first for i, ok in enumerate(eligible)) else 'within_or_before_window',
        eligible_days_original=sum(eligible),
        eligible_days_after_onset=0 if first is None else sum(ok and i >= first for i, ok in enumerate(eligible)))
    for label, schedules in [('original', old), ('onset', new)]:
        for (rule, residual), days in schedules.items():
            prefix = f'{label}_{rule.lower()}_r{residual}'
            result[prefix + '_uncapped'] = len(days)
            result[prefix + '_cap6'] = min(6, len(days))
            result[prefix + '_dates'] = ';'.join((start + timedelta(days=i)).isoformat() for i in days)
    return result


def prepare(source, out):
    if out.exists(): raise FileExistsError(out)
    complete = a.load(source / 'complete.json'); plan = a.load(source / 'plan.json')
    if complete.get('status') != 'completed' or set(complete['countries']) != set(COUNTRIES):
        raise ValueError('Both original countries must be complete')
    if len(plan['tasks']) != 7360 or len({t['name'] for t in plan['tasks']}) != 7360:
        raise ValueError('Unexpected source task matrix')
    out.mkdir(); (out / 'tasks').mkdir()
    a.save(out / 'plan.json', dict(source=str(source), source_plan_sha256=a.digest(source/'plan.json'),
        source_complete_sha256=a.digest(source/'complete.json'), tasks=plan['tasks'], policy=POLICY))
    print('PREPARED', len(plan['tasks']), 'cached partitions; DSSAT will NOT be executed')


def process_task(source, out, task, original, population):
    dest = out / 'tasks' / task['name']
    prior = source / 'tasks' / task['name']
    cache_path = prior/'daily_cache.jsonl.gz'; schedules_path = prior/'schedules.csv.gz'
    audit = a.load(prior/'multi_audit.json'); summary = a.load(prior/'summary.json')
    if (audit['task_sha256'] != task['task_sha256'] or summary['status'] != 'ok'
        or a.digest(cache_path) != audit['daily_cache_sha256']
        or a.digest(schedules_path) != summary['output_sha256']):
        raise ValueError(f'Source validation failed: {prior}')
    provenance = dict(task_sha256=task['task_sha256'], cache_sha256=audit['daily_cache_sha256'],
        schedules_sha256=summary['output_sha256'], onset_sha256=original.meta['sha256'], policy=POLICY)
    if dest.exists():
        done = a.load(dest/'complete.json')
        if done['provenance'] != provenance or a.digest(dest/'schedules.csv.gz') != done['sha256']:
            raise ValueError('Existing result is invalid; refusing overwrite')
        print('REUSED', task['name'], flush=True); return
    stage = dest.with_name(dest.name + '.partial.' + str(os.getpid()))
    stage.mkdir(); count=0; excluded=0; seen=set(); stats=Counter()
    with gzip.open(cache_path,'rt',encoding='utf-8') as caches, gzip.open(schedules_path,'rt',newline='',encoding='utf-8') as src, gzip.open(stage/'schedules.csv.gz','wt',newline='',encoding='utf-8',compresslevel=1) as dst:
        writer = None
        for row, line in zip_longest(csv.DictReader(src), caches):
            if row is None or line is None: raise ValueError('Daily-cache row count mismatch')
            cache = json.loads(line)
            if row['simulation_id'] != cache['simulation_id']: raise ValueError('Cache identity mismatch')
            cell = str(int(row['cell_id'])); country = task['country']
            if row['country'] != country: raise ValueError('Mixed country')
            if cell not in population['retained'][country]:
                if cell not in population['excluded'][country]: raise ValueError('Unknown cell')
                excluded += 1; continue
            meta = population['retained'][country][cell]
            for field in ('harvest_area_ha','latitude','longitude'):
                if not math.isclose(float(row[field]),float(meta[field]),rel_tol=1e-8,abs_tol=1e-4):
                    raise ValueError('Population metadata mismatch')
            if row['simulation_id'] in seen: raise ValueError('Duplicate simulation')
            seen.add(row['simulation_id'])
            record = recount(row, cache, original)
            if writer is None:
                writer=csv.DictWriter(dst,fieldnames=list(record)); writer.writeheader()
            writer.writerow(record); count+=1; stats[record['onset_status']]+=1
    if count == 0: raise ValueError('Empty retained partition')
    a.save(stage/'complete.json',dict(status='completed',rows=count,excluded_rows=excluded,
        onset_status=dict(stats),provenance=provenance,sha256=a.digest(stage/'schedules.csv.gz')))
    stage.rename(dest); print('DONE', task['name'], count, dict(stats), flush=True)


def worker(addon, out, index, workers):
    plan=a.load(out/'plan.json'); source=Path(plan['source'])
    if a.digest(source/'plan.json') != plan['source_plan_sha256'] or a.digest(source/'complete.json') != plan['source_complete_sha256']:
        raise ValueError('Source campaign manifest changed')
    pop=a.load(addon/'population.json'); originals={}
    if not 0 <= index < workers: raise ValueError('Invalid worker index')
    for task in plan['tasks'][index::workers]:
        country=task['country']
        if country not in originals:
            originals[country]=Original(addon/'global'/country.replace(' ','_'))
        process_task(source,out,task,originals[country],pop)


def analyze(addon, out, country):
    plan=a.load(out/'plan.json'); pop=a.load(addon/'population.json')
    expected=pop['retained'][country]; folder=out/country.replace(' ','_')
    if folder.exists(): raise FileExistsError(folder)
    folder.mkdir(); acc={}; rows=0; stats=Counter(); writer=None
    columns=[f'{mode}_{rule.lower()}_r{residual}_{cap}' for mode in ('original','onset')
             for rule,residual in POLICIES for cap in ('cap6','uncapped')]
    with gzip.open(folder/'summary_onset_dsv.csv.gz','wt',newline='',encoding='utf-8',compresslevel=1) as dst:
        for task in plan['tasks']:
            if task['country'] != country: continue
            path=out/'tasks'/task['name']; done=a.load(path/'complete.json')
            if done['status']!='completed' or a.digest(path/'schedules.csv.gz')!=done['sha256']:
                raise ValueError('Incomplete/changed replay partition')
            with gzip.open(path/'schedules.csv.gz','rt',newline='',encoding='utf-8') as src:
                reader=csv.DictReader(src)
                if writer is None:
                    writer=csv.DictWriter(dst,fieldnames=reader.fieldnames);writer.writeheader()
                elif writer.fieldnames!=reader.fieldnames: raise ValueError('Schema mismatch')
                for row in reader:
                    group=a.group_index(row['period'],row['scenario'],int(row['simulation_year']))
                    key=(row['cell_id'],group,a.GCMS.index(row['gcm']),a.PLANTS.index(row['planting_label']))
                    a.add_member(acc,key,int(row['simulation_year']),[int(row[c]) for c in columns])
                    writer.writerow(row); rows+=1;stats[row['onset_status']]+=1
    if rows!=len(expected)*e.N or {k[0] for k in acc}!=set(expected):
        raise ValueError('Incomplete country simulation matrix')
    nation={}; area=sum(float(v['harvest_area_ha']) for v in expected.values())
    def points():
        for cell in sorted(expected,key=int):
            meta=expected[cell];weight=float(meta['harvest_area_ha']);base=a.means(acc,cell,0)
            for group in range(1,len(a.GROUPS)):
                future=a.means(acc,cell,group)
                for j in range(16):
                    method=METHODS[j//4];residual=(14,16)[(j%4)//2];cap=('cap6','uncapped')[j%2]
                    b=math.fsum(v[j] for v in base)/5;f=math.fsum(v[j] for v in future)/5
                    for gcm,bv,fv in [('ALL',b,f)]+[(a.GCMS[g],base[g][j],future[g][j]) for g in range(5)]:
                        key=(country,group,method,residual,cap,gcm)
                        sums=nation.setdefault(key,[0.]*5);sums[0]+=weight*bv;sums[1]+=weight*fv
                        sums[{'increase':2,'decrease':3,'unchanged':4}[a.sign(fv-bv)]]+=weight
                    yield dict(country=country,cell_id=cell,latitude=meta['latitude'],longitude=meta['longitude'],
                        harvest_area_ha=weight,period=a.GROUPS[group][0],scenario=a.GROUPS[group][1],
                        method=method,buffer_days=residual,cap=cap,baseline=b,future=f,delta=f-b,
                        relative_change_pct=a.percent(b,f),direction=a.sign(f-b))
    a.csv_write(folder/'point_changes.csv.gz',points())
    results=a.summarize(nation,area,len(expected))
    a.csv_write(folder/'country_changes.csv',[r for r in results if r['gcm']=='ALL'])
    a.csv_write(folder/'country_changes_by_gcm.csv',[r for r in results if r['gcm']!='ALL'])
    a.save(folder/'complete.json',dict(status='completed',rows=rows,points=len(expected),onset_status=dict(stats),
        policy=POLICY,aggregation='equal years within GCM/planting; equal five GCMs and five plantings per point; fixed area across points',
        outputs={p.name:a.digest(p) for p in folder.iterdir() if p.suffix in ('.gz','.csv')}))
    print('COUNTRY COMPLETE',country,rows,flush=True)


def report(out):
    records=[]
    for country in COUNTRIES:
        folder=out/country.replace(' ','_'); done=a.load(folder/'complete.json')
        if done['status']!='completed' or a.digest(folder/'country_changes.csv')!=done['outputs']['country_changes.csv']:
            raise ValueError('Incomplete analysis')
        with (folder/'country_changes.csv').open(newline='',encoding='utf-8') as f:
            records.extend(csv.DictReader(f))
    a.csv_write(out/'country_changes_all.csv',records)
    lines=['Country | Period | Scenario | Policy | Buffer | Cap | Historical | Future | Delta | %']
    for r in records:
        pct=f"{float(r['relative_change_pct']):+.2f}" if r['relative_change_pct'] else 'NA'
        lines.append(' | '.join([r['country'],r['period'],r['scenario'],r['method'],r['buffer_days'],r['cap'],
            f"{float(r['baseline']):.4f}",f"{float(r['future']):.4f}",f"{float(r['delta']):+.4f}",pct]))
    text='\n'.join(lines)+'\n';(out/'report.txt').write_text(text,encoding='utf-8'); print(text)
    a.save(out/'complete.json',dict(status='completed',policy=POLICY,comparisons=len(records)))


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('prepare','worker','analyze','report'))
    p.add_argument('--addon',type=Path);p.add_argument('--source',type=Path);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--index',type=int);p.add_argument('--workers',type=int,default=160);p.add_argument('--country',choices=COUNTRIES)
    args=p.parse_args()
    if args.action=='prepare':prepare(args.source,args.out)
    elif args.action=='worker':worker(args.addon,args.out,args.index,args.workers)
    elif args.action=='analyze':analyze(args.addon,args.out,args.country)
    else:report(args.out)

if __name__=='__main__':main()
