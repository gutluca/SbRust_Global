"""Prepare exact current controls, audited historical reuse and healthy-crop tasks."""
import copy
import csv
import gzip
import math
from pathlib import Path
import common as c
from control_index import export, Index, changed_historical
import world_onset_runtime as r

def zero_parameters(source,target):
    text=Path(source).read_text(); lines=text.splitlines(keepends=True)
    headers=[s.split() for s in lines if s.split() and s.split()[0]=='@VAR#']
    records=[i for i,s in enumerate(lines) if s.split() and s.split()[0]=='D45382']
    if len(headers)!=1 or len(records)!=1: raise ValueError('Ambiguous regional parameter record')
    i=records[0]; tokens=lines[i].split(); header=headers[0]
    if len(tokens)!=len(header) or float(tokens[header.index('YMAX')])<=0: raise ValueError('Invalid production YMAX')
    before=tokens[:]; tokens[header.index('YMAX')]='0.0'
    lines[i]=' '.join(tokens)+'\n'; output=''.join(lines)
    after=output.splitlines()[i].split()
    if any(a!=b for k,(a,b) in enumerate(zip(before,after)) if k!=header.index('YMAX')):
        raise ValueError('Only YMAX may change')
    target.parent.mkdir(parents=True,exist_ok=True); target.write_text(output,encoding='utf-8',newline='\n')

def clone(source, future, zero, zero_sha, template, historical_forcing=None):
    """Copy management literally; historical jobs use the current frozen GM zones."""
    cfg=copy.deepcopy(source); p=cfg['pipeline']
    if not p.get('annual') or cfg['default_setup']['nyers']!=1: raise ValueError('Annual source required')
    if tuple(p['planting_labels'])!=c.PLANTS: raise ValueError('Five planting alternatives required')
    if cfg['dssat']['executable']!=future['dssat']['executable']: raise ValueError('DSSAT source differs')
    includes=cfg['default_setup']['include']
    old=p.get('disease_parameters_snapshot')
    if old not in includes:
        matches=[s for s in includes if Path(s).name=='disease_parameters.txt']
        if len(matches)!=1: raise ValueError('Missing unambiguous regional include')
        old=matches[0]
    cfg=c.replace(cfg,{old:str(zero)}); p=cfg['pipeline']
    disease=[v for v in cfg['runs'] if v['name'].startswith('disease_') and v.get('dises')=='Y']
    if not disease: raise ValueError('Missing disease arm')
    if historical_forcing is not None:
        prototypes={}; contracts={}
        for run in disease:
            plant=run['planting_label']; raw=run.get('sites',cfg['default_setup'].get('sites',''))
            if not raw.startswith('xy_from_vector::'): raise ValueError('Expected vector sites')
            normalized=c.replace(copy.deepcopy(run),{raw.split('::',1)[1]:'<ZONE>'})
            for key in ('name','sites','ingeno','cname','zone_id'): normalized.pop(key,None)
            if plant in contracts and contracts[plant]!=normalized: raise ValueError('Management differs between original GM zones')
            contracts[plant]=normalized; prototypes.setdefault(plant,run)
        if set(prototypes)!=set(c.PLANTS): raise ValueError('Missing historical planting prototype')
        zones={v['zone_id']:v for v in future['runs'] if v['name'].startswith('disease_') and v['planting_label']=='P01'}
        disease=[]
        for zone,selected in zones.items():
            for plant in c.PLANTS:
                proto=prototypes[plant]; raw=proto.get('sites',cfg['default_setup'].get('sites','')).split('::',1)[1]
                new=c.replace(copy.deepcopy(proto),{raw:selected['sites'].split('::',1)[1]})
                new.update(name='disease_'+plant+'_'+zone,sites=selected['sites'],ingeno=selected['ingeno'],
                           cname=selected['cname'],zone_id=zone)
                disease.append(new)
        p['co2_forcing']=dict(historical_forcing,scenario='historical',switch='M')
    cfg['runs']=disease; cfg['templateDir']=str(template)
    p.update(disease_parameters_snapshot=str(zero),disease_parameters_source=str(zero),disease_parameters_sha256=zero_sha,
             treatments_per_site=5,disease_treatments_per_site=5,
             applications_auxiliary={'DISES':'Y','YMAX':0,'source_normal_onset':True})
    # The production disease-only reuse metadata refers to obsolete control shards.
    # This campaign checks the unfiltered current master through its exact-key index.
    p.pop('disease_only_reuse',None)
    return cfg

def old_applications(country,key):
    for folder in (c.OLD/'countries'/key,c.OLD/'countries'/c.safe_name(country),c.OLD/c.safe_name(country)):
        if not (folder/'complete.json').is_file(): continue
        info=c.load(folder/'complete.json')
        path=Path(info.get('master',folder/('summary_applications_'+c.safe_name(country)+'.csv.gz')))
        if not path.is_file(): continue
        if info.get('status')!='completed' or info['country']!=country or info['policy']!=r.POLICY:
            raise ValueError('Old country not completed with the identical policy')
        c.verify(path,info['sha256']); return path,info
    raise FileNotFoundError('Old applications master missing for '+country)

def reuse_history(index,dirty,old_path,target):
    seen=bytearray(len(index.cells)*850); count=0
    with gzip.open(old_path,'rt',encoding='utf-8-sig',newline='') as src,gzip.open(target,'wt',encoding='utf-8',newline='',compresslevel=1) as dst:
        writer=csv.DictWriter(dst,fieldnames=r.OUTPUT_FIELDS); writer.writeheader()
        for row in csv.DictReader(src):
            if row['scenario']!='historical': continue
            cell=row['cell_id']
            if cell in dirty: continue
            plant,dae,y0=index.record(row)
            index.get(row)
            gm,weather,lat,lon,area=index.meta['static'][cell]
            if row['maturity_group']!=gm or row['weather_source_cell_id']!=weather:
                raise ValueError('Old applications GM/weather mismatch')
            if r.o.onset_value(row['disease_onset_dae'])!=dae: raise ValueError('Old applications onset mismatch')
            expected=r.o.onset_date(r.date.fromordinal(plant),dae)
            if row['onset_date_approx']!=('' if expected is None else expected.isoformat()): raise ValueError('Old onset calendar differs')
            for field,value in [('latitude',lat),('longitude',lon),('harvest_area_ha',area)]:
                if not math.isclose(float(row[field]),float(value),rel_tol=1e-8,abs_tol=1e-4): raise ValueError('Old applications population mismatch')
            year=int(row['simulation_year']); slot=r.o.e.slot(0,year,row['gcm'],row['planting_label'])
            j=index.cells[cell]*850+slot
            if seen[j]: raise ValueError('Duplicate old application history')
            seen[j]=1
            if int(row['emergence_lag_assumed_days'])!=5 or int(row['buffer_days'])!=14: raise ValueError('Old policy differs')
            writer.writerow(r.public_record(row)); count+=1
    expected=(len(index.cells)-len(dirty))*850
    if count!=expected: raise ValueError('Incomplete reused historical matrix')
    return count

def task(cfg_path,cfg_sha,rec,cells,name,index_info,files,pilot=False):
    return dict(country=rec['country'],key=rec['key'],config=str(cfg_path),config_sha256=cfg_sha,
                cells=sorted(cells,key=int),name=name,index_metadata_sha256=c.sha(index_info),
                inputs=files,pilot=pilot)

def prepare(state_path,key):
    c.compute_only(); state=c.load(state_path); rec=c.country(state,key); folder=c.ready(state,rec)
    if (folder/'complete.json').exists():
        verify_prepared(folder); print('REUSED_PREPARATION',key,flush=True); return
    folder.mkdir(parents=True,exist_ok=True)
    c.verify(rec['current_master'],rec['current_master_sha256'])
    with (Path(state['source_pack'])/'inputs/population.csv').open(newline='',encoding='utf-8') as f:
        keep={row['CELL_ID']:row for row in csv.DictReader(f) if row['COUNTRY']==rec['country']}
    if len(keep)!=rec['site_count']: raise ValueError('Point count differs')
    index=export(rec['current_master'],rec['current_master_sha256'],rec['country'],keep,folder/'index')
    old=c.load(c.PACK/'inputs/old_sources.json')[rec['country']]
    # Same archived source + verified verbatim baseline is a direct reuse proof.
    receipt=c.load(rec['current_complete'])
    if (receipt.get('historical_rows_copied_verbatim') is True and receipt['source_master_sha256']==old['sha256']):
        c.verify(old['master'],old['sha256']); dirty=set()
    else: dirty=changed_historical(index,old['master'],old['sha256'])
    old_path,old_info=old_applications(rec['country'],key)
    reused=reuse_history(index,dirty,old_path,folder/'historical_reused.csv.gz')
    c.verify(rec['disease_input'],rec['disease_input_sha256'])
    zero=folder/'parameters/disease_parameters.txt'; zero_parameters(rec['disease_input'],zero); zero_sha=c.sha(zero)
    source_ready=Path(state['source_output'])/key/'production'
    audit=c.load(source_ready/'reuse_audit.json')
    paths=[Path(s) for s in (source_ready/'annual_configs/manifest.txt').read_text().splitlines() if s.strip()]
    if len(paths)!=750: raise ValueError('Corrected future manifest incomplete')
    future=c.load(paths[0]); template=Path(state['engine'])/'templates'
    c.verify(future['dssat']['executable'],c.EXE_SHA)
    checked={}
    def digest_once(path):
        path=str(path)
        if path not in checked: checked[path]=c.sha(path)
        return checked[path]
    cfg_dir=folder/'configs'; cfg_dir.mkdir(exist_ok=True)
    tasks=[]; pilots=[]; config_hashes={}; input_hashes={}; climates=set()
    def add(path,source,historical=False):
        p=source['pipeline']; climate=(p['scenario'],p['gcm'],int(p['simulation_year']))
        if climate in climates: raise ValueError('Duplicate climate config')
        climates.add(climate)
        cfg=clone(source,future,zero,zero_sha,template,state['reference_co2'] if historical else None)
        cfg['name']='app_'+key+'_'+p['scenario']+'_'+p['gcm']+'_y'+str(p['simulation_year'])
        cfg['workDir']=str(folder/'never_execute_on_blue'/cfg['name'])
        dest=cfg_dir/(cfg['name']+'.json'); c.save(dest,cfg); cfg_sha=c.sha(dest); config_hashes[str(dest)]=cfg_sha
        files={str(path):digest_once(path),str(zero):zero_sha,str(template/'SBG00001.SBX'):digest_once(template/'SBG00001.SBX')}
        for run in cfg['runs']:
            site=run['sites'].split('::',1)[1]
            if audit['sites_sha256'].get(site)!=digest_once(site): raise ValueError('Current selected-GM sites changed')
            files[site]=audit['sites_sha256'][site]
        calendar=p.get('calendar',{}).get('source')
        if calendar: files[calendar]=digest_once(calendar)
        lib=p.get('cultivar_library')
        if lib:
            if digest_once(lib)!=p['cultivar_library_sha256']: raise ValueError('Cultivar library changed')
            files[lib]=p['cultivar_library_sha256']
        forcing=cfg['pipeline']['co2_forcing']; files[forcing['path']]=forcing['sha256']; input_hashes.update(files)
        cells=sorted(dirty if historical else keep,key=int)
        shards=max(1,math.ceil(len(cells)/512))
        for shard in range(shards):
            selected=cells[len(cells)*shard//shards:len(cells)*(shard+1)//shards]
            tasks.append(task(dest,cfg_sha,rec,selected,cfg['name']+f'__part{shard:03d}',folder/'index/metadata.json',files))
        core=rec['country'] in c.CORE
        if (not historical and ((core and int(p['simulation_year']) in (2020,2069)) or (not core and len(pilots)==0))) or (historical and len([t for t in pilots if t.get('historical')])==0):
            # One deterministic retained cell per current GM zone, including extremes.
            by_gm={}
            for cell in cells: by_gm.setdefault(index.meta['static'][cell][0],cell)
            sample=list(by_gm.values())
            pil=task(dest,cfg_sha,rec,sample,'pilot_'+cfg['name'],folder/'index/metadata.json',files,True)
            pil['historical']=historical; pilots.append(pil)
    for path in paths:
        c.verify(path,audit['config_sha256'][str(path)]); add(path,c.load(path))
    if len(climates)!=750: raise ValueError('Invalid future climate matrix')
    if dirty:
        historical=[]
        for line in Path(rec['original']['manifest']).read_text().splitlines():
            if not line.strip(): continue
            path=Path(line.strip()); source=c.load(path)
            if source['pipeline']['scenario']!='historical': continue
            prior=c.load(Path(rec['original']['annual'])/'tasks'/source['name']/'audit.json')
            if prior['status']!='ok': raise ValueError('Historical source execution was not successful')
            c.verify(path,prior['source_config_sha256']); historical.append((path,source))
        if len(historical)!=170: raise ValueError('Expected 170 original historical configurations')
        for path,source in historical: add(path,source,True)
    if rec['country'] in c.CORE and len([t for t in pilots if not t['historical']])!=30:
        raise ValueError('Core pilot must cover 2 years x 3 SSPs x 5 GCMs')
    bundles=[tasks[i:i+4] for i in range(0,len(tasks),4)]
    for collection in (bundles,[[t] for t in pilots]):
        for bundle in collection:
            for t in bundle: t['signature']=c.signature(t)
    c.save(folder/'bundles.json',bundles); c.save(folder/'pilots.json',[[t] for t in pilots])
    c.save(folder/'complete.json',dict(status='ok',country=rec['country'],key=key,points=len(keep),
        configs=config_hashes,inputs=input_hashes,index_metadata_sha256=c.sha(folder/'index/metadata.json'),
        old_source=old,old_applications=str(old_path),old_applications_sha256=old_info['sha256'],
        historical_dirty_cells=sorted(dirty,key=int),historical_reused_rows=reused,historical_rerun_rows=len(dirty)*850,
        future_rows=len(keep)*3750,bundles=len(bundles),pilots=len(pilots),policy=r.POLICY,
        outputs={name:c.sha(folder/name) for name in ('bundles.json','pilots.json','historical_reused.csv.gz')}))
    print('PREPARED',rec['country'],'reuse historical rows',reused,'rerun historical cells',len(dirty),'bundles',len(bundles),'pilots',len(pilots),flush=True)

def verify_prepared(folder):
    info=c.load(folder/'complete.json')
    if info['status']!='ok' or info['policy']!=r.POLICY: raise ValueError('Invalid preparation receipt')
    for name,sha in info['outputs'].items(): c.verify(folder/name,sha)
    c.verify(folder/'index/metadata.json',info['index_metadata_sha256'])
    return info
