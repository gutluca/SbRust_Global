"""Recompute the paired yield/loss analysis from the 103 final master files."""
from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
import os
from pathlib import Path
import time
import pandas as pd


def main():
    repo=Path(__file__).resolve().parents[2]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,required=True,help='Directory containing countries/<country>/summary_master_*.csv.gz')
    parser.add_argument('--output',type=Path,required=True,help='New directory; existing outputs are never overwritten')
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--countries',nargs='+',help='Optional exact country names; omit for all 103 countries')
    args=parser.parse_args()
    if args.workers<1:parser.error('--workers must be positive')
    args.output=args.output.resolve()
    inventory=json.loads((repo/'provenance/master_files.json').read_text(encoding='utf-8'))
    if args.countries:
        missing=set(args.countries)-{a['country'] for a in inventory}
        if missing:parser.error(f'Unknown countries: {sorted(missing)}')
        inventory=[a for a in inventory if a['country'] in args.countries]
    sources=[]
    for a in inventory:
        source=args.data_root.resolve()/a['path']
        if not source.is_file():parser.error(f'Missing master: {source}')
        sources.append(dict(country=a['country'],folder=a['folder'],source=str(source),sha256=a['sha256'],
                            rows_retained=a['rows'],expected_points=a['points']))
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'metadata').mkdir()
    gm=pd.read_csv(repo/'data/spatial/selected_maturity_groups.csv',dtype={'GM':str})
    for a in inventory:
        meta=gm.loc[gm.COUNTRY==a['country'],['COUNTRY','CELL_ID','LATITUDE','LONGITUDE','HARVEST_AREA_HA','GM']].rename(columns={'GM':'MATURITY_GROUP'})
        meta.to_csv(args.output/'metadata'/f"{a['folder']}.csv",index=False)
    os.environ['SBRUST_ANALYSIS_OUTPUT']=str(args.output)
    import engine
    engine.OUT=args.output
    (args.output/'input_manifest.json').write_text(json.dumps(sources,indent=2)+'\n',encoding='utf-8')
    audits=[];start=time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(engine.worker,a) for a in sources]):
            audit=future.result();audits.append(audit)
            print(f"{len(audits)}/{len(sources)} {audit['country']} retained={audit['rows']:,} excluded={audit['excluded_rows']:,} elapsed={time.time()-start:.0f}s",flush=True)
    engine.consolidate(audits)
    print('Completed:',args.output)


if __name__=='__main__':
    main()
