"""Streaming, complete-matrix Beruski contrasts; no model execution or source edits."""
import argparse
from array import array
from collections import defaultdict
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path

RULES=('DVIP6','DVIP9','DSV50','DSV80')
GCMS=('GFDL-ESM4','IPSL-CM6A-LR','MPI-ESM1-2-HR','MRI-ESM2-0','UKESM1-0-LL')
PLANTS=tuple(f'P{i:02d}' for i in range(1,6))
GROUPS=[('baseline','historical',1981,2014)]+[(p,s,a,b) for p,a,b in
    [('future_near',2020,2039),('future_mid',2040,2069)] for s in ('ssp126','ssp370','ssp585')]
METRICS=[(rule,r,cap) for rule in RULES for r in (14,16) for cap in ('cap6','uncapped')]
COLUMNS=[f'{rule.lower()}_r{r}_{cap}' for rule,r,cap in METRICS]
METHODS=RULES+('ENSEMBLE4',)


def load(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1048576),b''):h.update(block)
    return h.hexdigest()
def save(path,value): Path(path).write_text(json.dumps(value,indent=2,allow_nan=False)+'\n',encoding='utf-8')
def sign(x):return 'increase' if x>1e-12 else 'decrease' if x< -1e-12 else 'unchanged'
def percent(b,f):return 100*(f-b)/b if b>0 else ''
def csv_write(path,rows):
    rows=iter(rows);first=next(rows)
    opener=gzip.open if str(path).endswith('.gz') else open
    with opener(path,'wt',newline='',encoding='utf-8') as stream:
        w=csv.DictWriter(stream,fieldnames=list(first));w.writeheader();w.writerow(first);w.writerows(rows)


def group_index(period,scenario,year):
    for i,(p,s,a,b) in enumerate(GROUPS):
        if period in (p,f'{a}-{b}') and scenario==s and a<=year<=b:return i
    raise ValueError(f'Unexpected period/scenario/year: {period}/{scenario}/{year}')


def add_member(acc,key,year,values):
    a,b=GROUPS[key[1]][2:];bit=1<<(year-a)
    if not a<=year<=b:raise ValueError('Year outside window')
    entry=acc.setdefault(key,[0,array('d',[0]*16)])
    if entry[0]&bit:raise ValueError(f'Duplicate point/year/GCM/planting: {key}/{year}')
    entry[0]|=bit
    for j,v in enumerate(values):entry[1][j]+=v


def means(acc,cell,group):
    years=GROUPS[group][3]-GROUPS[group][2]+1
    result=[]
    for g in range(5):
        members=[]
        for p in range(5):
            entry=acc.get((cell,group,g,p))
            if entry is None or entry[0]!=(1<<years)-1:
                raise ValueError(f'Incomplete years/member: cell={cell}, group={GROUPS[group]}, GCM={GCMS[g]}, planting={PLANTS[p]}')
            members.append(entry[1])
        result.append([math.fsum(v[j] for v in members)/(5*years) for j in range(16)])
    return result


def expand(values):
    # Caps were already applied to each simulation and rule. Never cap an ensemble mean.
    return list(values)+[math.fsum(values[k*4+j] for k in range(4))/4 for j in range(4)]


def summarize(nation,area,npoints):
    result=[]
    for (country,group,method,buffer,cap,gcm),v in sorted(nation.items()):
        b,f=v[0]/area,v[1]/area
        result.append(dict(country=country,period=GROUPS[group][0],scenario=GROUPS[group][1],
            method=method,buffer_days=buffer,cap=cap,gcm=gcm,points=npoints,area_ha=area,
            baseline=b,future=f,delta=f-b,relative_change_pct=percent(b,f),direction=sign(f-b),
            area_increase_pct=100*v[2]/area,area_decrease_pct=100*v[3]/area,
            area_unchanged_pct=100*v[4]/area))
    return result


def analyze(source,out,country,population):
    finished=load(source/'complete.json')
    if finished.get('status')!='completed':raise ValueError('Campaign is not fully consolidated')
    info=finished['countries'][country]
    stem=country.replace(' ','_')
    master=source/f'summary_beruski_{stem}.csv.gz'
    expected=population['retained'][country]
    excluded=set(population['excluded'][country])
    if digest(master)!=info['sha256']:raise ValueError('Master SHA256 mismatch')
    if out.exists():raise FileExistsError(f'Preserve existing analysis: {out}')
    out.mkdir(parents=True)
    acc={};seen=set();discarded=set();rawrows=keptrows=0
    gids={v:i for i,v in enumerate(GCMS)};pids={v:i for i,v in enumerate(PLANTS)}
    print('READING',country,master,flush=True)
    with gzip.open(master,'rt',newline='',encoding='utf-8') as stream:
        reader=csv.reader(stream);header=next(reader);idx={v:i for i,v in enumerate(header)}
        required=COLUMNS+['country','cell_id','period','scenario','simulation_year','gcm',
                          'planting_label','plant_date','harvest_area_ha','latitude','longitude']
        if len(idx)!=len(header) or not set(required)<=idx.keys():raise ValueError('Invalid input columns')
        metric_indices=[idx[c] for c in COLUMNS]
        for row in reader:
            rawrows+=1
            if len(row)!=len(header):raise ValueError('Malformed CSV row')
            get=lambda name:row[idx[name]]
            if get('country')!=country:raise ValueError('Mixed country source')
            cell=str(int(get('cell_id')))
            if cell not in expected:
                if cell not in excluded:raise ValueError(f'Point not in approved population or exclusions: {country}/{cell}')
                discarded.add(cell);continue
            meta=expected[cell]
            for field,tolerance in [('harvest_area_ha',1e-5),('latitude',1e-4),('longitude',1e-4)]:
                actual=float(get(field));target=float(meta[field])
                if not math.isfinite(actual) or not math.isclose(actual,target,abs_tol=tolerance,rel_tol=1e-8):
                    raise ValueError(f'Population metadata differs: {cell}/{field}')
            values=[int(row[j]) for j in metric_indices]
            for k in range(0,16,2):
                if values[k+1]<0 or values[k]!=min(6,values[k+1]):raise ValueError('Invalid raw/capped count')
            year=int(get('simulation_year'));group=group_index(get('period'),get('scenario'),year)
            key=(cell,group,gids[get('gcm')],pids[get('planting_label')])
            add_member(acc,key,year,values);seen.add(cell);keptrows+=1
            if rawrows%1000000==0:print('ROWS',country,rawrows,flush=True)
    if rawrows!=info['rows']:raise ValueError('Master row count mismatch')
    if seen!=set(expected):raise ValueError(f'Missing retained points: {sorted(set(expected)-seen)[:20]}')
    if keptrows!=len(expected)*4600:raise ValueError('Not 4600 complete simulations per retained point')
    nation=defaultdict(lambda:[0.]*5)
    area=math.fsum(float(meta['harvest_area_ha']) for meta in expected.values())
    if area<=0 or any(float(m['harvest_area_ha'])<=0 for m in expected.values()):raise ValueError('Invalid represented area')
    configurations=[(m,b,c) for m in METHODS for b in (14,16) for c in ('cap6','uncapped')]
    def points():
        for cell in sorted(expected,key=int):
            meta=expected[cell];weight=float(meta['harvest_area_ha'])
            base_g=[expand(v) for v in means(acc,cell,0)]
            base=[math.fsum(v[j] for v in base_g)/5 for j in range(20)]
            for group in range(1,7):
                future_g=[expand(v) for v in means(acc,cell,group)]
                future=[math.fsum(v[j] for v in future_g)/5 for j in range(20)]
                for j,(method,buffer,cap) in enumerate(configurations):
                    b,f=base[j],future[j];d=f-b
                    gd=[future_g[g][j]-base_g[g][j] for g in range(5)]
                    for gcm,bv,fv in [('ALL',b,f)]+[(GCMS[g],base_g[g][j],future_g[g][j]) for g in range(5)]:
                        stats=nation[(country,group,method,buffer,cap,gcm)]
                        stats[0]+=weight*bv;stats[1]+=weight*fv
                        stats[{'increase':2,'decrease':3,'unchanged':4}[sign(fv-bv)]]+=weight
                    yield dict(country=country,cell_id=cell,latitude=meta['latitude'],longitude=meta['longitude'],
                        harvest_area_ha=weight,period=GROUPS[group][0],scenario=GROUPS[group][1],
                        method=method,buffer_days=buffer,cap=cap,baseline=b,future=f,delta=d,
                        relative_change_pct=percent(b,f),direction=sign(d),paired_members=25,
                        gcm_increase=sum(sign(x)=='increase' for x in gd),
                        gcm_decrease=sum(sign(x)=='decrease' for x in gd),
                        gcm_unchanged=sum(sign(x)=='unchanged' for x in gd),
                        gcm_delta_min=min(gd),gcm_delta_max=max(gd))
    csv_write(out/'point_changes.csv.gz',points())
    rows=summarize(nation,area,len(expected))
    national=[r for r in rows if r['gcm']=='ALL']
    by_gcm=[r for r in rows if r['gcm']!='ALL']
    for row in national:
        peers=[r for r in by_gcm if all(r[k]==row[k] for k in ('country','period','scenario','method','buffer_days','cap'))]
        row['gcms_increase']=sum(r['direction']=='increase' for r in peers)
        row['gcms_decrease']=sum(r['direction']=='decrease' for r in peers)
        row['gcms_unchanged']=sum(r['direction']=='unchanged' for r in peers)
    csv_write(out/'country_changes.csv',national);csv_write(out/'country_changes_by_gcm.csv',by_gcm)
    save(out/'analysis_complete.json',dict(status='completed',country=country,source=str(master),
        source_sha256=info['sha256'],raw_rows=rawrows,retained_rows=keptrows,retained_points=len(expected),
        excluded_present=sorted(discarded,key=int),area_ha=area,policy=finished.get('policy'),
        method='year mean per GCM x planting; equal 25-member weights per point; fixed-area country mean; ensemble4 equal rule weights',
        interpretation='future-minus-historical period contrasts; not temporal regression or control efficacy',
        source_complete_sha256=digest(source/'complete.json'),script_sha256=digest(__file__),
        population_provenance=population['provenance'],
        outputs={p.name:digest(p) for p in out.iterdir() if p.suffix in ('.csv','.gz')}))
    print('ANALYSIS COMPLETE',country,len(expected),'points',flush=True)


def report(out):
    combined=[]
    for country in ('Brazil','United States'):
        folder=out/country.replace(' ','_')
        audit=load(folder/'analysis_complete.json')
        if audit['status']!='completed':raise ValueError('Incomplete analysis')
        path=folder/'country_changes.csv'
        if digest(path)!=audit['outputs'][path.name]:raise ValueError('Summary changed')
        with path.open(newline='',encoding='utf-8') as stream:combined.extend(csv.DictReader(stream))
    csv_write(out/'country_changes_all.csv',combined)
    lines=['Country | Period | Scenario | Rule | Buffer | Cap | Historical | Future | Delta | Change % | GCM +/-']
    for r in combined:
        pct=f"{float(r['relative_change_pct']):+.2f}%" if r['relative_change_pct'] else 'undefined (baseline=0)'
        lines.append(f"{r['country']} | {r['period']} | {r['scenario']} | {r['method']} | {r['buffer_days']} | {r['cap']} | "
            f"{float(r['baseline']):.4f} | {float(r['future']):.4f} | {float(r['delta']):+.4f} | {pct} | {r['gcms_increase']}/{r['gcms_decrease']}")
    text='\n'.join(lines)+'\n';(out/'report.txt').write_text(text,encoding='utf-8');print(text)
    save(out/'analysis_complete.json',dict(status='completed',countries=2,comparisons=len(combined),
        country_changes_sha256=digest(out/'country_changes_all.csv')))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--country',choices=['Brazil','United States']);p.add_argument('--population',type=Path)
    p.add_argument('--report',action='store_true');args=p.parse_args()
    if args.report:report(args.out)
    else:analyze(args.source,args.out,args.country,load(args.population))
