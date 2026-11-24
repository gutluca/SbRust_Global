"""Healthy daily crop reconstruction, realized CO2 audit and unchanged DSV replay."""
import csv
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import traceback
import common as c
from control_index import Index
from co2_runtime import stage_co2, leaf_directories, read_daily_weather
import world_onset_runtime as r

def valid(destination,task,full=True):
    if not (destination/'complete.json').is_file(): return False
    receipt=c.load(destination/'complete.json')
    if receipt.get('status')!='ok' or receipt['task_signature']!=task['signature'] or receipt['policy']!=r.POLICY:
        raise ValueError('Existing task receipt does not match frozen task')
    required={'schedules.csv.gz','daily_cache.jsonl.gz','co2_audit.json','sample_daily_outputs.tar.gz'}
    if (set(receipt['outputs'])!=required or receipt['points']!=len(task['cells']) or
        receipt['rows']!=5*len(task['cells'])):
        raise ValueError('Incomplete published task or crop population')
    co2=c.load(destination/'co2_audit.json')
    if co2.get('status')!='ok' or co2['checked_experiments']!=receipt['rows'] or co2['checked_daily_values']<=0:
        raise ValueError('Published task lacks complete realized-CO2 checks')
    if full:
        for name,expected in receipt['outputs'].items(): c.verify(destination/name,expected)
    return True

def execute(state,rec,task,pilot=False):
    folder=c.ready(state,rec); destination=c.result(state,rec)/('pilots' if pilot else 'tasks')/task['name']
    if valid(destination,task): return
    if destination.exists(): raise ValueError('Incomplete published task directory; inspect before changing it')
    unsigned=dict(task); unsigned.pop('signature')
    if c.signature(unsigned)!=task['signature']: raise ValueError('Task definition changed')
    c.verify(task['config'],task['config_sha256'])
    c.verify(folder/'index/metadata.json',task['index_metadata_sha256'])
    for name,expected in task['inputs'].items(): c.verify(name,expected)
    c.verify(state['executable'],c.EXE_SHA)
    cfg=c.load(task['config']); index=Index(folder/'index'); controls=index.controls(cfg,task['cells'])
    scratch=os.environ.get('SLURM_TMPDIR') or os.environ.get('TMPDIR')
    if not scratch or not Path(scratch).is_dir(): raise RuntimeError('Node-local scratch unavailable')
    engine=Path(state['engine'])/'scripts'; sys.path.insert(0,str(engine))
    from beruski_worker_support import partition_sites
    from collate_summary import coordinates,site_attributes,summary_rows
    from export_analysis_table import dssat_date
    with tempfile.TemporaryDirectory(prefix='apps-co2-',dir=scratch) as raw:
        root=Path(raw); work=root/'work'; output=root/'result'; output.mkdir()
        def call(*args): subprocess.run([sys.executable,*map(str,args)],cwd=root,check=True)
        try:
            partition,selected=partition_sites(cfg,root,set(task['cells']))
            partition['cores']=partition['threads']=int(os.environ.get('SLURM_CPUS_PER_TASK','4'))
            c.save(root/'partition.json',partition)
            call(engine/'prepare_scratch_config.py',root/'partition.json',root/'execution.json','--work-dir',work)
            actual=c.load(root/'execution.json')
            if any(run.get('dises')!='Y' for run in actual['runs']): raise ValueError('Auxiliary run is not DISES=Y')
            call('-m','pythia',root/'execution.json','--setup','--quiet')
            staged=stage_co2(actual,work)
            call(engine/'materialize_weather_window.py',root/'execution.json',work,root/'weather','--audit',root/'weather_audit.json')
            call('-m','pythia',root/'execution.json','--run-dssat','--quiet')
            errors=list(work.rglob('ERROR.OUT'))
            if errors: raise ValueError('DSSAT wrote ERROR.OUT: '+str(errors[0]))
            leaves=leaf_directories(actual,work)
            if staged!=len(controls) or len(leaves)!=len(controls): raise ValueError('Incomplete crop-output population')
            attrs=site_attributes([Path(v['sample']) for v in selected]); replay=r.Replay(); rows=[]; caches=[]; checks=[]; seen=set()
            sample=[]
            for run,leaf in leaves:
                daily=leaf/'Weather.OUT'; dismo=leaf/'DISMO.OUT'; summary=leaf/'Summary.OUT'
                checks.append(dict(run=run['name'],directory=str(leaf.relative_to(work)),
                    **read_daily_weather(daily,actual['pipeline']['co2_forcing'].get('annual_ppm',actual['pipeline']['co2_forcing']))))
                lat,lon=coordinates(leaf); prop=attrs[round(lat,4),round(lon,4)]
                key=prop['CELL_ID'],run['planting_label']
                if key in seen: raise ValueError('Duplicate healthy output')
                seen.add(key); reference=controls[key]; raw_rows=summary_rows(summary)
                if len(raw_rows)!=1: raise ValueError('Expected one annual crop cycle')
                raw=raw_rows[0]
                if (float(raw['HWAH'])!=reference['YIELD_NO_DISEASE_KG_HA'] or dssat_date(raw['PDAT'])!=reference['PLANT_DATE']
                    or prop['GM_WIN']!=reference['MATURITY_GROUP'] or prop['WEATHER_SOURCE_CELL_ID']!=reference['WEATHER_SOURCE_CELL_ID']):
                    raise ValueError('Healthy auxiliary crop differs from CURRENT control: '+reference['SIMULATION_ID'])
                weather=sorted(leaf.glob('*.WTH'))
                if len(weather)>1: raise ValueError('Ambiguous annual WTH')
                source_weather=weather[0] if weather else daily
                recovered=replay(source_weather,dismo); cache=recovered['daily_cache']
                row=dict(simulation_id=reference['SIMULATION_ID'],country=reference['COUNTRY'],cell_id=key[0],
                    period=reference['PERIOD'],simulation_year=reference['SIM_YEAR'],scenario=reference['SCENARIO'],gcm=reference['GCM'],
                    planting_label=key[1],plant_date=reference['PLANT_DATE'],maturity_date=raw['MDAT'],
                    weather_source_cell_id=prop['WEATHER_SOURCE_CELL_ID'],maturity_group=prop['GM_WIN'],
                    harvest_area_ha=reference['HARVEST_AREA_HA'],latitude=lat,longitude=lon,raw_yield_kg_ha=raw['HWAH'])
                rows.append(r.decorate(row,cache,index)); caches.append(dict(simulation_id=row['simulation_id'],**cache))
                if not sample:
                    sample=[(run['name'],p) for p in {source_weather,daily,dismo,summary}]
            if seen!=set(controls): raise ValueError('Incomplete current control population')
            with gzip.open(output/'schedules.csv.gz','wt',encoding='utf-8',newline='',compresslevel=1) as f:
                writer=csv.DictWriter(f,fieldnames=(*r.OUTPUT_FIELDS,'raw_yield_kg_ha')); writer.writeheader()
                for row in rows: writer.writerow(dict(r.public_record(row),raw_yield_kg_ha=row['raw_yield_kg_ha']))
            with gzip.open(output/'daily_cache.jsonl.gz','wt',encoding='utf-8',compresslevel=1) as f:
                for cache in caches: f.write(json.dumps(cache,separators=(',',':'))+'\n')
            with tarfile.open(output/'sample_daily_outputs.tar.gz','w:gz',dereference=True) as archive:
                for run,path in sample: archive.add(path,arcname=run+'/'+path.name,recursive=False)
            c.save(output/'co2_audit.json',dict(status='ok',forcing=actual['pipeline']['co2_forcing'],
                checked_experiments=len(checks),checked_daily_values=sum(v['days'] for v in checks),experiments=checks))
            c.save(output/'complete.json',dict(status='ok',rows=len(rows),points=len(task['cells']),task_signature=task['signature'],
                raw_control_yield_plant_gm_weather_parity='exact, every output',policy=r.POLICY,source_current_master=rec['current_master_sha256'],
                outputs={name:c.sha(output/name) for name in ('schedules.csv.gz','daily_cache.jsonl.gz','co2_audit.json','sample_daily_outputs.tar.gz')}))
            destination.parent.mkdir(parents=True,exist_ok=True)
            staging=destination.with_name(destination.name+'.partial.'+os.environ['SLURM_JOB_ID'])
            if staging.exists(): raise FileExistsError(staging)
            shutil.copytree(output,staging); staging.rename(destination)
            print('COMPLETE',task['name'],'rows',len(rows),'CO2 days',sum(v['days'] for v in checks),flush=True)
        except BaseException:
            failure=c.result(state,rec)/'failures'/(task['name']+'.'+os.environ['SLURM_JOB_ID'])
            failure.mkdir(parents=True,exist_ok=True); (failure/'exception.txt').write_text(traceback.format_exc())
            with tarfile.open(failure/'diagnostics.tar.gz','w:gz') as archive:
                files=[p for p in root.rglob('*') if p.is_file() and not p.is_symlink() and
                       (p.name in ('ERROR.OUT','WARNING.OUT','Summary.OUT','DISMO.OUT') or p.suffix in ('.json','.log'))]
                for path in files[:60]: archive.add(path,arcname=str(path.relative_to(root)),recursive=False)
            raise

def bundle(state_path,key,index,pilot=False):
    c.compute_only(); state=c.load(state_path); rec=c.country(state,key); folder=c.ready(state,rec)
    info=c.load(folder/'complete.json'); name='pilots.json' if pilot else 'bundles.json'
    c.verify(folder/name,info['outputs'][name]); tasks=c.load(folder/name)[index]
    for task in tasks: execute(state,rec,task,pilot)
