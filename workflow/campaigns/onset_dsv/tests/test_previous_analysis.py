import csv,gzip,json,sys,tempfile,unittest
from pathlib import Path
from datetime import date,timedelta
from contextlib import redirect_stdout
from io import StringIO
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import analyze_world_onset as s

def fixture(year=2040,n50=2,n80=1):
    plant=date(year,1,1);onset=plant+timedelta(days=25)
    row=dict(buffer_days='14',emergence_lag_assumed_days='5',plant_date=str(plant),disease_onset_dae='20',onset_date_approx=str(onset),
        eligible_days_original='180',eligible_days_after_onset='150',maturity_date='-99')
    for rule,n in [('dsv50',n50),('dsv80',n80)]:
        prefix='onset_'+rule+'_r14';row[prefix+'_uncapped']=str(n);row[prefix+'_cap6']=str(min(6,n))
        row[prefix+'_dates']=';'.join(str(onset+timedelta(days=i*15)) for i in range(n))
    row['onset_ensemble2_r14_uncapped']=str((n50+n80)/2);row['onset_ensemble2_r14_cap6']=str((min(6,n50)+min(6,n80))/2)
    return row

class AnalysisTests(unittest.TestCase):
    def test_counts_caps_and_fractional_ensemble(self):
        values,*_=s.check_row(fixture(n50=8,n80=3))
        self.assertEqual(values,[8,6,3,3,5.5,4.5])
        row=fixture();row['onset_ensemble2_r14_uncapped']='99'
        with self.assertRaises(ValueError):s.check_row(row)

    def test_onset_and_residual_errors(self):
        for change in [dict(onset_date_approx='2040-01-27'),dict(buffer_days='16'),
                       dict(onset_dsv50_r14_dates='2040-01-26;2040-02-09'),dict(onset_dsv50_r14_uncapped='nan')]:
            row=fixture();row.update(change)
            with self.assertRaises(ValueError):s.check_row(row)

    def test_missing_onset_requires_zero(self):
        row=fixture(n50=0,n80=0);row.update(disease_onset_dae='',onset_date_approx='',eligible_days_after_onset='0')
        self.assertTrue(s.check_row(row)[3])
        row['eligible_days_after_onset']='1'
        with self.assertRaises(ValueError):s.check_row(row)

    def test_full_matrix_area_weighting_years_gcms_and_source_preservation(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'source';folder=source/'countries/India';folder.mkdir(parents=True)
            pop={'retained':{'India':{'1':dict(latitude='20',longitude='70',harvest_area_ha='1'),
                                    '2':dict(latitude='21',longitude='71',harvest_area_ha='3')}}}
            population=root/'population.json';s.a.save(population,pop)
            master=folder/'summary_applications_India.csv.gz';writer=None
            with gzip.open(master,'wt',newline='') as f:
                for cell in ('1','2'):
                    for group,(period,scenario,lo,hi) in enumerate(s.a.GROUPS):
                        for year in range(lo,hi+1):
                            for gcm in s.a.GCMS:
                                for planting in s.a.PLANTS:
                                    n50=(1 if cell=='1' else 3) if group==0 else (2 if cell=='1' else 1)
                                    row=fixture(year,n50,0 if group==0 else 1)
                                    row.update(country='India',cell_id=cell,**pop['retained']['India'][cell],maturity_group='5',
                                        period=period,scenario=scenario,simulation_year=str(year),gcm=gcm,planting_label=planting,
                                        simulation_id=s.e.identity('India',cell,group,year,gcm,planting))
                                    if writer is None:writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader()
                                    writer.writerow(row)
            digest=s.a.digest(master)
            s.a.save(folder/'complete.json',dict(status='completed',country='India',points=2,rows=9200,master=str(master),sha256=digest))
            out=root/'analysis'
            with redirect_stdout(StringIO()):s.prepare(source,population,out);s.analyze(out,0);s.report(out)
            self.assertEqual(digest,s.a.digest(master))
            with (out/'India/country_period_trends.csv').open(newline='') as f:rows=list(csv.DictReader(f))
            self.assertEqual(len(rows),36)
            selected=next(r for r in rows if r['rule']=='DSV50' and r['cap']=='uncapped')
            self.assertAlmostEqual(float(selected['historical_mean']),2.5)
            self.assertAlmostEqual(float(selected['mean_applications']),1.25)
            self.assertAlmostEqual(float(selected['delta']),-1.25)
            self.assertAlmostEqual(float(selected['area_increase_pct']),25.)
            self.assertAlmostEqual(float(selected['area_decrease_pct']),75.)
            for name,n in [('country_year.csv',1104),('country_year_gcm.csv',5520),('point_trends.csv.gz',72)]:
                p=out/'India'/name;opener=gzip.open if p.suffix=='.gz' else open
                with opener(p,'rt',newline='') as f:self.assertEqual(len(list(csv.DictReader(f))),n)
            self.assertEqual(s.a.load(out/'India/complete.json')['missing_maturity_rows'],9200)
            # A modified master is rejected before any analysis publication.
            with master.open('ab') as f:f.write(b'changed')
            with self.assertRaises(ValueError):s.analyze(out,0)

if __name__=='__main__':unittest.main()
