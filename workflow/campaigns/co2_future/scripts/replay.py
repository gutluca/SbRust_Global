"""Compare a real original-CO2 rerun with the immutable published master."""
import csv
import gzip
import math
from pathlib import Path
import subprocess
import sys
import common as c
from co2_runtime import leaf_directories


def rendered_inputs(config,work):
    result={}
    for run,leaf in leaf_directories(config,work):
        key=str(leaf.relative_to(work))
        files=[leaf/'SBG00001.SBX',leaf/'WTH0.WTH']
        files.extend(leaf/Path(p).name for p in config['default_setup']['include'])
        files.extend(leaf.glob('*.SOL'))
        if any(not p.is_file() for p in files): raise ValueError('Missing rendered replay input')
        result[key]={p.name:c.sha(p) for p in sorted(set(files)) if p.name!='CO2048.WDA'}
    return result


def compare_reference(reference, actual, config):
    meta=config['pipeline']
    def read(path,selected):
        result={}
        with gzip.open(path,'rt',newline='',encoding='utf-8') as stream:
            for row in csv.DictReader(stream,delimiter=';'):
                if selected and (int(row['SIM_YEAR'])!=int(meta['simulation_year']) or
                                  row['SCENARIO']!=meta['scenario'] or row['GCM']!=meta['gcm']):
                    continue
                key=row['CELL_ID'],row['PLANTING_LABEL']
                if key in result: raise ValueError('Duplicate numerical replay identity')
                result[key]=row
        return result
    expected,observed=read(reference,True),read(actual,False)
    if set(expected)!=set(observed) or len(expected)!=meta['co2_rerun']['expected_points']*5:
        raise ValueError('Replay population or paired matrix differs from published reference')
    max_errors={}
    numeric={'LATITUDE':1e-8,'LONGITUDE':1e-8,'HARVEST_AREA_HA':1e-6,'WEATHER_DISTANCE_KM':1e-6,
             'YIELD_WITH_DISEASE_KG_HA':0.,'YIELD_NO_DISEASE_KG_HA':0.,'LOSS_KG_HA':0.,'LOSS_PCT':1e-10,
             'TOTAL_SPRAYS':0.,'DISEASE_ONSET_DAE':0.}
    for key in expected:
        before,after=expected[key],observed[key]
        if set(before)!=set(after): raise ValueError('Replay master schema differs')
        for field in before:
            if field in numeric and before[field]!='' and after[field]!='':
                a,b=float(before[field]),float(after[field])
                error=abs(a-b)
                if not (math.isfinite(a) and math.isfinite(b)) or error>numeric[field]:
                    raise ValueError(f'Numerical replay mismatch {key} {field}: published={before[field]} replay={after[field]}')
                max_errors[field]=max(max_errors.get(field,0.),error)
            elif before[field]!=after[field]:
                raise ValueError(f'Replay identity/date/value mismatch {key} {field}: {before[field]!r} != {after[field]!r}')
    return {'status':'ok','matched_pairs':len(expected),'published_master_sha256':c.sha(reference),
            'replayed_master_sha256':c.sha(actual),'maximum_absolute_errors':max_errors,
            'yield_tolerance_kg_ha':0.,'policy':'same printed DSSAT yields, dates, onset, sprays, identities and loss equations'}


def pilot_master(state,country,config,shard,scratch):
    engine=Path(state['engine'])/'scripts'
    paired=Path(scratch)/'pilot_pairs';paired.mkdir()
    stem=c.safe_name(country['country'])
    def call(script,*args):
        subprocess.run([sys.executable,str(script),*map(str,args)],check=True)
    for kind in ('disease','control'):
        call(engine/'export_analysis_table.py',Path(shard)/f'summary_{kind}_{stem}.csv.gz',
             paired/(kind+'.csv.gz'),'--kind',kind,'--temporary-root',scratch,
             '--audit',Path(scratch)/(kind+'_export_audit.json'))
    finalize=Path(state['pack'])/'scripts/native_finalize.py'
    call(finalize,engine,'validate',paired/'disease.csv.gz',paired/'control.csv.gz',Path(scratch)/'pair_validation.json')
    actual=Path(shard)/'pilot_master.csv.gz'
    call(finalize,engine,'master',paired/'disease.csv.gz',paired/'control.csv.gz',actual,
         Path(state['output'])/country['key']/'project.json',Path(shard)/'pilot_master_audit.json')
    if config['pipeline']['co2_rerun'].get('reference_replay'):
        reference=Path(config['pipeline']['co2_rerun']['expected_master'])
        lock=c.load(Path(state['output'])/'pilot_lock.json')['sites_sha256']
        if c.sha(reference)!=lock[str(reference)]:
            raise ValueError('Published reference sample changed')
        audit=compare_reference(reference,actual,config)
        audit['full_source_master_sha256']=country['source_master_sha256']
        c.save(Path(shard)/'replay_audit.json',audit)
