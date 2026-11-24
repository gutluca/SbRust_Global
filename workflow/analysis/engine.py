"""Audited 23 November calculations with portable paths and canonical expected keys.

Run via run_analysis.py. Original unmodified reference is in reference/.
No old campaign output is required. No scientific formula/filter was changed.
"""
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor,as_completed
import hashlib,json,time,gzip,os
from itertools import product
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[2]
OUT=Path(os.environ.get('SBRUST_ANALYSIS_OUTPUT', str(ROOT/'results/analysis')))
GCMS=['GFDL-ESM4','IPSL-CM6A-LR','MPI-ESM1-2-HR','MRI-ESM2-0','UKESM1-0-LL']
PLANTS=['P01','P02','P03','P04','P05']
PERIOD_SCENARIOS=[('1981-2014','historical')]+[(p,s) for p in ['2020-2039','2040-2069'] for s in ['ssp126','ssp370','ssp585']]

def member_template(meta):
 return pd.DataFrame([(cell,p,s,g,pl) for cell,(p,s),g,pl in product(meta.CELL_ID,PERIOD_SCENARIOS,GCMS,PLANTS)],columns=K)

def annual_template():
 cases=[('1981-2014','historical',y) for y in range(1981,2015)]+[(('2020-2039' if y<2040 else '2040-2069'),s,y) for s in ['ssp126','ssp370','ssp585'] for y in range(2020,2070)]
 return pd.DataFrame([(p,s,y,g,pl) for (p,s,y),g,pl in product(cases,GCMS,PLANTS)],columns=['PERIOD','SCENARIO','SIM_YEAR','GCM','PLANTING_LABEL'])
METRICS=['Y0','YASR','LOSS','R','SPRAYS','ONSET','ONSET_REPORTED','R_GE10','R_GE25','R_GE50']
UNITS=dict(Y0='kg/ha',YASR='kg/ha',LOSS='kg/ha',R='%',SPRAYS='applications/crop',ONSET='DAE',ONSET_REPORTED='%',R_GE10='%',R_GE25='%',R_GE50='%')
K=['CELL_ID','PERIOD','SCENARIO','GCM','PLANTING_LABEL']
def sha(p):
 with open(p,'rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def worker(a):
 folder=OUT/'countries'/a.get('folder',Path(a['source']).parent.name);folder.mkdir(parents=True,exist_ok=True)
 assert sha(a['source'])==a['sha256']
 if (folder/'audit.json').exists():
  cached=json.loads((folder/'audit.json').read_text())
  if cached['sha256']!=a['sha256']:raise ValueError(f"Stale source cache: {folder}")
  return cached
 meta=pd.read_csv(OUT/'metadata'/(folder.name+'.csv'));keep=set(meta.CELL_ID)
 exclusions=[];before=0;removed_file=gzip.open(folder/'excluded_simulations.csv.gz','wt',encoding='utf-8',newline='');removed_header=True
 parts=[];annual=[];qc={v:dict(n=0,missing=0,negative=0,min=np.inf,max=-np.inf) for v in METRICS};resid=0;n=0
 for d in pd.read_csv(a['source'],sep=';',chunksize=200000):
  d=d[d.CELL_ID.isin(keep)].copy()
  if d.empty:continue
  before+=len(d)
  x=pd.to_numeric(d.YIELD_NO_DISEASE_KG_HA,errors='coerce');y=pd.to_numeric(d.YIELD_WITH_DISEASE_KG_HA,errors='coerce')
  finite=np.isfinite(x)&np.isfinite(y)
  zero=x.eq(0);negative_loss=finite&(y>x);invalid=(~finite)|(x<0)|(y<0)
  lx=pd.to_numeric(d.LOSS_KG_HA,errors='coerce');lr=pd.to_numeric(d.LOSS_PCT,errors='coerce')
  non_numeric_loss=~np.isfinite(lx)|~np.isfinite(lr)
  negative_reported_loss=lx.lt(0)|lr.lt(0)
  extra_loss_only=(non_numeric_loss|negative_reported_loss)&~(zero|negative_loss|invalid)
  remove=zero|negative_loss|invalid|non_numeric_loss|negative_reported_loss
  counts=d[['COUNTRY','CELL_ID','PERIOD','SCENARIO','GCM','PLANTING_LABEL']].copy()
  counts['input_rows']=1;counts['zero_Y0']=zero.astype(int);counts['negative_loss']=negative_loss.astype(int)
  counts['zero_and_negative_loss']=(zero&negative_loss).astype(int);counts['other_invalid_yield']=invalid.astype(int)
  counts['excluded_rows']=remove.astype(int);counts['retained_rows']=(~remove).astype(int)
  counts['non_numeric_reported_loss']=non_numeric_loss.astype(int);counts['negative_reported_loss']=negative_reported_loss.astype(int);counts['reported_loss_only']=extra_loss_only.astype(int)
  exclusions.append(counts.groupby(['COUNTRY','CELL_ID','PERIOD','SCENARIO','GCM','PLANTING_LABEL']).sum().reset_index())
  removed=d.loc[remove,['SIMULATION_ID','COUNTRY','CELL_ID','PERIOD','SIM_YEAR','SCENARIO','GCM','PLANTING_LABEL','YIELD_NO_DISEASE_KG_HA','YIELD_WITH_DISEASE_KG_HA','LOSS_KG_HA','LOSS_PCT']].copy()
  removed['zero_Y0']=zero[remove];removed['negative_loss']=negative_loss[remove];removed['other_invalid_yield']=invalid[remove]
  removed['non_numeric_reported_loss']=non_numeric_loss[remove];removed['negative_reported_loss']=negative_reported_loss[remove]
  removed.to_csv(removed_file,index=False,header=removed_header);removed_header=False
  d=d.loc[~remove].copy();x=x.loc[~remove];y=y.loc[~remove]
  if d.empty:continue
  n+=len(d)
  assert (x>0).all() and (y>=0).all() and (y<=x).all()
  paired=np.isfinite(x)&np.isfinite(y)&(x>=0)&(y>=0)
  d['Y0']=x.where(paired);d['YASR']=y.where(paired);d['LOSS']=(x-y).where(paired)
  d['R']=(100*(x-y)/x).where(paired&(x>0))
  assert np.allclose(d.R,pd.to_numeric(d.LOSS_PCT,errors='coerce'),rtol=0,atol=1e-6)
  assert np.allclose(d.LOSS,pd.to_numeric(d.LOSS_KG_HA,errors='coerce'),rtol=0,atol=1e-8)
  r=np.abs(d.LOSS-pd.to_numeric(d.LOSS_KG_HA,errors='coerce'));resid=max(resid,float(r.max()))
  d['SPRAYS']=pd.to_numeric(d.TOTAL_SPRAYS,errors='coerce');d['SPRAYS']=d.SPRAYS.where(np.isfinite(d.SPRAYS)&(d.SPRAYS>=0))
  onset=pd.to_numeric(d.DISEASE_ONSET_DAE,errors='coerce');d['ONSET']=onset.where(np.isfinite(onset)&(onset>=0))
  d['ONSET_REPORTED']=100*d.ONSET.notna().astype(float)
  for t in [10,25,50]:d['R_GE'+str(t)]=(100*(d.R>=t).astype(float)).where(d.R.notna())
  for v in METRICS:
   z=d[v];valid=z.dropna();q=qc[v];q['n']+=int(z.notna().sum());q['missing']+=int(z.isna().sum());q['negative']+=int((z<0).sum())
   if len(valid):q['min']=min(q['min'],float(valid.min()));q['max']=max(q['max'],float(valid.max()))
  sums=d.groupby(K,sort=False)[METRICS].agg(['sum','count']);sums.columns=['_'.join(c) for c in sums.columns];parts.append(sums.reset_index())
  ak=['PERIOD','SCENARIO','SIM_YEAR','GCM','PLANTING_LABEL'];work=d[ak+['HARVEST_AREA_HA']].copy()
  for v in METRICS:work[v+'_weighted']=d[v]*d.HARVEST_AREA_HA;work[v+'_area']=d.HARVEST_AREA_HA.where(d[v].notna(),0)
  annual.append(work.groupby(ak,sort=False).sum(numeric_only=True).reset_index())
 removed_file.close()
 ex=pd.concat(exclusions).groupby(['COUNTRY']+K).sum(numeric_only=True).reset_index()
 ex.to_csv(folder/'exclusion_counts.csv.gz',index=False)
 assert before==a['rows_retained'] and ex.input_rows.sum()==before and ex.retained_rows.sum()==n
 template=member_template(meta)
 columns=K+[v+suffix for v in METRICS for suffix in ['_sum','_count']]
 m=pd.concat(parts) if parts else pd.DataFrame(columns=columns)
 m=m.groupby(K,sort=False).sum(numeric_only=True).reindex(pd.MultiIndex.from_frame(template[K]),fill_value=0).reset_index()
 assert len(m)==len(meta)*175
 for v in METRICS:m[v]=m[v+'_sum']/m[v+'_count'].replace(0,np.nan)
 m.insert(0,'COUNTRY',a['country']);m.to_csv(folder/'member_means.csv.gz',index=False)
 gm=m.groupby(['COUNTRY','CELL_ID','PERIOD','SCENARIO','GCM'])[METRICS].agg(['mean','count']);gm.columns=['_'.join(c) for c in gm.columns];gm=gm.reset_index()
 for v in METRICS:gm[v]=gm[v+'_mean'].where(gm[v+'_count']==5)
 gm=gm[['COUNTRY','CELL_ID','PERIOD','SCENARIO','GCM']+METRICS]
 b=gm[gm.SCENARIO=='historical'][['COUNTRY','CELL_ID','GCM']+METRICS].rename(columns={v:v+'_base' for v in METRICS})
 f=gm[gm.SCENARIO!='historical'].merge(b,on=['COUNTRY','CELL_ID','GCM'],validate='many_to_one').merge(meta,on=['COUNTRY','CELL_ID'],validate='many_to_one')
 f.to_csv(folder/'gcm_comparisons.csv.gz',index=False)
 # Sowing alternatives kept separate, five complete paired GCMs required per alternative.
 mb=m[m.SCENARIO=='historical'][['CELL_ID','GCM','PLANTING_LABEL']+METRICS].rename(columns={v:v+'_base' for v in METRICS})
 ps=m[m.SCENARIO!='historical'][K+METRICS].merge(mb,on=['CELL_ID','GCM','PLANTING_LABEL'],validate='many_to_one').merge(meta[['CELL_ID','HARVEST_AREA_HA']],on='CELL_ID')
 pr=[]
 for v in METRICS:
  z=ps[ps[v].notna()&ps[v+'_base'].notna()].groupby(['CELL_ID','PERIOD','SCENARIO','PLANTING_LABEL']).agg(future=(v,'mean'),baseline=(v+'_base','mean'),n=(v,'count'),area=('HARVEST_AREA_HA','first')).reset_index();z=z[z.n==5]
  for key,g in z.groupby(['PERIOD','SCENARIO','PLANTING_LABEL']):
   pr.append(dict(COUNTRY=a['country'],PERIOD=key[0],SCENARIO=key[1],PLANTING_LABEL=key[2],metric=v,area_ha=g.area.sum(),baseline=np.average(g.baseline,weights=g.area),future=np.average(g.future,weights=g.area)))
 pd.DataFrame(pr,columns=['COUNTRY','PERIOD','SCENARIO','PLANTING_LABEL','metric','area_ha','baseline','future']).to_csv(folder/'planting_summary.csv',index=False)
 ak=['PERIOD','SCENARIO','SIM_YEAR','GCM','PLANTING_LABEL']
 at=annual_template()
 an=pd.concat(annual) if annual else pd.DataFrame(columns=ak+['HARVEST_AREA_HA']+[v+s for v in METRICS for s in ['_weighted','_area']])
 an=an.groupby(ak).sum(numeric_only=True).reindex(pd.MultiIndex.from_frame(at[ak]),fill_value=0).reset_index()
 for v in METRICS:an[v]=an[v+'_weighted']/an[v+'_area'].replace(0,np.nan)
 an.insert(0,'COUNTRY',a['country']);an.to_csv(folder/'annual.csv.gz',index=False)
 result=dict(rows_before_filter=before,excluded_rows=before-n,exclusion_totals={c:int(ex[c].sum()) for c in ['zero_Y0','negative_loss','zero_and_negative_loss','other_invalid_yield','non_numeric_reported_loss','negative_reported_loss','reported_loss_only']},country=a['country'],sha256=a['sha256'],source=a['source'],rows=n,points=len(meta),area_ha=float(meta.HARVEST_AREA_HA.sum()),loss_formula_max_residual=resid,qc=qc)
 (folder/'audit.json').write_text(json.dumps(result,indent=2));return result
def wq(x,w,q):
 order=np.argsort(x);return float(np.asarray(x)[order][np.searchsorted(np.cumsum(np.asarray(w)[order]),q*np.sum(w),side='left')])
def consolidate(audits, target_countries=None, include_filter_audit=True):
 folders=sorted((OUT/'countries').iterdir());f=pd.concat([pd.read_csv(p/'gcm_comparisons.csv.gz') for p in folders],ignore_index=True)
 meta=pd.concat([pd.read_csv(OUT/'metadata'/(p.name+'.csv')) for p in folders]);meta.to_csv(OUT/'country_cell_metadata.csv',index=False)
 summary=[];gcmrows=[];locrows=[]
 for v in METRICS:
  valid=f[v].notna()&f[v+'_base'].notna();d=f.copy();d['future']=d[v].where(valid);d['baseline']=d[v+'_base'].where(valid);d['delta']=d.future-d.baseline
  d['positive_delta']=d.delta.gt(0);d['negative_delta']=d.delta.lt(0)
  z=d.groupby(['COUNTRY','CELL_ID','PERIOD','SCENARIO']).agg(future=('future','mean'),baseline=('baseline','mean'),delta=('delta','mean'),n_gcm=('delta','count'),n_positive=('positive_delta','sum'),n_negative=('negative_delta','sum'),gcm_delta_min=('delta','min'),gcm_delta_max=('delta','max'),area_ha=('HARVEST_AREA_HA','first')).reset_index();z['metric']=v
  for c in ['future','baseline','delta']:z.loc[z.n_gcm!=5,c]=np.nan
  locrows.append(z)
  countries=sorted(meta.COUNTRY.unique()) if target_countries is None else sorted(target_countries)
  for scope,country in [('country',c) for c in countries]+[('global','ALL')]:
   countrytotal=float(meta.loc[meta.COUNTRY==country,'HARVEST_AREA_HA'].sum()) if scope=='country' else float(meta.HARVEST_AREA_HA.sum())
   zz=z[z.COUNTRY==country] if scope=='country' else z
   dd=d[d.COUNTRY==country] if scope=='country' else d
   for (period,scenario),p in zz.groupby(['PERIOD','SCENARIO']):
    good=p[p.n_gcm==5];area=good.area_ha.sum();row=dict(scope=scope,COUNTRY=country,PERIOD=period,SCENARIO=scenario,metric=v,unit=UNITS[v],locations_valid=len(good),locations_total=len(p),valid_area_ha=area,excluded_area_ha=p.loc[p.n_gcm!=5,'area_ha'].sum(),study_area_ha=countrytotal)
    if len(good):
     w=good.area_ha
     for c in ['baseline','future','delta']:row[c]=np.average(good[c],weights=w)
     row['relative_change_pct']=100*row['delta']/abs(row['baseline']) if row['baseline']!=0 else np.nan
     for q in [.05,.25,.5,.75,.95]:row['spatial_delta_wq'+str(q)]=wq(good.delta,w,q)
     row['delta_min']=good.delta.min();row['delta_max']=good.delta.max();row['spatial_weighted_sd']=np.sqrt(np.average((good.delta-row['delta'])**2,weights=w))
     for label,cond in [('decreasing',good.n_negative.ge(4)&good.delta.lt(0)),('increasing',good.n_positive.ge(4)&good.delta.gt(0))]:row[label+'_area_pct']=100*w[cond].sum()/area
     row['uncertain_area_pct']=100-row['decreasing_area_pct']-row['increasing_area_pct']
     if v in ['Y0','YASR','LOSS']:row['fixed_area_equivalent_baseline_t']=row['baseline']*area/1000;row['fixed_area_equivalent_future_t']=row['future']*area/1000;row['fixed_area_equivalent_delta_t']=row['delta']*area/1000
     keys=pd.MultiIndex.from_frame(good[['COUNTRY','CELL_ID']]);sub=dd[(dd.PERIOD==period)&(dd.SCENARIO==scenario)];sub=sub[pd.MultiIndex.from_frame(sub[['COUNTRY','CELL_ID']]).isin(keys)]
     gm=[]
     for name,ss in sub.groupby('GCM'):
      gr=dict(scope=scope,COUNTRY=country,PERIOD=period,SCENARIO=scenario,metric=v,GCM=name,valid_area_ha=ss.HARVEST_AREA_HA.sum())
      for c in ['baseline','future','delta']:gr[c]=np.average(ss[c],weights=ss.HARVEST_AREA_HA)
      gm.append(gr['delta']);gcmrows.append(gr)
     row['gcm_delta_min']=min(gm);row['gcm_delta_max']=max(gm);row['gcm_delta_sd']=np.std(gm,ddof=1);row['gcm_negative']=sum(x<0 for x in gm);row['gcm_positive']=sum(x>0 for x in gm)
     assert np.isclose(np.mean(gm),row['delta'],atol=1e-8)
    summary.append(row)
 sm=pd.DataFrame(summary);sm.to_csv(OUT/'country_global_comparisons.csv',index=False);pd.DataFrame(gcmrows).to_csv(OUT/'country_global_gcm.csv',index=False);pd.concat(locrows).to_csv(OUT/'country_cell_comparisons.csv.gz',index=False)
 if not include_filter_audit:
  return
 ps=pd.concat([pd.read_csv(p/'planting_summary.csv') for p in folders]);ps['delta']=ps.future-ps.baseline;ps.to_csv(OUT/'planting_sensitivity.csv',index=False)
 an=pd.concat([pd.read_csv(p/'annual.csv.gz') for p in folders]);ar=[]
 for v in METRICS:
  for key,g in an.groupby(['COUNTRY','PERIOD','SCENARIO','SIM_YEAR']):
   ar.append(dict(COUNTRY=key[0],PERIOD=key[1],SCENARIO=key[2],SIM_YEAR=key[3],metric=v,mean=g[v].mean() if g[v].notna().sum()==25 else np.nan,n_members=g[v].notna().sum(),minimum_member_area_ha=g[v+'_area'].min(),maximum_member_area_ha=g[v+'_area'].max()))
 pd.DataFrame(ar).to_csv(OUT/'annual_country_series.csv',index=False)
 ex=pd.concat([pd.read_csv(p/'exclusion_counts.csv.gz') for p in folders])
 ex.to_csv(OUT/'exclusions_by_country_cell_member.csv.gz',index=False)
 vals=['input_rows','zero_Y0','negative_loss','zero_and_negative_loss','other_invalid_yield','non_numeric_reported_loss','negative_reported_loss','reported_loss_only','excluded_rows','retained_rows']
 for label,keys in [('country',['COUNTRY']),('country_period_scenario',['COUNTRY','PERIOD','SCENARIO']),('global_period_scenario',['PERIOD','SCENARIO'])]:
  counts=ex.groupby(keys)[vals].sum().reset_index();counts['excluded_pct']=100*counts.excluded_rows/counts.input_rows
  counts.to_csv(OUT/('exclusions_'+label+'.csv'),index=False)
 (OUT/'audit.json').write_text(json.dumps(dict(filter_rule='finite Y0>0; finite 0<=YASR<=Y0; stored LOSS_KG_HA and LOSS_PCT finite and nonnegative; otherwise remove complete row from all metrics',rows_before_filter=sum(a['rows_before_filter'] for a in audits),excluded_rows=sum(a['excluded_rows'] for a in audits),rows=sum(a['rows'] for a in audits),countries=len(audits),area_ha=float(meta.HARVEST_AREA_HA.sum()),country_cells=len(meta),countries_audit=audits),indent=2))
 print('CONSOLIDATION COMPLETE',flush=True)
