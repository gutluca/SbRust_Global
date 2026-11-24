"""Read-only, complete-country checks and area-weighted onset-DSV trends."""
import argparse
from collections import Counter
import csv
from datetime import date,timedelta
import gzip
import json
import math
import os
from pathlib import Path
import analyze_beruski_trends as a
import ensemble3_global as e

METRICS=[(rule,cap) for rule in ('DSV50','DSV80','ENSEMBLE2') for cap in ('uncapped','cap6')]
COLS=['onset_'+rule.lower()+'_r14_'+cap for rule,cap in METRICS]

def integer(v,label):
    x=float(v)
    if not math.isfinite(x) or x<0 or not x.is_integer():raise ValueError('Invalid '+label+': '+str(v))
    return int(x)

def check_row(row):
    if integer(row['buffer_days'],'buffer')!=14 or integer(row['emergence_lag_assumed_days'],'lag')!=5:
        raise ValueError('Wrong residual or onset convention')
    plant=date.fromisoformat(row['plant_date']);onset_text=row['onset_date_approx']
    absent=row['disease_onset_dae'].strip() in ('','-99')
    if absent:
        if onset_text:raise ValueError('Onset date without onset DAE')
        onset=None
    else:
        onset=plant+timedelta(days=5+integer(row['disease_onset_dae'],'onset DAE'))
        if onset.isoformat()!=onset_text:raise ValueError('Onset calendar mismatch')
    before=integer(row['eligible_days_original'],'eligible days')
    after=integer(row['eligible_days_after_onset'],'eligible days after onset')
    if after>before or absent and after:raise ValueError('Invalid eligible window')
    values=[]
    for rule in ('dsv50','dsv80'):
        prefix='onset_'+rule+'_r14'
        raw=integer(row[prefix+'_uncapped'],'count');cap=integer(row[prefix+'_cap6'],'cap')
        if cap!=min(6,raw) or raw>after or absent and raw:raise ValueError('Count/cap/window mismatch')
        dates=[date.fromisoformat(v) for v in row[prefix+'_dates'].split(';') if v]
        if len(dates)!=raw or any(onset is None or d<onset for d in dates):raise ValueError('Event date/count mismatch')
        if any((b-a).days<15 for a,b in zip(dates,dates[1:])):raise ValueError('Residual violated')
        values.extend((raw,cap))
    values.extend(((values[0]+values[2])/2,(values[1]+values[3])/2))
    for key,value in zip(COLS,values):
        if float(row[key])!=value:raise ValueError('Invalid ensemble or count: '+key)
    return values,before,after,absent

def fresh():return dict(rows=0,weight=0.,sums=[0.]*6,zero=[0.]*6,cut=[0.]*6,maximum=[0.]*6,before=0.,after=0.,no_onset=0.)
def add(entry,weight,values,before,after,absent):
    entry['rows']+=1;entry['weight']+=weight;entry['before']+=weight*before;entry['after']+=weight*after;entry['no_onset']+=weight*absent
    for i,value in enumerate(values):
        entry['sums'][i]+=weight*value;entry['zero'][i]+=weight*(value==0)
        entry['maximum'][i]=max(entry['maximum'][i],value)
        entry['cut'][i]+=weight*(values[i//2*2]!=values[i//2*2+1])

def mean(entry):return [v/entry['weight'] for v in entry['sums']]
def normalize(entries):
    # Equal weights for complete annual GCM members, after spatial/planting aggregation.
    n=len(entries)
    return {key:[math.fsum(v[key][i]/v['weight'] for v in entries)/n for i in range(6)]
            for key in ('sums','zero','cut')} | {
        key:math.fsum(v[key]/v['weight'] for v in entries)/n for key in ('before','after','no_onset')}

def prepare(source,population,out):
    pop=a.load(population);countries=[]
    for marker in sorted((source/'countries').glob('*/complete.json')):
        rec=a.load(marker)
        if rec.get('status')!='completed':continue
        country=rec['country'];points=len(pop['retained'][country])
        if rec['points']!=points or rec['rows']!=points*e.N:raise ValueError('Country completion/population mismatch')
        path=Path(rec['master'])
        if path.parent.resolve()!=marker.parent.resolve() or not path.is_file():raise ValueError('Wrong master location')
        if country in {v['country'] for v in countries}:raise ValueError('Duplicate country')
        countries.append(dict(country=country,master=str(path),sha256=rec['sha256'],rows=rec['rows'],points=points,
            marker=str(marker),marker_sha256=a.digest(marker),folder=marker.parent.name,reused_br_us=rec.get('reused_br_us',False)))
    if not countries:raise ValueError('No completed country masters yet')
    out.mkdir(parents=True,exist_ok=False)
    a.save(out/'population.json',pop)
    a.save(out/'plan.json',dict(source=str(source),population_sha256=a.digest(out/'population.json'),countries=countries,
        scope='snapshot of completed countries only; running campaign unchanged'))
    for i,rec in enumerate(countries):print(i,rec['country'],rec['rows'],'rows',flush=True)
    print('SNAPSHOT_COUNTRIES='+str(len(countries)),flush=True)

def analyze(out,index):
    plan=a.load(out/'plan.json');rec=plan['countries'][index];country=rec['country'];path=Path(rec['master'])
    if a.digest(path)!=rec['sha256'] or a.digest(rec['marker'])!=rec['marker_sha256']:raise ValueError('Published source changed')
    if a.digest(out/'population.json')!=plan['population_sha256']:raise ValueError('Population changed')
    pop=a.load(out/'population.json')['retained'][country];cells=sorted(pop,key=int);cell_index={v:i for i,v in enumerate(cells)}
    seen=bytearray(len(cells)*e.N);annual={};point={};static={};rows=0;missing_maturity=0
    dest=out/rec['folder'];dest.mkdir(exist_ok=False)
    try:
        with gzip.open(path,'rt',encoding='utf-8-sig',newline='') as f:
            reader=csv.DictReader(f)
            for row in reader:
                cell=str(int(row['cell_id']));year=int(row['simulation_year']);gcm=row['gcm'];planting=row['planting_label']
                group=a.group_index(row['period'],row['scenario'],year)
                if row['country']!=country or cell not in pop or row['simulation_id']!=e.identity(country,cell,group,year,gcm,planting):raise ValueError('Simulation identity mismatch')
                slot=cell_index[cell]*e.N+e.slot(group,year,gcm,planting)
                if seen[slot]:raise ValueError('Duplicate simulation')
                seen[slot]=1
                for field in ('latitude','longitude','harvest_area_ha'):
                    value=float(row[field]);expected=float(pop[cell][field])
                    if not math.isfinite(value) or not math.isclose(value,expected,rel_tol=1e-8,abs_tol=1e-4):raise ValueError('Population metadata differs: '+field)
                weight=float(pop[cell]['harvest_area_ha'])
                if weight<=0:raise ValueError('Invalid harvest area')
                mg=row['maturity_group']
                if cell in static and static[cell]!=mg:raise ValueError('GM changes within point')
                static[cell]=mg
                values,before,after,absent=check_row(row)
                add(annual.setdefault((group,year,gcm),fresh()),weight,values,before,after,absent)
                pt=point.setdefault((cell,group),[0,[0.]*6]);pt[0]+=1
                for i,v in enumerate(values):pt[1][i]+=v
                missing_maturity+=row['maturity_date'].strip() in ('','-99','-999','-9')
                rows+=1
                if rows%1000000==0:print('CHECKED',country,rows,flush=True)
        if rows!=rec['rows'] or 0 in seen:raise ValueError('Incomplete 4600-member point matrix')
        if len(annual)!=920:raise ValueError('Incomplete annual/GCM matrix')
        for entry in annual.values():
            if entry['rows']!=len(cells)*5:raise ValueError('Incomplete annual planting/point coverage')
        total_area=math.fsum(float(pop[c]['harvest_area_ha']) for c in cells)
        period={};member={};yearly=[];year_gcm=[];period_rows=[];trends=[];gcm_trends=[]
        def record(group,year,gcm,metric,stats,baseline):
            rule,cap=METRICS[metric];future=stats['sums'][metric]
            return dict(country=country,period=a.GROUPS[group][0],scenario=a.GROUPS[group][1],year=year,gcm=gcm,rule=rule,cap=cap,buffer_days=14,
                mean_applications=future,historical_mean=baseline,delta=future-baseline,relative_change_pct=a.percent(baseline,future),
                zero_applications_area_planting_year_pct=100*stats['zero'][metric],affected_by_cap_pct=100*stats['cut'][metric],
                mean_eligible_days=stats['before'],mean_eligible_days_after_onset=stats['after'],no_onset_pct=100*stats['no_onset'])
        for group,(_,_,lo,hi) in enumerate(a.GROUPS):
            entries=[annual[group,y,g] for y in range(lo,hi+1) for g in a.GCMS]
            period[group]=normalize(entries)
            for g in a.GCMS:member[group,g]=normalize([annual[group,y,g] for y in range(lo,hi+1)])
        for group,(_,_,lo,hi) in enumerate(a.GROUPS):
            for j in range(6):period_rows.append(record(group,'ALL','ALL',j,period[group],period[0]['sums'][j]))
            for year in range(lo,hi+1):
                ensemble=normalize([annual[group,year,g] for g in a.GCMS])
                for j in range(6):yearly.append(record(group,year,'ALL',j,ensemble,period[0]['sums'][j]))
                for gcm in a.GCMS:
                    stats=normalize([annual[group,year,gcm]])
                    for j in range(6):year_gcm.append(record(group,year,gcm,j,stats,member[0,gcm]['sums'][j]))
            if group==0:continue
            for j in range(6):
                changes=[member[group,g]['sums'][j]-member[0,g]['sums'][j] for g in a.GCMS]
                directions=[0.,0.,0.]
                for cell in cells:
                    b=point[cell,0];fut=point[cell,group];delta=fut[1][j]/fut[0]-b[1][j]/b[0]
                    directions[0 if delta>1e-12 else 1 if delta< -1e-12 else 2]+=float(pop[cell]['harvest_area_ha'])
                row=record(group,'ALL','ALL',j,period[group],period[0]['sums'][j])
                row.update(gcms_increase=sum(v>1e-12 for v in changes),gcms_decrease=sum(v< -1e-12 for v in changes),
                    area_increase_pct=100*directions[0]/total_area,area_decrease_pct=100*directions[1]/total_area,area_unchanged_pct=100*directions[2]/total_area)
                trends.append(row)
                for g in a.GCMS:gcm_trends.append(record(group,'ALL',g,j,member[group,g],member[0,g]['sums'][j]))
        def point_rows():
            for cell in cells:
                for group in range(1,7):
                    for j,(rule,cap) in enumerate(METRICS):
                        b=point[cell,0];fut=point[cell,group];baseline=b[1][j]/b[0];future=fut[1][j]/fut[0]
                        yield dict(country=country,cell_id=cell,**pop[cell],maturity_group=static[cell],period=a.GROUPS[group][0],scenario=a.GROUPS[group][1],
                            rule=rule,cap=cap,historical_mean=baseline,future_mean=future,delta=future-baseline,relative_change_pct=a.percent(baseline,future))
        for name,data in [('country_year_gcm.csv',year_gcm),('country_year.csv',yearly),('country_period_means.csv',period_rows),
                          ('country_period_trends.csv',trends),('country_period_gcm_trends.csv',gcm_trends),('point_trends.csv.gz',point_rows())]:
            a.csv_write(dest/name,data)
        # Recheck source immutability after the streaming pass.
        if a.digest(path)!=rec['sha256']:raise ValueError('Master changed during analysis')
        maxima={COLS[i]:max(v['maximum'][i] for v in annual.values()) for i in range(6)}
        a.save(dest/'complete.json',dict(status='completed',country=country,source_sha256=rec['sha256'],rows=rows,points=len(cells),
            missing_maturity_rows=missing_maturity,maximum_applications=maxima,checks='hash; full matrix; identity; area/GM; onset; event dates; 14 complete-day residual; caps; ensemble; window counts',
            interpretation='Internal consistency only; no validation of efficacy or agreement with loss trend',
            outputs={p.name:a.digest(p) for p in dest.iterdir() if p.suffix in ('.csv','.gz')}))
        print('COUNTRY ANALYSIS COMPLETE',country,rows,flush=True)
    except Exception as error:
        a.save(dest/'failure.json',dict(country=country,row=rows+1,error=str(error)))
        raise

def report(out):
    plan=a.load(out/'plan.json');lines=['ANALISE DE MESTRAS CONCLUIDAS — BUFFER 14'];combined=[];audits=[]
    for rec in plan['countries']:
        folder=out/rec['folder'];done=a.load(folder/'complete.json')
        if done['status']!='completed' or done['source_sha256']!=rec['sha256']:raise ValueError('Incomplete analysis')
        for name,sha in done['outputs'].items():
            if a.digest(folder/name)!=sha:raise ValueError('Analysis output changed')
        audits.append(done);lines.append(f"\n{rec['country']}: {done['points']} pontos; {done['rows']} linhas; verificacoes OK; maturacao ausente={done['missing_maturity_rows']}")
        lines.append('Maximos sem teto: '+', '.join(f"{rule}={done['maximum_applications']['onset_'+rule.lower()+'_r14_uncapped']}" for rule in ('DSV50','DSV80','ENSEMBLE2')))
        lines.append('PERIODO | CENARIO | REGRA | TETO | HISTORICO | FUTURO | DELTA | % | GCMS SOBEM/CAEM')
        with (folder/'country_period_trends.csv').open(encoding='utf-8',newline='') as f:
            for r in csv.DictReader(f):
                combined.append(r);pct=f"{float(r['relative_change_pct']):+.2f}" if r['relative_change_pct'] else 'NA'
                lines.append(f"{r['period']} | {r['scenario']} | {r['rule']} | {r['cap']} | {float(r['historical_mean']):.4f} | {float(r['mean_applications']):.4f} | {float(r['delta']):+.4f} | {pct} | {r['gcms_increase']}/{r['gcms_decrease']}")
    a.csv_write(out/'country_period_trends_all.csv',combined)
    lines.extend(['\nAgregacao: cinco plantios com pesos iguais por ponto; ponderacao pela area fixa; pesos iguais entre anos e cinco GCMs.',
        'Cada ano futuro e comparado com a media historica 1981–2014; o arquivo por GCM usa o historico do mesmo GCM.',
        'Checks OK indicam consistencia interna, nao eficacia comprovada, significancia estatistica ou obrigacao de acompanhar o sinal das perdas.',
        'Sem maturacao registrada nao implica erro nesta regra: a janela e diaria, derivada de DAE/LAI.',
        'Campanha e mestras originais nao foram alteradas. Paises ainda em execucao nao entram nesta fotografia.'])
    text='\n'.join(lines)+'\n';(out/'report.txt').write_text(text,encoding='utf-8');a.save(out/'complete.json',dict(status='completed',countries=audits));print(text)

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','analyze','report']);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--source',type=Path);p.add_argument('--population',type=Path);p.add_argument('--index',type=int);args=p.parse_args()
    if args.action=='prepare':prepare(args.source,args.population,args.out)
    elif args.action=='analyze':analyze(args.out,args.index)
    else:report(args.out)
if __name__=='__main__':main()
