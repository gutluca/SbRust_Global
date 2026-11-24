"""Exercise actual native validators/master builder and scheduler integration."""
import copy
import csv
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK / 'scripts'))
import common as c
import controller


def engine_path():
    if os.environ.get('SBRUST_ENGINE_ROOT'):
        return Path(os.environ['SBRUST_ENGINE_ROOT']) / 'scripts'
    remote = c.BASE / 'releases/world_disease_only_20261022_v1/scripts'
    return remote if remote.is_dir() else PACK.parents[1] / 'production_reference'


class NativeFlow(unittest.TestCase):
    def test_full_country_750_native_shards_to_4600_row_combined_master(self):
        engine = engine_path()
        sys.path.insert(0,str(engine))
        from export_analysis_table import SOURCE_IDENTITY
        from build_master_summary import MASTER_FIELDS
        from test_contracts import rows
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); gen=root/'gen'; result=root/'results/Brazil'; ready=gen/'Brazil/production'
            configs=ready/'annual_configs'; configs.mkdir(parents=True)
            with (ready/'gm_frozen.csv').open('w',newline='') as stream:
                writer=csv.writer(stream);writer.writerow(['CELL_ID','GM']);writer.writerow(['123','7'])
            c.save(gen/'Brazil/project.json', {'experiment':{'country':'Brazil','periods':[
                {'name':'baseline','start_year':1981,'end_year':2014},
                {'name':'future_near','start_year':2020,'end_year':2039},
                {'name':'future_mid','start_year':2040,'end_year':2069}]}})
            old=root/'old.csv.gz'
            with gzip.open(old,'wt',newline='',encoding='utf-8') as stream:
                writer=csv.DictWriter(stream,fieldnames=MASTER_FIELDS,delimiter=';')
                writer.writeheader()
                for record in rows(True): writer.writerow(record)
            manifests=[]
            for i,(year,ssp,gcm) in enumerate(sorted(c.matrix())):
                name=f'c{i:03d}';path=configs/(name+'.json');manifests.append(path)
                forcing={'scenario':ssp,'sha256':'c'*64}
                c.save(path,{'schema_version':2,'name':name,'pipeline':{'country':'Brazil',
                    'disease_design':'single_calibrated_parameter_set','co2_forcing':forcing}})
                shard=result/'annual/tasks'/name;shard.mkdir(parents=True)
                c.save(shard/'audit.json',{'schema_version':2,'status':'ok','config_name':name,
                    'source_config_sha256':c.sha(path),'disease_design':'single_calibrated_parameter_set',
                    'site_count':1,'result_rows':{'disease':1,'control':1},'files':{'WARNING.OUT':{'observed':0}}})
                c.save(shard/'co2_audit.json',{'status':'ok','forcing_sha256':'c'*64,'scenario':ssp})
                for kind in ('disease','control'):
                    record={field:'' for field in SOURCE_IDENTITY}
                    record.update(COUNTRY='Brazil',PERIOD='future_near' if year<2040 else 'future_mid',
                        SIM_YEAR=str(year),SCENARIO=ssp,GCM=gcm,CELL_ID='123',WEATHER_SOURCE_CELL_ID='123',
                        WEATHER_BORROWED='0',WEATHER_DISTANCE_KM='0',LATITUDE='-20',LONGITUDE='-50',
                        HARVEST_AREA='100',GM_WIN='7',N_PLANTING_DATES='5')
                    for plant in c.PLANTS:
                        record['PDAT_'+plant]=str(year)+'274'
                        suffix='_disease' if kind=='disease' else ''
                        record['MDAT'+suffix+'_'+plant]=str(year+1)+'050'
                        record['HWAH'+suffix+'_'+plant]='3000' if kind=='control' else '2800'
                        if kind=='disease':
                            record['NSPRAYS_'+plant]='0';record['DISEASE_ONSET_DAE_'+plant]='35'
                    with gzip.open(shard/f'summary_{kind}_Brazil.csv.gz','wt',newline='',encoding='utf-8') as stream:
                        writer=csv.DictWriter(stream,fieldnames=list(record));writer.writeheader();writer.writerow(record)
            (configs/'manifest.txt').write_text(''.join(str(p)+'\n' for p in manifests))
            c.save(gen/'state.json',{'output':str(gen),'results':str(root/'results'),'pack':str(PACK),
                'engine':str(engine.parent),'countries':[{'country':'Brazil','key':'Brazil',
                    'source_master':str(old),'source_master_sha256':c.sha(old)}]})
            environment=dict(os.environ,TMPDIR=str(root))
            process=subprocess.run([sys.executable,str(PACK/'scripts/merge.py'),str(gen/'state.json'),'Brazil'],
                capture_output=True,text=True,env=environment)
            self.assertEqual(process.returncode,0,process.stdout+'\n'+process.stderr)
            audit=c.load(result/'complete.json')
            self.assertEqual(audit['historical_rows_preserved'],850)
            self.assertEqual(audit['corrected_future_rows'],3750)
            self.assertEqual(audit['points'],1)
            with gzip.open(result/'summary_master_Brazil.csv.gz','rt') as stream:
                records=list(csv.DictReader(stream,delimiter=';'))
            self.assertEqual(len(records),4600)
            self.assertEqual(records[-1]['LOSS_KG_HA'],'200')
            self.assertEqual(records[-1]['MATURITY_GROUP'],'7')

    def test_native_master_matches_v5_zero_effect_and_zero_yield_policy(self):
        engine = engine_path()
        sys.path.insert(0, str(engine))
        from export_analysis_table import output_fields
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pairs = root / 'pairs'; pairs.mkdir()
            records = []
            for plant in c.PLANTS:
                record = dict(COUNTRY='Brazil',CELL_ID='123',WEATHER_SOURCE_CELL_ID='123',WEATHER_BORROWED='0',
                    WEATHER_DISTANCE_KM='0',LATITUDE='-20',LONGITUDE='-50',HARVEST_AREA_HA='100',MATURITY_GROUP='7',
                    PERIOD='future_mid',SIM_YEAR='2069',SCENARIO='ssp585',GCM='GFDL-ESM4',PLANTING_LABEL=plant,
                    PLANT_DATE='2069-10-01',MATURITY_DATE='2070-02-01',YIELD_KG_HA='0' if plant=='P01' else '3000',
                    TOTAL_SPRAYS='0',DISEASE_ONSET_DAE='')
                record['SIMULATION_ID'] = '|'.join(record[k] for k in ('COUNTRY','CELL_ID','PERIOD','SIM_YEAR','SCENARIO','GCM','PLANTING_LABEL'))
                records.append(record)
            paths = []
            for kind in ('disease','control'):
                path = pairs / (kind+'.csv.gz'); paths.append(path)
                with gzip.open(path,'wt',newline='',encoding='utf-8') as stream:
                    writer = csv.DictWriter(stream,fieldnames=output_fields(kind),delimiter=';',extrasaction='ignore')
                    writer.writeheader(); writer.writerows(records)
            def call(mode, *args):
                return subprocess.run([sys.executable,str(PACK/'scripts/native_finalize.py'),str(engine),mode,*map(str,args)],
                    capture_output=True,text=True)
            validation = call('validate',*paths,root/'validation.json')
            self.assertEqual(validation.returncode,0,validation.stderr)
            self.assertEqual(c.load(root/'validation.json')['disease_yield_lower_than_control'],0)
            c.save(root/'project.json',{'experiment':{'country':'Brazil','periods':[{'name':'future_mid','start_year':2040,'end_year':2069}]}})
            master = call('master',*paths,root/'master.csv.gz',root/'project.json',root/'master_audit.json')
            self.assertEqual(master.returncode,0,master.stderr)
            with gzip.open(root/'master.csv.gz','rt') as stream:
                actual = list(csv.DictReader(stream,delimiter=';'))
            self.assertEqual(len(actual),5)
            self.assertEqual(actual[0]['YIELD_NO_DISEASE_KG_HA'],'0')
            self.assertEqual(actual[0]['PERIOD'],'2040-2069')
            self.assertEqual(actual[1]['LOSS_PCT'],'0')
            # An identity mismatch must still fail native pairing validation.
            records[0]['WEATHER_SOURCE_CELL_ID']='999'
            with gzip.open(paths[1],'wt',newline='',encoding='utf-8') as stream:
                writer=csv.DictWriter(stream,fieldnames=output_fields('control'),delimiter=';',extrasaction='ignore')
                writer.writeheader();writer.writerows(records)
            mismatch = call('validate',*paths,root/'broken.json')
            self.assertNotEqual(mismatch.returncode,0)
            self.assertIn('WEATHER_SOURCE_CELL_ID',mismatch.stderr)

    def test_new_dispatch_stages_complete_future_only_and_global_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); ready=root/'Country/production/annual_configs';ready.mkdir(parents=True)
            (ready/'manifest.txt').write_text(''.join(f'/config_{i}.json\n' for i in range(750)))
            state={'engine':str(engine_path().parent),'output':str(root),'results':str(root/'results'),
                   'budget_cpus':{'wpavan':89,'wpavan-b':810},'max_active_countries':6,'turn':0}
            country={'country':'Country','key':'Country','stage':1,'status':'active','history':{},'batches':[]}
            state['countries']=[country]
            legacy=controller.configure(state)
            self.assertEqual(legacy.STAGES,('prepare','annual_first','annual_rest','master'))
            first=legacy.specification(state,country)[3]
            country['stage']=2
            rest=legacy.specification(state,country)[3]
            self.assertEqual(first+rest,list(range(750)))
            def submit(st,record,qos,indices,sp):
                record['batches'].append(dict(job='100',indices=indices,qos=qos,cpus=4,array=True,
                                              states={str(i):'PENDING' for i in indices}))
            with patch.object(legacy,'refresh'),patch.object(legacy,'submit',side_effect=submit):
                legacy.tick(state,root/'state.json')
                self.assertEqual(legacy.reservations(state),{'wpavan':88,'wpavan-b':808})
                all_indices=[i for b in country['batches'] for i in b['indices']]
                self.assertEqual(len(all_indices),224)
                self.assertEqual(len(set(all_indices)),224)
                # Pending jobs reserve the entire pool and cannot be duplicated.
                legacy.tick(state,root/'state.json')
                self.assertEqual(sum(len(b['indices']) for b in country['batches']),224)


if __name__=='__main__': unittest.main()
