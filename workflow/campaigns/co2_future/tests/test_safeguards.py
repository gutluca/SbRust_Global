import copy
import csv
from datetime import date
import gzip
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PACK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PACK/'scripts'))
import common as c
from co2_runtime import dated_co2,expected_co2,read_daily_weather
from contracts import compare_science
from replay import compare_reference
import launch
from test_contracts import source_config


class ScientificParity(unittest.TestCase):
    def test_only_output_paths_fixed_gm_zone_paths_and_co2_can_change(self):
        source=source_config(PACK/'model')
        zones=[dict(gm='7',path=PACK/'selected.gpkg',count=3,ingeno='990007',cname='M GROUP 7')]
        state=dict(pack=str(PACK),executable=str(PACK/'model'),output='/new/out',results='/new/results')
        new=c.clone_config(source,zones,state,{'key':'Brazil'},PACK,'/new/disease_parameters.txt','a'*64,'/new/template')
        self.assertTrue(compare_science(source,new))
        disease_only=copy.deepcopy(source)
        disease_only['runs']=[r for r in disease_only['runs'] if r['dises']=='Y']
        self.assertTrue(compare_science(disease_only,new,allow_missing_control=True))
        for mutate in (
            lambda x:x['default_setup'].update({'icsw%':50}),
            lambda x:x['default_setup'].update(sdate='date_offset::$pfrst::-30'),
            lambda x:x['pipeline']['calendar'].update(year=2068),
            lambda x:x.update(weatherDir='/wrong/weather'),
            lambda x:x['dssat'].update(executable='/wrong/model'),
            lambda x:x['runs'][0].update(pfrst='changed planting'),
        ):
            bad=copy.deepcopy(new);mutate(bad)
            with self.assertRaises(ValueError):compare_science(source,bad)

    def test_original_monthly_M_step_and_terminal_hold_are_exact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'CO2048.WDA'
            path.write_text('@CO2BAS\n380.\n@ YEAR   DOY    CO2\n 2019 350 411.0\n 2020 15 412.0\n 2020 45 -99.99\n 2020 75 414.0\n 2024 289 425.37\n')
            series={'dated_ppm':dated_co2(path)}
            self.assertEqual(expected_co2(series,date(2020,1,14)),411.)
            self.assertEqual(expected_co2(series,date(2020,1,15)),412.)
            self.assertEqual(expected_co2(series,date(2020,2,20)),412.)
            self.assertEqual(expected_co2(series,date(2069,12,31)),425.37)
            weather=Path(tmp)/'Weather.OUT'
            weather.write_text('@YEAR DOY DAS PRED CO2D\n 2020 014 1 0 411.0\n 2020 015 2 0 412.0\n')
            self.assertEqual(read_daily_weather(weather,series)['days'],2)

    def test_published_replay_requires_exact_yields_and_same_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);reference=root/'reference.gz';actual=root/'actual.gz'
            fields=['CELL_ID','PLANTING_LABEL','SIM_YEAR','SCENARIO','GCM','YIELD_WITH_DISEASE_KG_HA','YIELD_NO_DISEASE_KG_HA','PLANT_DATE']
            records=[dict(CELL_ID='123',PLANTING_LABEL=p,SIM_YEAR='2069',SCENARIO='ssp585',GCM='GFDL-ESM4',
                YIELD_WITH_DISEASE_KG_HA='2800',YIELD_NO_DISEASE_KG_HA='3000',PLANT_DATE='2069-10-01') for p in c.PLANTS]
            def write(path,data):
                with gzip.open(path,'wt',newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=fields,delimiter=';');writer.writeheader();writer.writerows(data)
            config={'pipeline':dict(simulation_year=2069,scenario='ssp585',gcm='GFDL-ESM4',co2_rerun={'expected_points':1})}
            write(reference,records);write(actual,records)
            self.assertEqual(compare_reference(reference,actual,config)['matched_pairs'],5)
            changed=copy.deepcopy(records);changed[0]['YIELD_NO_DISEASE_KG_HA']='3001';write(actual,changed)
            with self.assertRaises(ValueError):compare_reference(reference,actual,config)
            changed=copy.deepcopy(records);changed[0]['PLANT_DATE']='2069-10-02';write(actual,changed)
            with self.assertRaises(ValueError):compare_reference(reference,actual,config)


class NodeExclusion(unittest.TestCase):
    def test_annual_retry_policy_matches_original_and_never_repeats_success(self):
        from test_native_flow import engine_path
        import controller
        legacy=controller.configure({'engine':str(engine_path().parent)})
        country={'stage':c.STAGES.index('annual_rest'),'batches':[{'job':'100','qos':'wpavan','cpus':4,
            'indices':[1,2,3],'states':{'1':'COMPLETED','2':'FAILED','3':'RUNNING'},'array':True}]}
        success,active,exhausted,available=legacy.task_status(country,[1,2,3,4])
        self.assertEqual((success,active,exhausted,available),({1},{3},set(),[2,4]))
        self.assertEqual(legacy.ATTEMPT_LIMITS['annual_rest'],2)
        self.assertEqual(os.environ['SBATCH_EXCLUDE'],c.EXCLUDED_NODES)

    def test_all_batch_scripts_exclude_both_known_failure_nodes(self):
        scripts=list((PACK/'slurm').glob('*.slurm'))
        self.assertEqual(len(scripts),8)
        for path in scripts:
            self.assertIn('#SBATCH --exclude='+c.EXCLUDED_NODES,path.read_text(),str(path))
        for node in c.BAD_NODES:
            with self.assertRaises(RuntimeError):c.assert_allowed_node(node)
            with self.assertRaises(RuntimeError):c.assert_allowed_node(node+'.rc.ufl.edu')
        c.assert_allowed_node('c0706a-s24')

    def test_bootstrap_preflight_pilot_gate_and_controller_submit_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal={'jobs':{}}
            def sbatch(command,**kwargs):
                self.assertIn('--exclude='+c.EXCLUDED_NODES,command)
                saved=c.load(Path(tmp)/'journal.json')
                self.assertTrue(any(entry['job'] is None for entry in saved['jobs'].values()))
                return SimpleNamespace(returncode=0,stdout='12345\n',stderr='')
            with patch.object(launch,'JOURNAL',Path(tmp)/'journal.json'),patch.object(launch.subprocess,'run',side_effect=sbatch):
                for label in ('bootstrap','preflight','pilot','gate','controller'):
                    launch.submit(journal,label,label+'.slurm')
            self.assertEqual(len(journal['jobs']),5)

    def test_validation_launcher_does_not_queue_production(self):
        import ast
        tree=ast.parse((PACK/'scripts/launch.py').read_text())
        # Controller submission must be inside the explicit production branch.
        main=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
        production=next(n for n in main.body if isinstance(n,ast.If) and
                        isinstance(n.test,ast.Attribute) and n.test.attr=='production')
        controller_calls=[n for n in ast.walk(main) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='submit'
                          and len(n.args)>1 and isinstance(n.args[1],ast.Constant) and n.args[1].value=='controller']
        self.assertEqual(len(controller_calls),1)
        self.assertIn(controller_calls[0],list(ast.walk(production)))


if __name__=='__main__':unittest.main()
