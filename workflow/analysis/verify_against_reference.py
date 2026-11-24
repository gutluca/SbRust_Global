"""Compare recomputed country results against the previously audited report."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd


def compare(left,right,keys):
    if set(left.columns)!=set(right.columns):raise AssertionError('Column mismatch')
    left=left.sort_values(keys).reset_index(drop=True)
    right=right.sort_values(keys).reset_index(drop=True)[left.columns]
    assert len(left)==len(right)
    maximum=0.
    for column in left:
        a,b=left[column],right[column]
        if pd.api.types.is_numeric_dtype(a) and pd.api.types.is_numeric_dtype(b):
            av,bv=a.to_numpy(dtype=float),b.to_numpy(dtype=float)
            if not np.allclose(av,bv,rtol=1e-12,atol=1e-8,equal_nan=True):
                raise AssertionError(f'Numerical mismatch: {column}')
            finite=np.isfinite(av)&np.isfinite(bv)
            if finite.any():maximum=max(maximum,float(np.max(np.abs(av[finite]-bv[finite]))))
        else:
            assert a.fillna('').astype(str).equals(b.fillna('').astype(str)),column
    return {'rows':len(left),'columns':len(left.columns),'max_abs_numeric_difference':maximum}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference',required=True,type=Path,help='Audited report data/ directory')
    parser.add_argument('--recomputed',required=True,type=Path,nargs='+')
    parser.add_argument('--receipt',required=True,type=Path)
    args=parser.parse_args();checks=[];counts=[]
    tables={
        'member_means.csv.gz':['COUNTRY','CELL_ID','PERIOD','SCENARIO','GCM','PLANTING_LABEL'],
        'gcm_comparisons.csv.gz':['COUNTRY','CELL_ID','PERIOD','SCENARIO','GCM'],
        'planting_summary.csv':['COUNTRY','PERIOD','SCENARIO','PLANTING_LABEL','metric'],
        'annual.csv.gz':['COUNTRY','PERIOD','SCENARIO','SIM_YEAR','GCM','PLANTING_LABEL'],
        'exclusion_counts.csv.gz':['COUNTRY','CELL_ID','PERIOD','SCENARIO','GCM','PLANTING_LABEL'],
        'excluded_simulations.csv.gz':['SIMULATION_ID']}
    for output in args.recomputed:
        for folder in sorted((output/'countries').iterdir()):
            a=json.loads((folder/'audit.json').read_text(encoding='utf-8'))
            b=json.loads((args.reference/'countries'/folder.name/'audit.json').read_text(encoding='utf-8'))
            for key in ['rows_before_filter','excluded_rows','exclusion_totals','rows','points','area_ha','qc']:
                assert a[key]==b[key],(folder.name,key)
            counts.append({'country':a['country'],'raw_rows':a['rows_before_filter'],'retained_rows':a['rows'],
                           'excluded_rows':a['excluded_rows']})
            for filename,keys in tables.items():
                result=compare(pd.read_csv(folder/filename),pd.read_csv(args.reference/'countries'/folder.name/filename),keys)
                checks.append(dict(country=a['country'],table=filename,**result))
        for name in ['country_global_comparisons.csv','country_global_gcm.csv']:
            current=pd.read_csv(output/name);current=current[current.scope=='country']
            old=pd.read_csv(args.reference/name)
            old=old[(old.scope=='country')&old.COUNTRY.isin(current.COUNTRY)]
            keys=['scope','COUNTRY','PERIOD','SCENARIO','metric']+(['GCM'] if name.endswith('_gcm.csv') else [])
            result=compare(current,old,keys)
            checks.append(dict(table=name,**result))
    receipt={'status':'passed','date':'2026-11-24','scope':'Numerical portability; no DSSAT rerun',
             'raw_rows_checked':sum(a['raw_rows'] for a in counts),'countries':counts,'checks':checks,
             'tolerance':{'relative':1e-12,'absolute':1e-8},
             'note':'Three real countries including both final GM revisions. This does not certify the pending application campaign, all supplementary plots, or full model rebuild.'}
    args.receipt.parent.mkdir(parents=True,exist_ok=True)
    args.receipt.write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':receipt['status'],'raw_rows_checked':receipt['raw_rows_checked'],
                      'countries':len(counts),'tables_checked':len(checks),
                      'max_abs_numeric_difference':max(a['max_abs_numeric_difference'] for a in checks)},indent=2))


if __name__=='__main__':main()
