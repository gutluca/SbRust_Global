"""Fail on any unapproved scientific difference from an executed config."""
import copy
import common as c


def signature(config):
    result={key:copy.deepcopy(config[key]) for key in ('weatherDir','ghr_root','dssat','threads','cores')}
    setup=copy.deepcopy(config['default_setup'])
    disease=config['pipeline']['disease_parameters_snapshot']
    setup['include']=['<REGIONAL_DISEASE_INPUT>' if p==disease else p for p in setup['include']]
    result['default_setup']=setup
    result['calendar']={key:config['pipeline'][key] for key in
        ('country','period','simulation_year','scenario','gcm','annual','planting_labels','calendar')}
    runs={}
    for run in config['runs']:
        arm='disease' if run['name'].startswith('disease_') else 'control' if run['name'].startswith('control_') else None
        if arm is None: raise ValueError('Unknown executed arm')
        if run['dises']!=('Y' if arm=='disease' else 'N'): raise ValueError('DISES changed')
        key=arm,run['planting_label']
        raw=run.get('sites',setup.get('sites','')).split('::',1)[1]
        record=c.replace(copy.deepcopy(run),{raw:'<SAME_FIXED_POINT_SET>'})
        for field in ('name','sites','ingeno','cname','zone_id'): record.pop(field,None)
        if key in runs and record!=runs[key]: raise ValueError('Management differs among executed GM zones')
        runs[key]=record
    result['runs']=runs
    return result


def compare_science(reference,candidate,allow_missing_control=False):
    before,after=signature(reference),signature(candidate)
    before_runs=before.pop('runs');after_runs=after.pop('runs')
    if before!=after:
        fields=[key for key in before if before[key]!=after[key]]
        raise ValueError('Scientific configuration differs from executed campaign: '+','.join(fields))
    if allow_missing_control:
        before_runs={k:v for k,v in before_runs.items() if k[0]=='disease'}
        after_runs={k:v for k,v in after_runs.items() if k[0]=='disease'}
    if before_runs!=after_runs:
        raise ValueError('Run-level management/planting differs from executed campaign')
    return True
