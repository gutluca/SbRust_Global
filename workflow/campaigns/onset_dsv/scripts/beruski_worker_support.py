"""Spatial partition, archived-baseline parity, and compact daily-cache output."""
import csv
import gzip
import json
from pathlib import Path


def partition_controls(controls, shard, shards):
    if shards<1 or not 0<=shard<shards: raise ValueError("Invalid shard")
    cells=sorted({key[0] for key in controls},key=int)
    selected=set(cells[len(cells)*shard//shards:len(cells)*(shard+1)//shards])
    if not selected: raise ValueError("Empty shard")
    return {k:v for k,v in controls.items() if k[0] in selected},selected


def partition_sites(config, root, cells):
    import fiona
    from prepare_scratch_config import vector_site_paths,replace_strings
    paths=dict(vector_site_paths(config)); replacements={}; nonempty=set(); selected=[]
    for i,(raw,source) in enumerate(paths.items()):
        target=root/f"partition_{i:03d}.gpkg"
        with fiona.open(source) as src:
            features=[f for f in src if str(f["properties"]["CellID"]) in cells]
            if not features: continue
            with fiona.open(target,"w",driver="GPKG",schema=src.schema,crs_wkt=src.crs_wkt,layer="sites") as dst:
                dst.writerecords(features)
        replacements[raw]=str(target); nonempty.add(raw); selected.append({"sample":str(target)})
    config=dict(config)
    config["runs"]=[r for r in config["runs"] if r.get("sites",config["default_setup"].get("sites","")).split("::",1)[1] in nonempty]
    if not config["runs"]: raise ValueError("No partition runs")
    return replace_strings(config,replacements),selected


def parity(rows, previous, expected_sha):
    from beruski_campaign import digest
    if digest(previous)!=expected_sha: raise ValueError("Previous DSV50 results changed")
    wanted={r["simulation_id"]:r for r in rows}
    matched=set()
    with gzip.open(previous,"rt",newline="",encoding="utf-8") as stream:
        for old in csv.DictReader(stream):
            key=old["simulation_id"]
            if key not in wanted: continue
            if key in matched: raise ValueError("Duplicate previous simulation")
            new=wanted[key]
            for field in ("plant_date","maturity_date","maturity_group","weather_source_cell_id","spray_dates"):
                if str(new[field])!=old[field]: raise ValueError(f"Previous identity/date mismatch {key}: {field}")
            for field in ("raw_yield_kg_ha","harvest_area_ha","eligible_crop_days",
                          "dsv50_sprays_capped_at_6","dsv50_sprays_without_cap"):
                if float(new[field])!=float(old[field]): raise ValueError(f"Previous DSV50 mismatch {key}: {field}")
            matched.add(key)
    if matched!=set(wanted): raise ValueError("Previous DSV50 lacks some partition simulations")


def output_extras(staging, rows, caches, task):
    from beruski_campaign import save,digest
    from beruski_rules import POLICY,RULES,RESIDUALS
    cache=staging/"daily_cache.jsonl.gz"
    with gzip.open(cache,"wt",encoding="utf-8",compresslevel=1) as stream:
        for row,values in zip(rows,caches):
            stream.write(json.dumps(dict(simulation_id=row["simulation_id"],**values),separators=(",",":"))+"\n")
    weight=sum(float(r["harvest_area_ha"]) for r in rows)
    metrics={}
    for rule in RULES:
        for residual in RESIDUALS:
            prefix=f"{rule.lower()}_r{residual}"
            metrics[prefix]={key:sum(float(r["harvest_area_ha"])*int(r[prefix+"_"+key]) for r in rows)
                for key in ("cap6","uncapped","excess")}
            metrics[prefix]["censored_weight"]=sum(float(r["harvest_area_ha"]) for r in rows if r[prefix+"_excess"]>0)
    audit=dict(task_sha256=task["task_sha256"],daily_cache_sha256=digest(cache),
        previous_dsv50_r14_parity="all rows match",partition=[task["shard"],task["shards"]],
        policy=POLICY,rows=len(rows),weight=weight,metrics=metrics,
        dvip_positive_day_weight=sum(float(r["harvest_area_ha"])*r["dvip_positive_days"] for r in rows),
        eligible_day_weight=sum(float(r["harvest_area_ha"])*r["eligible_crop_days"] for r in rows))
    save(staging/"multi_audit.json",audit)
    return audit
