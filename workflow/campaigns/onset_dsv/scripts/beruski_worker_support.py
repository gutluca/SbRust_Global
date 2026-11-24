"""Partition current healthy-crop configurations by retained cell identifiers."""

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
