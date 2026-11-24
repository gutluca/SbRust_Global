"""Exact-key join of archived global DVIP counts and completed DSV counts.

No model execution, fitted weights, rounding, or changes to either source.
Binary export is little-endian <HII: sprays, planting ordinal, maturity ordinal.
Index order is explicit in metadata: cell, group, year, GCM, planting.
"""
import argparse
from array import array
import csv
from datetime import date
import gzip
import json
import math
from pathlib import Path
import struct

import analyze_beruski_trends as a

RECORD=struct.Struct('<HII')
METHODS=('DVIP_ADAPTED_GLOBAL','DSV50','DSV80','ENSEMBLE3')
OFFSETS=[]
N=0
for _,_,lo,hi in a.GROUPS:
    OFFSETS.append(N);N+=(hi-lo+1)*25
assert N==4600

def slot(group,year,gcm,plant):
    lo,hi=a.GROUPS[group][2:]
    if not lo<=year<=hi:raise ValueError('Year outside group')
    return OFFSETS[group]+(year-lo)*25+a.GCMS.index(gcm)*5+a.PLANTS.index(plant)

def identity(country,cell,group,year,gcm,plant):
    lo,hi=a.GROUPS[group][2:]
    return f'{country}|{cell}|{lo}-{hi}|{year}|{a.GROUPS[group][1]}|{gcm}|{plant}'

def count(value):
    v=float(value)
    if not math.isfinite(v) or v<0 or not v.is_integer() or v>=65535:
        raise ValueError(f'Invalid integer count: {value}')
    return int(v)

def metrics(old,row):
    vals=[min(6,old),old]*2
    for rule in ('dsv50','dsv80'):
        for residual in (14,16):
            raw=count(row[f'{rule}_r{residual}_uncapped'])
            capped=count(row[f'{rule}_r{residual}_cap6'])
            if capped!=min(6,raw):raise ValueError('Invalid cap')
            vals.extend((capped,raw))
    return vals+[math.fsum(vals[k*4+j] for k in range(3))/3 for j in range(4)]

def export(master,expected_sha,country,pop,out):
    if out.exists():raise FileExistsError(out)
    if a.digest(master)!=expected_sha:raise ValueError('Archived global SHA mismatch')
    cells=sorted(pop['retained'][country],key=int);index={c:i for i,c in enumerate(cells)}
    sprays=array('H',[65535])*(len(cells)*N)
    planting=array('I',[0])*len(sprays);maturity=array('I',[0])*len(sprays)
    kept=rawrows=0
    with gzip.open(master,'rt',newline='',encoding='utf-8') as f:
        reader=csv.reader(f,delimiter=';');head=next(reader);ix={c:i for i,c in enumerate(head)}
        for row in reader:
            rawrows+=1
            get=lambda k:row[ix[k]]
            if get('COUNTRY')!=country:raise ValueError('Country mismatch')
            cell=str(int(get('CELL_ID')))
            if cell not in index:
                if cell not in pop['excluded'][country]:raise ValueError('Unknown point')
                continue
            year=int(get('SIM_YEAR'));group=a.group_index(get('PERIOD'),get('SCENARIO'),year)
            gcm,plant=get('GCM'),get('PLANTING_LABEL')
            if get('SIMULATION_ID')!=identity(country,cell,group,year,gcm,plant):raise ValueError('Unexpected global ID')
            i=index[cell]*N+slot(group,year,gcm,plant)
            if sprays[i]!=65535:raise ValueError('Duplicate global simulation')
            meta=pop['retained'][country][cell]
            for field,tol in [('HARVEST_AREA_HA',1e-5),('LATITUDE',1e-4),('LONGITUDE',1e-4)]:
                if not math.isclose(float(get(field)),float(meta[field.lower()]),rel_tol=1e-8,abs_tol=tol):
                    raise ValueError('Global population mismatch')
            sprays[i]=count(get('TOTAL_SPRAYS'))
            planting[i]=date.fromisoformat(get('PLANT_DATE')).toordinal()
            maturity[i]=date.fromisoformat(get('MATURITY_DATE')).toordinal() if get('MATURITY_DATE') else 0
            kept+=1
            if kept%1000000==0:print('EXPORTED',country,kept,flush=True)
    if kept!=len(sprays) or 65535 in sprays:raise ValueError('Incomplete global matrix')
    out.mkdir(parents=True)
    packed=out/'global_counts.bin.gz'
    with gzip.open(packed,'wb',compresslevel=6) as f:
        block=bytearray()
        for values in zip(sprays,planting,maturity):
            block.extend(RECORD.pack(*values))
            if len(block)>=1048576:f.write(block);block.clear()
        if block:f.write(block)
    a.save(out/'global_counts.json',dict(country=country,cells=cells,groups=a.GROUPS,gcms=a.GCMS,
        plants=a.PLANTS,record_format='<HII',rows=kept,source_rows=rawrows,source=str(master),
        source_sha256=expected_sha,packed_sha256=a.digest(packed),population_sha256=None,
        source_field='TOTAL_SPRAYS',source_window='original global disease-enabled crop',
        adapted_rule='archived global count unchanged; no 14/16-day recalculation'))
    print('GLOBAL EXPORT COMPLETE',country,kept,packed.stat().st_size,flush=True)

def read_global(folder,country,pop):
    meta=a.load(folder/'global_counts.json');path=folder/'global_counts.bin.gz'
    if meta['country']!=country or meta['record_format']!='<HII':raise ValueError('Global metadata mismatch')
    if meta['groups']!=[list(g) for g in a.GROUPS] or meta['gcms']!=list(a.GCMS) or meta['plants']!=list(a.PLANTS):
        raise ValueError('Global index layout changed')
    cells=sorted(pop['retained'][country],key=int)
    if meta['cells']!=cells or meta['rows']!=len(cells)*N:raise ValueError('Global coverage differs')
    if a.digest(path)!=meta['packed_sha256']:raise ValueError('Global packed SHA mismatch')
    with gzip.open(path,'rb') as f:data=f.read()
    if len(data)!=len(cells)*N*RECORD.size:raise ValueError('Global data size differs')
    return meta,{c:i for i,c in enumerate(cells)},data

def analyze(source,global_folder,out,country,pop):
    from collections import defaultdict
    finished=a.load(source/'complete.json')
    if finished.get('status')!='completed':raise ValueError('DSV campaign incomplete')
    info=finished['countries'][country];stem=country.replace(' ','_')
    master=source/f'summary_beruski_{stem}.csv.gz'
    if a.digest(master)!=info['sha256']:raise ValueError('DSV master SHA mismatch')
    oldmeta,cells,data=read_global(global_folder,country,pop)
    if out.exists():raise FileExistsError(out)
    out.mkdir(parents=True)
    seen=bytearray(len(cells)*N);acc={};rawrows=kept=0;changed_maturity=0
    expected=pop['retained'][country]
    columns=[f'{m.lower()}_dsv_r{r}_{cap}' for m in METHODS for r in (14,16) for cap in ('cap6','uncapped')]
    joined=out/'simulation_ensemble3.csv.gz'
    with gzip.open(master,'rt',newline='',encoding='utf-8') as f,gzip.open(joined,'wt',newline='',encoding='utf-8',compresslevel=1) as dst:
        reader=csv.reader(f);header=next(reader);ix={v:i for i,v in enumerate(header)}
        required=['country','cell_id','period','scenario','simulation_year','gcm','planting_label','plant_date',
                  'simulation_id','maturity_date','latitude','longitude','harvest_area_ha']
        required += [f'dsv{x}_r{r}_{cap}' for x in (50,80) for r in (14,16) for cap in ('cap6','uncapped')]
        if len(ix)!=len(header) or not set(required)<=ix.keys():raise ValueError('Missing/duplicate DSV columns')
        writer=csv.writer(dst)
        writer.writerow(['simulation_id','country','cell_id','period','scenario','simulation_year','gcm',
            'planting_label','plant_date','global_maturity_date','dsv_maturity_date','harvest_area_ha']+columns)
        for row in reader:
            rawrows+=1
            if len(row)!=len(header):raise ValueError('Malformed DSV row')
            get=lambda k:row[ix[k]]
            if get('country')!=country:raise ValueError('DSV country mismatch')
            cell=str(int(get('cell_id')))
            if cell not in cells:
                if cell not in pop['excluded'][country]:raise ValueError('Unexpected DSV point')
                continue
            year=int(get('simulation_year'));group=a.group_index(get('period'),get('scenario'),year)
            gcm,plant=get('gcm'),get('planting_label')
            sid=identity(country,cell,group,year,gcm,plant)
            if get('simulation_id')!=sid:raise ValueError(f'DSV identity mismatch {sid}')
            i=cells[cell]*N+slot(group,year,gcm,plant)
            if seen[i]:raise ValueError(f'Duplicate DSV simulation {sid}')
            old,plantday,matday=RECORD.unpack_from(data,i*RECORD.size)
            if old==65535:raise ValueError('Missing global count')
            if date.fromisoformat(get('plant_date')).toordinal()!=plantday:raise ValueError(f'Plant date mismatch {sid}')
            for field,tol in [('harvest_area_ha',1e-5),('latitude',1e-4),('longitude',1e-4)]:
                if not math.isclose(float(get(field)),float(expected[cell][field]),rel_tol=1e-8,abs_tol=tol):
                    raise ValueError(f'Population metadata differs {sid}/{field}')
            vals=metrics(old,{k:row[ix[k]] for k in required[-8:]})
            global_mat=date.fromordinal(matday).isoformat() if matday else ''
            changed_maturity+=global_mat!=get('maturity_date')
            writer.writerow([sid,country,cell,a.GROUPS[group][0],get('scenario'),year,gcm,plant,
                get('plant_date'),global_mat,get('maturity_date'),get('harvest_area_ha')]+vals)
            a.add_member(acc,(cell,group,a.GCMS.index(gcm),a.PLANTS.index(plant)),year,vals)
            seen[i]=1;kept+=1
            if kept%1000000==0:print('JOINED',country,kept,flush=True)
    if rawrows!=info['rows'] or kept!=len(seen) or 0 in seen:raise ValueError('Incomplete one-to-one join')
    summarize(out,country,pop,acc)
    a.save(out/'analysis_complete.json',dict(status='completed',country=country,rows=kept,
        retained_points=len(cells),global_source=oldmeta,dsv_source=str(master),dsv_sha256=info['sha256'],
        dsv_policy=finished.get('policy'),population_provenance=pop['provenance'],
        matched_keys='country, cell, period, scenario, year, GCM, planting; exact plant date',
        maturity_dates_differ=changed_maturity,
        interpretation='Hybrid descriptive ensemble of three archived counters, not a new control schedule; original DVIP crop window retained',
        weights=[1/3]*3,dvip_buffer='original global settings unchanged',dsv_buffers=[14,16],
        cap='min(6,count) per component before averaging',script_sha256=a.digest(__file__),
        outputs={p.name:a.digest(p) for p in out.iterdir() if p.suffix in ('.gz','.csv')}))
    print('ENSEMBLE3 COMPLETE',country,kept,flush=True)

def summarize(out,country,pop,acc):
    from collections import defaultdict
    expected=pop['retained'][country];nation=defaultdict(lambda:[0.]*5)
    area=math.fsum(float(v['harvest_area_ha']) for v in expected.values())
    configurations=[(m,r,c) for m in METHODS for r in (14,16) for c in ('cap6','uncapped')]
    def points():
        for cell in sorted(expected,key=int):
            meta=expected[cell];w=float(meta['harvest_area_ha']);bg=a.means(acc,cell,0)
            for group in range(1,7):
                fg=a.means(acc,cell,group)
                for j,(method,buffer,cap) in enumerate(configurations):
                    b=math.fsum(v[j] for v in bg)/5;f=math.fsum(v[j] for v in fg)/5
                    gd=[fg[g][j]-bg[g][j] for g in range(5)]
                    for gcm,bv,fv in [('ALL',b,f)]+[(a.GCMS[g],bg[g][j],fg[g][j]) for g in range(5)]:
                        stats=nation[(country,group,method,buffer,cap,gcm)]
                        stats[0]+=w*bv;stats[1]+=w*fv
                        stats[{'increase':2,'decrease':3,'unchanged':4}[a.sign(fv-bv)]]+=w
                    yield dict(country=country,cell_id=cell,**meta,period=a.GROUPS[group][0],scenario=a.GROUPS[group][1],
                        method=method,buffer_days=buffer,cap=cap,baseline=b,future=f,delta=f-b,
                        relative_change_pct=a.percent(b,f),direction=a.sign(f-b),paired_members=25,
                        gcm_increase=sum(a.sign(x)=='increase' for x in gd),gcm_decrease=sum(a.sign(x)=='decrease' for x in gd))
    a.csv_write(out/'point_changes.csv.gz',points())
    rows=a.summarize(nation,area,len(expected));national=[r for r in rows if r['gcm']=='ALL']
    by_gcm=[r for r in rows if r['gcm']!='ALL']
    for r in national:
        peers=[v for v in by_gcm if all(v[k]==r[k] for k in ('country','period','scenario','method','buffer_days','cap'))]
        for name in ('increase','decrease','unchanged'):r['gcms_'+name]=sum(v['direction']==name for v in peers)
    a.csv_write(out/'country_changes.csv',national);a.csv_write(out/'country_changes_by_gcm.csv',by_gcm)

def report(out):
    a.report(out)
    path=out/'report.txt'
    body=path.read_text(encoding='utf-8').replace('Rule | Buffer |','Rule | DSV Buffer |')
    note='Buffer: applies to DSV50/80 only; DVIP retains original global configuration and crop window.\n'
    path.write_text(note+body,encoding='utf-8')
    audit=a.load(out/'analysis_complete.json')
    audit.update(method='equal-weight ENSEMBLE3; DVIP_ADAPTED_GLOBAL + DSV50 + DSV80',
                 report_sha256=a.digest(path),buffer_scope='DSV only; original DVIP unchanged')
    a.save(out/'analysis_complete.json',audit)
    print(note,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path);p.add_argument('--global-folder',type=Path)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--country');p.add_argument('--population',type=Path)
    p.add_argument('--report',action='store_true');args=p.parse_args()
    if args.report:report(args.out)
    else:analyze(args.source,args.global_folder,args.out,args.country,a.load(args.population))
