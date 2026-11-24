import copy
import csv
from datetime import date,timedelta
import gzip
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

PACK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PACK/'scripts'))
import common as c
import control_index as idx
import prepare as prep
import campaign
import worker
import merge as merger
import world_onset_runtime as r
import scheduler_recovery as recovery
from co2_runtime import read_daily_weather,expected_co2,stage_co2

def master_rows(cells=('1','2')):
    for cell in cells:
        for group,(period,scenario,lo,hi) in enumerate(r.o.a.GROUPS):
            for year in range(lo,hi+1):
                for gcm in c.GCMS:
                    for plant in c.PLANTS:
                        yield dict(COUNTRY='India',CELL_ID=cell,SIM_YEAR=str(year),PERIOD=period,SCENARIO=scenario,
                            GCM=gcm,PLANTING_LABEL=plant,SIMULATION_ID=r.o.e.identity('India',cell,group,year,gcm,plant),
                            PLANT_DATE=f'{year}-01-01',DISEASE_ONSET_DAE='' if cell=='1' else '20',
                            YIELD_NO_DISEASE_KG_HA='0' if cell=='1' else '1234',MATURITY_GROUP='5',
                            WEATHER_SOURCE_CELL_ID=cell,LATITUDE='20',LONGITUDE='70',HARVEST_AREA_HA='10')

def write_master(path,rows):
    rows=iter(rows); first=next(rows)
    with gzip.open(path,'wt',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(first),delimiter=';');w.writeheader();w.writerow(first);w.writerows(rows)

def lower(row):
    return dict(country=row['COUNTRY'],cell_id=row['CELL_ID'],period=row['PERIOD'],scenario=row['SCENARIO'],
        simulation_year=row['SIM_YEAR'],gcm=row['GCM'],planting_label=row['PLANTING_LABEL'],simulation_id=row['SIMULATION_ID'],
        plant_date=row['PLANT_DATE'],maturity_date='-99',maturity_group=row['MATURITY_GROUP'],
        weather_source_cell_id=row['WEATHER_SOURCE_CELL_ID'],latitude=row['LATITUDE'],longitude=row['LONGITUDE'],
        harvest_area_ha=row['HARVEST_AREA_HA'])

def fixture_cfg(scenario='ssp585',year=2069):
    runs=[]
    for plant in c.PLANTS:
        for arm in ('disease','control'):
            raw='/old/GM5.gpkg'
            runs.append(dict(name=f'{arm}_{plant}_GM_5',planting_label=plant,dises='Y' if arm=='disease' else 'N',
                ingeno='990005',cname='M GROUP 5',zone_id='GM_5',sites='xy_from_vector::'+raw,
                wsta='vector::'+raw+'::WTH_CELLID',pfrst='vector::'+raw+'::PLANT_DOY::-1::0',startYear=year))
    return dict(name='source',workDir='/old/work',templateDir='/same/templates',weatherDir='/same/weather/'+scenario,
        ghr_root='/same/soils',dssat={'executable':'/same/model'},default_setup=dict(nyers=1,include=['/same/control.cde','/old/disease_parameters.txt'],irrig='N'),
        runs=runs,pipeline=dict(country='Brazil',annual=True,planting_labels=list(c.PLANTS),simulation_year=year,
            period='baseline' if scenario=='historical' else 'future_mid',scenario=scenario,gcm=c.GCMS[0],
            disease_parameters_snapshot='/old/disease_parameters.txt',calendar={'source':'/same/calendar/'+scenario,'year':year},
            co2_forcing={'scenario':scenario,'path':'/same/CO2048.WDA','sha256':'forcing','annual_ppm':{str(year):732.647888},'switch':'M'}))

class CompactControls(unittest.TestCase):
    def test_complete_raw_matrix_zero_yield_and_changed_historical_cell(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/'master.csv.gz'; write_master(source,master_rows())
            keep={cell:dict(LATITUDE='20',LONGITUDE='70',HARVEST_AREA_HA='10') for cell in ('1','2')}
            control=idx.export(source,c.sha(source),'India',keep,root/'index')
            self.assertEqual(control.meta['records'],9200)
            cfg=dict(pipeline=dict(period='baseline',scenario='historical',simulation_year=1981,gcm=c.GCMS[0]))
            records=control.controls(cfg,['1','2']); self.assertEqual(len(records),10)
            self.assertEqual(records['1','P01']['YIELD_NO_DISEASE_KG_HA'],0)
            self.assertEqual(control.get(lower(next(master_rows()))),(-1,None))
            old=list(master_rows())
            changed=next(v for v in old if v['CELL_ID']=='2' and v['SCENARIO']=='historical')
            changed['YIELD_NO_DISEASE_KG_HA']='1233'
            older=root/'old.csv.gz';write_master(older,old)
            self.assertEqual(idx.changed_historical(control,older,c.sha(older)),{'2'})
            old[-1]['SIMULATION_ID']='wrong'
            write_master(older,old)
            with self.assertRaises(ValueError): idx.export(older,c.sha(older),'India',keep,root/'bad')

    def test_duplicate_and_missing_simulations_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/'master.csv.gz'; rows=list(master_rows(('1',)))
            keep={'1':dict(LATITUDE='20',LONGITUDE='70',HARVEST_AREA_HA='10')}
            for data in (rows[:-1],[*rows,rows[0]]):
                write_master(source,data)
                with self.assertRaises(ValueError): idx.export(source,c.sha(source),'India',keep,root/'bad')

    def test_historical_reuse_is_exact_and_omits_only_dirty_cells(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); source=root/'master.csv.gz'; data=list(master_rows());write_master(source,data)
            keep={cell:dict(LATITUDE='20',LONGITUDE='70',HARVEST_AREA_HA='10') for cell in ('1','2')}
            control=idx.export(source,c.sha(source),'India',keep,root/'index'); schedules=root/'old_apps.csv.gz'
            cache=dict(start='1980-12-01',rain=[0.]*180,eligible=[0]*32+[1]*148)
            with gzip.open(schedules,'wt',encoding='utf-8',newline='') as f:
                w=csv.DictWriter(f,fieldnames=r.OUTPUT_FIELDS);w.writeheader()
                for raw in data:
                    if raw['SCENARIO']=='historical': w.writerow(r.public_record(r.decorate(lower(raw),cache,control)))
            reused=root/'reused.csv.gz'
            self.assertEqual(prep.reuse_history(control,{'2'},schedules,reused),850)
            with gzip.open(reused,'rt',newline='') as f:
                actual=list(csv.DictReader(f))
            self.assertEqual({row['cell_id'] for row in actual},{'1'})
            self.assertTrue(all(float(row['onset_ensemble2_r14_cap6'])==0 for row in actual))

class ScientificProtocol(unittest.TestCase):
    def test_identical_schedules_to_previous_approved_recount(self):
        rng=random.Random(113); start=date(2039,12,1)
        for gate in (None,date(2040,1,26),date(2041,1,1)):
            original=types.SimpleNamespace(get=lambda row,g=gate:(-1,None) if g is None else (20,g))
            for _ in range(12):
                raw=next(master_rows(('2',))); row=lower(raw); row.update(simulation_year='2040',plant_date='2040-01-01')
                cache=dict(simulation_id=row['simulation_id'],start=str(start),rain=[rng.choice([0,.5,.6,8,35]) for _ in range(220)],
                    eligible=[int(i>38 and rng.random()>.1) for i in range(220)])
                for (rule,residual),days in r.o.schedule(r.o.dsv_values(cache['rain']),cache['eligible']).items():
                    prefix=f'{rule.lower()}_r{residual}'
                    row[prefix+'_uncapped']=len(days);row[prefix+'_cap6']=min(6,len(days))
                    row[prefix+'_dates']=';'.join(str(start+timedelta(days=i)) for i in days)
                previous=r.o.recount(row,cache,original); new=r.decorate(dict(row),cache,original)
                for rule in ('dsv50','dsv80'):
                    for tail in ('uncapped','cap6','dates'):
                        key='onset_'+rule+'_r14_'+tail;self.assertEqual(previous[key],new[key])
        self.assertEqual(r.o.schedule([100.]*70,[0]*10+[1]*40+[0]*20,0)['DSV50',14],[10,25,40])

    def test_only_ymax_parameter_changes(self):
        with tempfile.TemporaryDirectory() as td:
            source=Path(td)/'source.txt'; target=Path(td)/'zero.txt'
            source.write_text('! untouched\n@VAR# FAV_THR YMAX\nD45382 13.97 85\n')
            prep.zero_parameters(source,target)
            self.assertEqual(target.read_text(),'! untouched\n@VAR# FAV_THR YMAX\nD45382 13.97 0.0\n')

    def test_future_clone_preserves_management_forcing_and_fixed_gms(self):
        source=fixture_cfg(); new=prep.clone(source,source,Path('/new/disease_parameters.txt'),'zero',Path('/same/templates'))
        self.assertEqual(len(new['runs']),5)
        self.assertTrue(all(v['dises']=='Y' for v in new['runs']))
        for key in ('weatherDir','ghr_root','dssat'):self.assertEqual(new[key],source[key])
        for key in ('calendar','co2_forcing'):self.assertEqual(new['pipeline'][key],source['pipeline'][key])
        self.assertEqual(new['runs'],[v for v in source['runs'] if v['dises']=='Y'])
        self.assertEqual(source['default_setup']['include'][-1],'/old/disease_parameters.txt')

    def test_historical_clone_changes_to_current_zone_but_keeps_old_calendar_weather(self):
        old=fixture_cfg('historical',1981); future=fixture_cfg()
        for run in future['runs']:
            run.update(sites='xy_from_vector::/new/GM7.gpkg',ingeno='990007',cname='M GROUP 7',zone_id='GM_7')
        forcing=dict(path='/frozen/CO2048.WDA',sha256='old',dated_ppm=[['1980-01-01',340.]])
        new=prep.clone(old,future,Path('/new/disease_parameters.txt'),'zero',Path('/same/templates'),forcing)
        self.assertEqual(new['weatherDir'],old['weatherDir']); self.assertEqual(new['pipeline']['calendar'],old['pipeline']['calendar'])
        self.assertTrue(all(v['ingeno']=='990007' and '/new/GM7.gpkg' in v['pfrst'] for v in new['runs']))
        self.assertEqual(new['pipeline']['co2_forcing']['dated_ppm'],forcing['dated_ppm'])

    def test_actual_daily_co2_catches_flat_default_cross_year_and_leap_day(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'Weather.OUT'
            path.write_text('@YEAR DOY PRED CO2D\n2020 366 2 415.2\n2021 1 1 417.7\n')
            self.assertEqual(read_daily_weather(path,{'2020':415.154968,'2021':417.67})['days'],2)
            path.write_text('@YEAR DOY PRED CO2D\n2069 1 2 425.4\n')
            with self.assertRaises(ValueError):read_daily_weather(path,{'2069':732.647888})
            path.write_text('@YEAR DOY PRED CO2D\n2020 60 2 415.2\n')
            self.assertEqual(read_daily_weather(path,{'2020':415.154968})['first_date'],'2020-02-29')
        self.assertEqual(expected_co2({'dated_ppm':[['1980-01-01',340.],['1981-01-01',342.]]},date(2070,1,1)),342.)

    def test_co2_is_staged_locally_and_rendered_switch_is_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); leaf=root/'work/disease_P01_GM_5/site';leaf.mkdir(parents=True)
            source=root/'CO2048.WDA'; source.write_text('frozen SSP forcing\n'); source_sha=c.sha(source)
            sbx=leaf/'SBG00001.SBX';text=('@E ODATE EDAY ERAD EMAX EMIN ERAIN ECO2 EDEW EWIND ENVNAME\n'
                ' 1 69001 A 0 A 0 A 0 A 0 A 0 A 0 A 0 A 0 global\n'
                '@N OPTIONS WATER NITRO SYMBI PHOSP POTAS DISES CHEM TILL CO2\n 1 OP Y N N N N Y N N M\n')
            sbx.write_text(text);cfg=dict(runs=[{'name':'disease_P01_GM_5'}],pipeline=dict(scenario='ssp585',
                co2_forcing=dict(path=str(source),sha256=source_sha,scenario='ssp585',switch='M')))
            self.assertEqual(stage_co2(cfg,root/'work'),1)
            self.assertEqual(c.sha(source),source_sha);self.assertEqual(c.sha(leaf/'CO2048.WDA'),source_sha)
            sbx.write_text(text.replace(' N M\n',' N D\n'))
            with self.assertRaises(ValueError): stage_co2(cfg,root/'work')

class OperationalRecovery(unittest.TestCase):
    def test_account_routing_journals_before_submit_and_configures_multiple_phases(self):
        state={'stages':['annual_rest'],'budget_cpus':{'wpavan':89,'hoogenboom':200,'wpavan-b':810},
               'code':str(PACK),'output':'/output','results':'/results','countries':[]}
        legacy,_=campaign.configure(state); legacy,_=campaign.configure(state)
        fn=legacy.submit; ns=fn.__globals__; saved=[]; commands=[]
        ns['specification']=lambda s,rec:('job.slurm',[],4,[0],24)
        ns['save_json']=lambda p,s:saved.append(copy.deepcopy(s))
        def submit(command,**kwargs):
            self.assertIsNone(saved[-1]['countries'][0]['batches'][-1]['job']);commands.append(command)
            return types.SimpleNamespace(returncode=0,stdout='123',stderr='')
        ns['subprocess']=types.SimpleNamespace(run=submit,TimeoutExpired=subprocess.TimeoutExpired)
        for qos,account in [('wpavan','wpavan'),('hoogenboom','hoogenboom'),('wpavan-b','wpavan')]:
            rec={'country':'India','key':'India','stage':0,'batches':[]};state['countries']=[rec]
            fn(state,rec,qos,[0],Path('/state'))
            self.assertIn('--account='+account,commands[-1]);self.assertIn('--time=24:00:00',commands[-1])
            self.assertEqual(rec['batches'][-1]['job'],'123')

    def test_accepted_submission_is_adopted_and_timeout_never_blindly_retried(self):
        batch=dict(job=None,name='unique',indices=[0],submission_started_utc='2026-11-19T04:00:00+00:00',
            submission_error='sbatch response timed out; acceptance unknown',submission_stdout='')
        state={'countries':[{'batches':[batch]}]}
        with patch.object(recovery,'lookup',return_value=None):self.assertFalse(recovery.reconcile(state,lambda s:None))
        self.assertEqual(state['countries'][0]['batches'],[batch])
        with patch.object(recovery,'lookup',return_value='456'):self.assertTrue(recovery.reconcile(state,lambda s:None))
        self.assertEqual(batch['job'],'456')

    def test_explicit_connect_rejection_requires_two_separated_checks(self):
        batch=dict(job=None,name='unique',indices=[0],submission_started_utc='2026-11-19T04:00:00+00:00',submission_stdout='',
            submission_error='sbatch: error: Batch job submission failed: Unable to contact slurm controller (connect failure)')
        state={'countries':[{'batches':[batch]}]}
        with patch.object(recovery,'lookup',return_value=None),patch.object(recovery.time,'time',return_value=1000):
            self.assertFalse(recovery.reconcile(state,lambda s:None))
        with patch.object(recovery,'lookup',return_value=None),patch.object(recovery.time,'time',return_value=1121):
            self.assertTrue(recovery.reconcile(state,lambda s:None))
        self.assertEqual(state['countries'][0]['batches'],[])
        self.assertEqual(len(state['countries'][0]['rejected_submissions']),1)

    def test_changed_published_cache_is_not_reused(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);data=root/'schedules.csv.gz';data.write_bytes(b'original')
            task={'signature':'same','cells':['1']}
            (root/'daily_cache.jsonl.gz').write_bytes(b'cache');(root/'sample_daily_outputs.tar.gz').write_bytes(b'sample')
            c.save(root/'co2_audit.json',dict(status='ok',checked_experiments=5,checked_daily_values=500))
            c.save(root/'complete.json',dict(status='ok',task_signature='same',policy=r.POLICY,points=1,rows=5,
                outputs={p.name:c.sha(p) for p in root.iterdir()}))
            self.assertTrue(worker.valid(root,task));data.write_bytes(b'changed')
            with self.assertRaises(ValueError):worker.valid(root,task)
            (root/'co2_audit.json').unlink()
            with self.assertRaises(FileNotFoundError):worker.valid(root,task,full=False)

    def test_authorities_cover_103_countries_and_have_only_hpg_data_paths(self):
        old=c.load(PACK/'inputs/old_sources.json');new=c.load(PACK/'inputs/expected_masters.json')
        self.assertEqual(len(old),103);self.assertEqual(set(old),set(new))
        self.assertEqual(sum(v['points'] for v in new.values()),16612)
        self.assertTrue(all(v['master'].startswith('/blue/') for v in old.values()))

    def test_previous_scientific_and_runtime_sources_are_hash_locked(self):
        provenance=c.load(PACK/'inputs/science_provenance.json')
        for name,record in provenance.items():
            self.assertEqual(c.sha(PACK/'scripts'/name),record['normalized_copy_sha256'])

class CountryPreparationIntegration(unittest.TestCase):
    def test_all_750_configs_baseline_reuse_pilots_and_final_complete_matrix(self):
        """Exercise the actual preparation/merge orchestration with tiny spatial data."""
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); pack=root/'pack'; source=root/'source'; generated=root/'generated'; results=root/'results'
            key='India'; rec=dict(country='India',key=key,site_count=2)
            current=root/'master.csv.gz'; data=list(master_rows());write_master(current,data)
            rec.update(current_master=str(current),current_master_sha256=c.sha(current),current_complete=str(root/'current_complete.json'))
            c.save(rec['current_complete'],dict(historical_rows_copied_verbatim=True,source_master_sha256=c.sha(current)))
            c.save(pack/'inputs/old_sources.json',{'India':dict(master=str(current),sha256=c.sha(current))})
            population=pack/'inputs/population.csv';population.parent.mkdir(parents=True,exist_ok=True)
            population.write_text('COUNTRY,CELL_ID,LATITUDE,LONGITUDE,HARVEST_AREA_HA\nIndia,1,20,70,10\nIndia,2,20,70,10\n')
            model=root/'dscsm048';model.write_bytes(b'frozen model fixture'); model_sha=c.sha(model)
            regional=root/'normal/disease_parameters.txt';regional.parent.mkdir()
            regional.write_text('@VAR# FAV_THR YMAX\nD45382 13.97 85\n')
            rec.update(disease_input=str(regional),disease_input_sha256=c.sha(regional))
            template=root/'engine/templates/SBG00001.SBX';template.parent.mkdir(parents=True);template.write_text('unchanged template')
            sites=root/'zone.gpkg';sites.write_bytes(b'frozen selected GM geometry fixture')
            calendar=root/'calendar.nc';calendar.write_bytes(b'frozen annual calendar fixture')
            co2=root/'CO2048.WDA';co2.write_text('frozen CO2 forcing fixture')
            source_ready=source/key/'production'; config_dir=source_ready/'annual_configs';config_dir.mkdir(parents=True)
            config_hashes={}; manifest=[]
            for year in range(2020,2070):
                for scenario in ('ssp126','ssp370','ssp585'):
                    for gcm in c.GCMS:
                        cfg=fixture_cfg(scenario,year); cfg.update(name=f'{scenario}_{gcm}_{year}',dssat={'executable':str(model)})
                        cfg['default_setup']['include']=[str(regional)]
                        cfg['pipeline'].update(country='India',gcm=gcm,disease_parameters_snapshot=str(regional),
                            period='future_near' if year<=2039 else 'future_mid',calendar=dict(source=str(calendar),year=year),
                            co2_forcing=dict(path=str(co2),sha256=c.sha(co2),scenario=scenario,switch='M',annual_ppm={str(year):500.}))
                        for run in cfg['runs']:
                            raw=run['sites'].split('::',1)[1]
                            run.update(c.replace(run,{raw:str(sites)}))
                        path=config_dir/(cfg['name']+'.json'); c.save(path,cfg);config_hashes[str(path)]=c.sha(path);manifest.append(str(path))
            (config_dir/'manifest.txt').write_text('\n'.join(manifest)+'\n')
            c.save(source_ready/'reuse_audit.json',dict(config_sha256=config_hashes,sites_sha256={str(sites):c.sha(sites)}))
            oldroot=root/'old';oldfolder=oldroot/'countries'/key;oldfolder.mkdir(parents=True)
            oldapp=oldfolder/'summary_applications_India.csv.gz'
            class NoOnset:
                def get(self,row): return (-1,None) if row['cell_id']=='1' else (20,date.fromisoformat(row['plant_date'])+timedelta(days=25))
            cache=dict(start='1980-12-01',rain=[0.]*180,eligible=[0]*32+[1]*148)
            with gzip.open(oldapp,'wt',encoding='utf-8',newline='') as f:
                w=csv.DictWriter(f,fieldnames=r.OUTPUT_FIELDS);w.writeheader()
                for row in data:w.writerow(r.public_record(r.decorate(lower(row),cache,NoOnset())))
            c.save(oldfolder/'complete.json',dict(country='India',status='completed',policy=r.POLICY,master=str(oldapp),sha256=c.sha(oldapp)))
            state=dict(output=str(generated),results=str(results),source_output=str(source),source_pack=str(pack),
                engine=str(root/'engine'),reference_co2={},countries=[rec])
            statepath=root/'state.json';c.save(statepath,state)
            with patch.object(c,'PACK',pack),patch.object(c,'OLD',oldroot),patch.object(c,'EXE_SHA',model_sha),patch.object(c,'compute_only'):
                prep.prepare(statepath,key)
            folder=c.ready(state,rec);info=prep.verify_prepared(folder)
            self.assertEqual(info['historical_reused_rows'],1700);self.assertEqual(info['historical_rerun_rows'],0)
            self.assertEqual(len(info['configs']),750);self.assertEqual(info['bundles'],188);self.assertEqual(info['pilots'],1)
            self.assertEqual(sum(len(batch) for batch in c.load(folder/'bundles.json')),750)
            # The merge consumes independently supplied annual schedules, not the
            # code that wrote the preparation index; a complete 9200-key matrix is mandatory.
            index=idx.Index(folder/'index')
            for batch in c.load(folder/'bundles.json'):
                for task in batch:
                    cfg=c.load(task['config']);controls=index.controls(cfg,task['cells'])
                    dest=c.result(state,rec)/'tasks'/task['name'];dest.mkdir(parents=True)
                    with gzip.open(dest/'schedules.csv.gz','wt',encoding='utf-8',newline='') as f:
                        w=csv.DictWriter(f,fieldnames=r.OUTPUT_FIELDS);w.writeheader()
                        for reference in controls.values():
                            row=lower(reference);w.writerow(r.public_record(r.decorate(row,cache,index)))
                    (dest/'daily_cache.jsonl.gz').write_bytes(b'fixture cache')
                    (dest/'sample_daily_outputs.tar.gz').write_bytes(b'fixture sample')
                    c.save(dest/'co2_audit.json',dict(status='ok',checked_experiments=len(controls),checked_daily_values=len(controls)*200))
                    c.save(dest/'complete.json',dict(status='ok',task_signature=task['signature'],policy=r.POLICY,
                        points=len(task['cells']),rows=len(controls),outputs={p.name:c.sha(p) for p in dest.iterdir()}))
            with patch.object(c,'compute_only'),patch.dict('os.environ',{'SLURM_JOB_ID':'fixture'}):merger.merge(statepath,key)
            done=c.load(c.result(state,rec)/'complete.json')
            self.assertEqual(done['rows'],9200);self.assertEqual(done['historical_reused_rows'],1700)
            campaign.prepare_analysis(state)
            plan=c.load(results/'analysis/plan.json');pop=c.load(results/'analysis/population.json')
            self.assertEqual(len(plan['countries']),1)
            self.assertEqual(set(pop['retained']['India']['1']),{'latitude','longitude','harvest_area_ha'})

if __name__=='__main__':unittest.main()
