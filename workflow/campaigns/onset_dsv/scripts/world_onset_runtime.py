"""World onset-DSV: same healthy daily mask and rain algorithm as approved replay."""
from datetime import date, timedelta
import gzip
import json
from pathlib import Path
import onset_dsv as o
from replay_dsv50_dssat_outputs import read_weather, read_crop
from beruski_worker_support import partition_sites

POLICY=dict(o.POLICY, residual_days=[14], methods=['DSV50','DSV80'], ensemble='arithmetic mean',
            crop='same approved binary; DISES=Y; YMAX=0; no GM calibration')

ORIGINALS={}
def get_original(folder):
    if folder not in ORIGINALS:ORIGINALS[folder]=o.Original(Path(folder))
    return ORIGINALS[folder]

def partition_controls(controls, task):
    wanted=set(get_original(task['onset_folder']).meta['cells'])
    cells=sorted(wanted,key=int);n=task['shards'];i=task['shard']
    selected=set(cells[len(cells)*i//n:len(cells)*(i+1)//n])
    result={k:v for k,v in controls.items() if k[0] in selected}
    if not selected or set(result)!={(c,p) for c in selected for p in o.a.PLANTS}:
        raise ValueError('Incomplete retained cell/planting matrix')
    return result,selected

class Replay:
    def __init__(self): self.cache={}
    def __call__(self,weather_path,crop_path):
        key=str(weather_path.resolve())
        if key not in self.cache:self.cache[key]=read_weather(weather_path)
        daily=self.cache[key];crop,old=read_crop(crop_path)
        index={d:i for i,(d,r) in enumerate(daily)}
        if len(index)!=len(daily) or set(crop)-set(index):raise ValueError('Daily weather/crop mismatch')
        active=[d for d,flag in crop.items() if flag]
        if active and index[min(active)]<32:raise ValueError('Missing antecedent rain')
        lo=max(0,index[min(crop)]-32);hi=index[max(crop)]+1
        daily=daily[lo:hi];mask=[int(crop.get(d,False)) for d,r in daily]
        cache=dict(start=daily[0][0].isoformat(),rain=[r for d,r in daily],eligible=mask)
        # Legacy fields satisfy the proven crop executor; replaced by onset counts below.
        return dict(existing_dismo_sprays=old,eligible_crop_days=sum(mask),daily_cache=cache,
                    dsv50_sprays_capped_at_6=0,dsv50_sprays_without_cap=0,
                    recommendations_censored_by_cap=0,spray_dates=[])

def decorate(row,cache,original):
    dae,onset=original.get(row);start=date.fromisoformat(cache['start'])
    first=None if onset is None else (onset-start).days
    events=o.schedule(o.dsv_values(cache['rain']),cache['eligible'],first)
    row.update(disease_onset_dae='' if dae==-1 else dae,onset_date_approx='' if onset is None else onset.isoformat(),
        emergence_lag_assumed_days=5,buffer_days=14,eligible_days_original=sum(cache['eligible']),
        eligible_days_after_onset=0 if first is None else sum(v and i>=first for i,v in enumerate(cache['eligible'])))
    for rule in ('DSV50','DSV80'):
        days=events[rule,14];prefix='onset_'+rule.lower()+'_r14'
        row[prefix+'_uncapped']=len(days);row[prefix+'_cap6']=min(6,len(days))
        row[prefix+'_dates']=';'.join((start+timedelta(days=i)).isoformat() for i in days)
    for cap in ('uncapped','cap6'):
        row['onset_ensemble2_r14_'+cap]=(row['onset_dsv50_r14_'+cap]+row['onset_dsv80_r14_'+cap])/2
    row['dsv50_sprays_without_cap']=row['onset_dsv50_r14_uncapped']
    row['dsv50_sprays_capped_at_6']=row['onset_dsv50_r14_cap6']
    row['recommendations_censored_by_cap']=max(0,row['onset_dsv50_r14_uncapped']-6)
    row['spray_dates']=';'.join(row['onset_dsv50_r14_dates'].split(';')[:6])
    return row

def output_extras(staging,rows,caches,task):
    path=staging/'daily_cache.jsonl.gz'
    with gzip.open(path,'wt',encoding='utf-8',compresslevel=1) as f:
        for row,cache in zip(rows,caches):
            f.write(json.dumps(dict(simulation_id=row['simulation_id'],**cache),separators=(',',':'))+'\n')
    o.a.save(staging/'multi_audit.json',dict(status='ok',rows=len(rows),policy=POLICY,
        task_sha256=task['task_sha256'],daily_cache_sha256=o.a.digest(path),
        onset_sha256=task['onset_sha256']))

IDENTIFIERS=('simulation_id','country','cell_id','period','simulation_year','scenario','gcm','planting_label',
    'plant_date','maturity_date','weather_source_cell_id','maturity_group','harvest_area_ha','latitude','longitude',
    'disease_onset_dae','onset_date_approx','emergence_lag_assumed_days','eligible_days_original','eligible_days_after_onset')
COUNTS=tuple('onset_'+rule+'_r14_'+cap for rule in ('dsv50','dsv80','ensemble2') for cap in ('uncapped','cap6'))
OUTPUT_FIELDS=IDENTIFIERS+('buffer_days',)+COUNTS+('onset_dsv50_r14_dates','onset_dsv80_r14_dates')

def public_record(row):
    result={k:row[k] for k in IDENTIFIERS}
    result['buffer_days']=14
    for rule in ('dsv50','dsv80'):
        raw=int(row['onset_'+rule+'_r14_uncapped']);cap=int(row['onset_'+rule+'_r14_cap6'])
        if raw<0 or cap!=min(6,raw):raise ValueError('Invalid count/cap')
        result['onset_'+rule+'_r14_uncapped']=raw;result['onset_'+rule+'_r14_cap6']=cap
        result['onset_'+rule+'_r14_dates']=row['onset_'+rule+'_r14_dates']
    for cap in ('uncapped','cap6'):
        result['onset_ensemble2_r14_'+cap]=(result['onset_dsv50_r14_'+cap]+result['onset_dsv80_r14_'+cap])/2
    return result
