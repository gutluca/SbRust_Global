"""Open the production gate only after every pilot validates realized CO2."""
from pathlib import Path
import common as c


def main():
    state = c.load(c.STATE)
    shared = state['shared_default_co2']
    if c.sha(shared['path']) != shared['sha256_before']:
        raise ValueError('Shared default CO2 changed during pilot; inspect before production')
    manifest = [Path(p) for p in (c.GEN / 'pilot_manifest.txt').read_text().splitlines() if p.strip()]
    lock = c.load(c.GEN / 'pilot_lock.json')['config_sha256']
    combinations = set()
    replayed = set()
    fingerprints={}
    days = runs = 0
    for path in manifest:
        if c.sha(path) != lock[str(path)]:
            raise ValueError('Pilot config changed')
        cfg = c.load(path)
        meta = cfg['pipeline']
        identity=(meta['country'], meta['simulation_year'], meta['scenario'], meta['gcm'])
        is_replay=meta['co2_rerun'].get('reference_replay',False)
        target_set=replayed if is_replay else combinations
        if identity in target_set: raise ValueError('Duplicate pilot identity')
        target_set.add(identity)
        folder = c.RESULTS / 'pilot/tasks' / cfg['name']
        audit = c.load(folder / 'audit.json')
        c.assert_allowed_node(audit['execution_node'])
        fingerprints[(identity,is_replay)]=c.load(folder/'pilot_inputs_sha256.json')
        co2 = c.load(folder / 'co2_audit.json')
        if audit.get('status') != 'ok' or audit['source_config_sha256'] != c.sha(path):
            raise ValueError('Pilot model/collation did not pass')
        if (co2.get('status') != 'ok' or co2['forcing_sha256'] != meta['co2_forcing']['sha256'] or
                co2['checked_experiments'] != meta['co2_rerun']['expected_points'] * 10 or
                co2['maximum_absolute_error_ppm'] > .051):
            raise ValueError('Pilot daily CO2 validation did not pass')
        days += co2['checked_daily_values']
        runs += co2['checked_experiments']
        if is_replay:
            proof=c.load(folder/'replay_audit.json')
            if (proof.get('status')!='ok' or proof['matched_pairs']!=meta['co2_rerun']['expected_points']*5 or
                proof['maximum_absolute_errors']['YIELD_NO_DISEASE_KG_HA']!=0 or
                proof['maximum_absolute_errors']['YIELD_WITH_DISEASE_KG_HA']!=0):
                raise ValueError('Original-CO2 replay did not reproduce published outputs exactly')
    expected = c.pilot_matrix()
    if combinations != expected or replayed != expected or len(manifest) != c.PILOT_CONFIGS:
        raise ValueError('Pilot coverage incomplete')
    for identity in expected:
        if fingerprints[(identity,False)]!=fingerprints[(identity,True)]:
            raise ValueError('Rendered experiment/weather/soil/management inputs changed beyond CO2: '+str(identity))
    for country in state['countries']:
        _,ready,_=c.paths(state,country)
        receipt=c.load(ready/'reuse_audit.json')
        if (receipt['status']!='ok' or receipt['points']!=country['site_count'] or
            len(receipt['config_sha256'])!=750 or len(receipt['executed_reference_config_sha256'])!=750):
            raise ValueError('Full-country preflight incomplete: '+country['country'])
        for path,expected_hash in {**receipt['config_sha256'],**receipt['sites_sha256']}.items():
            if c.sha(path)!=expected_hash: raise ValueError('Prepared input changed: '+path)
    for record in state['frozen_files']:
        if c.sha(record['path'])!=record['sha256']: raise ValueError('Frozen input changed: '+record['path'])
    c.save(c.GEN / 'pilot_passed.json', {'status': 'ok', 'pilot_configurations': len(manifest),
        'numerical_replays_passed':len(replayed),'prepared_countries':len(state['countries']),
        'all_future_configurations_checked':750*len(state['countries']),
        'excluded_nodes':list(c.BAD_NODES),
        'checked_experiments': runs, 'checked_daily_values': days,
        'gms_recalibrated': 0, 'historical_runs_repeated': 0})
    print('PILOT_PASSED', len(manifest), 'configs', runs, 'crop runs', days,
          'daily CO2 checks;',c.PILOT_CASES,'numerical replays passed; 103 countries prepared; no production job submitted', flush=True)


if __name__ == '__main__':
    main()
