"""Four warning rules on daily weather; explicit Tmin proxy and 14/16-day gaps.

DVIP inputs are adapted, not measured subdaily LWD/T_LWD. Published NL is
rounded half-up to one decimal before the four risk classes (0, <=3, <=6, >6).
The upper class includes NL>9. Seven-day history continues during protection.
DSV uses the established US/Brazil three-day-mean implementation unchanged.
Residual R blocks R complete days after spraying; next possible day is D+R+1.
"""
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
import math
from pathlib import Path

from replay_dsv50_dssat_outputs import _day, read_crop

RULES = ("DVIP6", "DVIP9", "DSV50", "DSV80")
RESIDUALS = (14, 16)
POLICY = dict(version=1, wet_temperature="TMIN_PROXY", rh="DISMO_TMIN_TMAX_DEWPOINT",
    lwd="DISMO_RH_LOGISTIC", dvip_equation="Beruski2020_eq3",
    dvip_classes="NL_half_up_1_decimal;0->0;(0,3]->1;(3,6]->2;>6->3",
    dvip_history="rolling_7_days_including_today_continues_during_protection",
    dsv="prior30days_weighted_15_15_then_trailing3day_mean",
    residual="block_R_complete_days; earliest_next=D+R+1", residual_days=[14,16],
    eligibility="DAE>0 and LAIH>=0.1", cap=6,
    interpretation="weather warning counts; no efficacy or loss target; adapted daily inputs")


def humidity_wetness(tmin, tmax):
    if not all(math.isfinite(v) for v in (tmin,tmax)) or tmin > tmax or not -90 <= tmin <= tmax <= 65:
        raise ValueError(f"Invalid temperature {tmin}, {tmax}")
    t = (tmin+tmax)/2
    dew = -.036*t + .9679*tmin + .0072*(tmax-tmin) + 1.0111
    rh = min(100., max(0.,100*math.exp(17.625*dew/(243.04+dew)-17.625*t/(243.04+t))))
    # Preserve DISMO's RH<=1 fraction convention as well as its clipping.
    rh_lwd = rh*100 if rh <= 1 else rh
    lwd = min(24., max(0.,31.31/(1+math.exp(-(rh_lwd-85.17)/9.13))))
    return rh,lwd


def dvip_class(lwd, twet):
    nl = 12.611*math.exp(-5*(((lwd-21.41)/4.22)**2+((twet-14.14)/2.43)**2))
    rounded = math.floor(nl*10+.5)/10
    return (0 if rounded == 0 else 1 if rounded <= 3 else 2 if rounded <= 6 else 3), nl


def weather(path):
    """Read only. Source files are never opened in a writing mode."""
    columns = None
    rows = []
    is_wth = path.suffix.upper() == ".WTH"
    with path.open(encoding="utf-8",errors="replace") as stream:
        for line in stream:
            if line.startswith("@") and ((is_wth and "DATE" in line) or (not is_wth and "YEAR" in line)):
                columns = line.lstrip("@").split()
                needed = {"DATE","TMIN","TMAX","RAIN"} if is_wth else {"YEAR","DOY","TMIN","TMAX","PRED"}
                if not needed <= set(columns):
                    raise ValueError(f"Missing weather columns {needed}: {path}")
                continue
            fields = line.split()
            if columns is None or not fields or not fields[0].isdigit():
                continue
            def field(key): return fields[columns.index(key)]
            if is_wth:
                token=field("DATE")
                if len(token)==7:
                    stamp=_day(token[:4],token[4:])
                elif len(token)==5:
                    yy=int(token[:2]); stamp=_day(str(1900+yy if yy>=80 else 2000+yy),token[2:])
                else: raise ValueError(f"Invalid WTH date: {token}")
            else: stamp=_day(field("YEAR"),field("DOY"))
            rain=float(field("RAIN" if is_wth else "PRED"))
            tmin,tmax=float(field("TMIN")),float(field("TMAX"))
            humidity_wetness(tmin,tmax)
            if not math.isfinite(rain) or rain<0:
                raise ValueError(f"Invalid rain: {stamp}")
            if rows and stamp!=rows[-1][0]+timedelta(days=1):
                raise ValueError(f"Nonconsecutive weather: {stamp}")
            rows.append((stamp,rain,tmin,tmax))
    if not rows: raise ValueError(f"No weather: {path}")
    return rows


def drivers(rows):
    """Compute weather-dependent quantities once, shared across plantings/rules."""
    rain_history=deque(maxlen=30); means=deque(maxlen=3); dvips=deque(maxlen=7)
    output=[]
    for stamp,rain,tmin,tmax in rows:
        rh,lwd=humidity_wetness(tmin,tmax)
        dvip,nl=dvip_class(lwd,tmin)
        dvips.append(dvip)
        dsv=None
        if len(rain_history)==30:
            prior=tuple(rain_history)
            def partial(seq):
                wet=[v for v in seq if v>.5]
                return -2.1433+.3622*sum(wet)+2.573*len(wet)
            means.append(.7*partial(prior[15:])+.3*partial(prior[:15]))
            if len(means)==3: dsv=sum(means)/3
        output.append((stamp,rain,tmin,tmax,rh,lwd,nl,dvip,sum(dvips) if len(dvips)==7 else None,dsv))
        rain_history.append(rain)
    return output


def schedules(daily, eligible):
    dates={(rule,r):[] for rule in RULES for r in RESIDUALS}
    for i,values in enumerate(daily):
        if not eligible[i]: continue
        sum7,dsv=values[8],values[9]
        for (rule,residual),events in dates.items():
            if events and i-events[-1]<=residual: continue
            if rule.startswith("DVIP"):
                fire=sum7 is not None and sum7>=int(rule[4:])
            else:
                thresholds=(50,45,35,30) if rule=="DSV50" else (80,60,55,50)
                fire=dsv is not None and dsv>=thresholds[min(len(events),3)]
            if fire: events.append(i)
    return dates


class Replay:
    def __init__(self): self.cache={}

    def __call__(self, weather_path, crop_path):
        key=str(weather_path.resolve())
        if key not in self.cache: self.cache[key]=drivers(weather(weather_path))
        all_daily=self.cache[key]
        crop,old=read_crop(crop_path)
        indices={d[0]:i for i,d in enumerate(all_daily)}
        if set(crop)-set(indices): raise ValueError("Weather misses crop days")
        true=[d for d,flag in crop.items() if flag]
        if true and indices[min(true)]<32: raise ValueError("Fewer than 32 prior weather days")
        # Include 32 preceding days; retain all crop days, even in zero-canopy runs.
        lo=max(0,indices[min(crop)]-32); hi=indices[max(crop)]+1
        daily=all_daily[lo:hi]
        eligible=[crop.get(v[0],False) for v in daily]
        events=schedules(daily,eligible)
        result=dict(existing_dismo_sprays=old,eligible_crop_days=sum(eligible),
            dvip_temperature_proxy="TMIN",mean_rh_eligible=None,mean_lwd_eligible=None)
        active=[v for v,e in zip(daily,eligible) if e]
        for name,col in (("mean_rh_eligible",4),("mean_lwd_eligible",5),("mean_tmin_eligible",2)):
            result[name]=sum(v[col] for v in active)/len(active) if active else ""
        result["dvip_positive_days"]=sum(v[7]>0 for v in active)
        result["dvip_max_nl"]=max((v[6] for v in active),default=0)
        for (rule,residual),items in events.items():
            prefix=f"{rule.lower()}_r{residual}"
            result[prefix+"_uncapped"]=len(items)
            result[prefix+"_cap6"]=min(6,len(items))
            result[prefix+"_excess"]=max(0,len(items)-6)
            result[prefix+"_dates"]=";".join(daily[i][0].isoformat() for i in items)
        # Old names retain exact DSV50-R14 compatibility for the existing worker.
        result.update(dsv50_sprays_capped_at_6=result["dsv50_r14_cap6"],
            dsv50_sprays_without_cap=result["dsv50_r14_uncapped"],
            recommendations_censored_by_cap=result["dsv50_r14_excess"],
            # The legacy column lists the capped schedule; the rule-specific
            # dsv50_r14_dates column above preserves every uncapped event.
            spray_dates=result["dsv50_r14_dates"].split(";")[:6] if result["dsv50_r14_dates"] else [])
        result["daily_cache"]={"start":daily[0][0].isoformat(),
            "rain":[v[1] for v in daily],"tmin":[v[2] for v in daily],"tmax":[v[3] for v in daily],
            "eligible":[int(e) for e in eligible]}
        return result
