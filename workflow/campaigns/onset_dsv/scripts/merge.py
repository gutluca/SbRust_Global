"""Publish every simulation once; no raw-yield selection in application counts."""
import csv
import gzip
import os
from pathlib import Path
import common as c
from control_index import Index
import world_onset_runtime as r
from worker import valid

def merge(state_path,key):
    c.compute_only(); state=c.load(state_path); rec=c.country(state,key); folder=c.result(state,rec)
    final=folder/('summary_applications_'+c.safe_name(rec['country'])+'.csv.gz')
    if (folder/'complete.json').is_file():
        info=c.load(folder/'complete.json'); c.verify(final,info['sha256']); return
    prepared=c.ready(state,rec); info=c.load(prepared/'complete.json')
    c.verify(prepared/'bundles.json',info['outputs']['bundles.json'])
    c.verify(prepared/'historical_reused.csv.gz',info['outputs']['historical_reused.csv.gz'])
    sources=[prepared/'historical_reused.csv.gz']
    for batch in c.load(prepared/'bundles.json'):
        for task in batch:
            dest=folder/'tasks'/task['name']
            if not valid(dest,task): raise ValueError('Missing or invalid annual partition')
            sources.append(dest/'schedules.csv.gz')
    current=Index(prepared/'index'); seen=bytearray(current.meta['records']); count=0
    tmp=final.with_name(final.name+'.partial.'+os.environ['SLURM_JOB_ID']); folder.mkdir(parents=True,exist_ok=True)
    with gzip.open(tmp,'wt',encoding='utf-8',newline='',compresslevel=1) as dst:
        writer=csv.DictWriter(dst,fieldnames=r.OUTPUT_FIELDS); writer.writeheader()
        for path in sources:
            with gzip.open(path,'rt',encoding='utf-8',newline='') as f:
                for row in csv.DictReader(f):
                    current.get(row)
                    year=int(row['simulation_year']); group=r.o.a.group_index(row['period'],row['scenario'],year)
                    j=current.cells[row['cell_id']]*r.o.e.N+r.o.e.slot(group,year,row['gcm'],row['planting_label'])
                    if seen[j]: raise ValueError('Duplicate final simulation')
                    seen[j]=1; writer.writerow(r.public_record(row)); count+=1
    if count!=len(seen) or 0 in seen: raise ValueError('Incomplete 4600-row/cell final applications matrix')
    tmp.rename(final)
    c.save(folder/'complete.json',dict(status='completed',country=rec['country'],points=len(current.cells),rows=count,
        master=str(final),sha256=c.sha(final),policy=r.POLICY,current_disease_master_sha256=rec['current_master_sha256'],
        historical_reused_rows=info['historical_reused_rows'],historical_rerun_rows=info['historical_rerun_rows'],
        future_recomputed_rows=info['future_rows']))
    print('COUNTRY_COMPLETE',rec['country'],count,flush=True)
