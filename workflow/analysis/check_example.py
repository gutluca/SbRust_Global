"""Verify the real Albania example against independently audited counts."""
import argparse
import json
from pathlib import Path
import numpy as np
import pandas as pd


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True,type=Path)
    args=p.parse_args()
    a=json.loads((args.output/'countries/Albania/audit.json').read_text(encoding='utf-8'))
    assert a['country']=='Albania' and a['points']==17
    assert a['rows_before_filter']==78200 and a['rows']==78005 and a['excluded_rows']==195
    m=pd.read_csv(args.output/'country_global_comparisons.csv')
    keys=['scope','COUNTRY','PERIOD','SCENARIO']
    yields=m[m.metric.isin(['Y0','YASR','LOSS'])].pivot(index=keys,columns='metric',values=['baseline','future','delta'])
    for field in ['baseline','future','delta']:
        assert np.allclose(yields[(field,'Y0')]-yields[(field,'YASR')],yields[(field,'LOSS')],rtol=0,atol=1e-8)
    print('PASS: audited raw/retained/excluded counts and paired yield-loss identities.')


if __name__=='__main__':main()
