"""Existing scientific warning-rule checks retained for the shared replay code."""
import csv,gzip,hashlib,json,math,os,random,sys,tempfile,unittest,io
from contextlib import redirect_stdout
from datetime import date,timedelta
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'campaigns/onset_dsv/scripts'))
import beruski_rules as rules
from dsv50_warning_pilot import Day,warning_schedule
from replay_dsv50_dssat_outputs import replay as previous_replay

class RulesTests(unittest.TestCase):
    def test_published_equation_peak_and_extremes(self):
        category,nl=rules.dvip_class(21.41,14.14)
        self.assertAlmostEqual(nl,12.611);self.assertEqual(category,3)
        self.assertEqual(rules.dvip_class(0,30)[0],0)
        for target,expected in ((0.04,0),(.06,1),(3,1),(3.1,2),(6,2),(6.1,3),(10,3)):
            t=14.14+2.43*math.sqrt(-math.log(target/12.611)/5)
            self.assertEqual(rules.dvip_class(21.41,t)[0],expected)

    def test_humidity_matches_fortran_expression(self):
        lo,hi=14,24;t=(lo+hi)/2;dew=-.036*t+.9679*lo+.0072*(hi-lo)+1.0111
        rh=100*math.exp(17.625*dew/(243.04+dew))/math.exp(17.625*t/(243.04+t))
        actual,lwd=rules.humidity_wetness(lo,hi)
        self.assertAlmostEqual(actual,rh)
        self.assertAlmostEqual(lwd,31.31/(1+math.exp(-(rh-85.17)/9.13)))
        with self.assertRaises(ValueError):rules.humidity_wetness(30,20)

    def test_dsv50_14_matches_previous_algorithm_for_random_daily_series(self):
        rng=random.Random(47)
        for n in range(20):
            rain=[rng.choice([0,.5,.6,2,10,30,60]) for i in range(220)]
            eligible=[i>=40 and i<190 and rng.random()>.1 for i in range(220)]
            series=[(date(2030,1,1)+timedelta(days=i),r,15,25) for i,r in enumerate(rain)]
            events=rules.schedules(rules.drivers(series),eligible)["DSV50",14]
            old=warning_schedule([Day(r,e) for r,e in zip(rain,eligible)])
            self.assertEqual(events[:6],list(old.spray_day_indices))
            self.assertEqual(len(events),old.uncapped_spray_count)

    def test_residual_windows_and_four_integer_rules(self):
        daily=[(date(2030,1,1)+timedelta(days=i),10,14,16,95,22,10,3,21,100) for i in range(150)]
        events=rules.schedules(daily,[True]*150)
        for rule in rules.RULES:
            self.assertEqual(events[rule,14],list(range(0,150,15)))
            self.assertEqual(events[rule,16],list(range(0,150,17)))
        self.assertTrue(all(not v for v in rules.schedules(daily,[False]*150).values()))

    def test_rain_threshold_and_continuous_rolling_dvip(self):
        series=[(date(2030,1,1)+timedelta(days=i),.5,14,16) for i in range(70)]
        daily=rules.drivers(series)
        self.assertIsNone(daily[5][8]);self.assertIsNotNone(daily[6][8])
        self.assertAlmostEqual(daily[-1][9],-2.1433)

    def files(self,root):
        lines=["@ DATE SRAD TMAX TMIN RAIN"]; crop=["@YEAR DOY DAE LAIH NSPRAYS"]
        for i in range(170):
            d=date(2035,12,1)+timedelta(days=i); doy=d.timetuple().tm_yday
            lines.append(f"{d.year}{doy:03d} 20 16 14 10")
            if i>=40:crop.append(f"{d.year} {doy} {i-39} 1 2")
        w=root/"test.WTH";c=root/"DISMO.OUT";w.write_text("\n".join(lines));c.write_text("\n".join(crop))
        return w,c

    def test_readonly_weather_cache_and_recount(self):
        with tempfile.TemporaryDirectory() as temp:
            w,c=self.files(Path(temp));original=w.read_bytes()
            result=rules.Replay()(w,c);cache=result["daily_cache"]
            self.assertEqual(w.read_bytes(),original)
            self.assertEqual(result["dsv50_r14_cap6"],6)
            self.assertGreater(result["dsv50_r14_uncapped"],6)
            rows=[(date.fromisoformat(cache["start"])+timedelta(days=i),rain,lo,hi)
                  for i,(rain,lo,hi) in enumerate(zip(cache["rain"],cache["tmin"],cache["tmax"]))]
            events=rules.schedules(rules.drivers(rows),cache["eligible"])
            for (rule,residual),values in events.items():
                self.assertEqual(len(values),result[f"{rule.lower()}_r{residual}_uncapped"])
            lines=original.decode().splitlines();w.write_text("\n".join(lines[:50]+lines[51:]))
            with self.assertRaises(ValueError):rules.Replay()(w,c)

    def test_legacy_dates_match_previous_replay_above_cap(self):
        # Independent reference: the exact replay shipped for the prior DSV50
        # campaign, not a fixture generated with the new implementation.
        with tempfile.TemporaryDirectory() as temp:
            w,c=self.files(Path(temp))
            old=previous_replay(w,c); new=rules.Replay()(w,c)
            self.assertEqual(old["dsv50_sprays_without_cap"],9)
            for field in ("eligible_crop_days","existing_dismo_sprays",
                          "dsv50_sprays_capped_at_6","dsv50_sprays_without_cap",
                          "recommendations_censored_by_cap","spray_dates"):
                self.assertEqual(new[field],old[field],field)
            self.assertEqual(len(new["spray_dates"]),6)
            self.assertEqual(len(new["dsv50_r14_dates"].split(";")),9)
            self.assertEqual(new["spray_dates"],new["dsv50_r14_dates"].split(";")[:6])

if __name__=='__main__':unittest.main()
