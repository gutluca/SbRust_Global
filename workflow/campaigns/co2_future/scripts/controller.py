"""Use the previous bounded dispatcher; skip sites/calibration/history stages."""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import sys

import common as c


def configure(state):
    # Slurm honors SBATCH_EXCLUDE for every dynamic sbatch call from the
    # unchanged legacy dispatcher; all batch scripts repeat the same exclusion.
    os.environ['SBATCH_EXCLUDE']=c.EXCLUDED_NODES
    sys.path.insert(0, str(Path(state['engine']) / 'scripts'))
    import world_campaign as legacy
    def check_inputs(current):
        if current['version'] != 7 or current['expected_configs'] != 750 or tuple(current['stages']) != c.STAGES:
            raise ValueError('Not the frozen CO2 future campaign')
        gate = c.load(Path(current['output']) / 'pilot_passed.json')
        if (gate.get('status') != 'ok' or gate['pilot_configurations'] != c.PILOT_CONFIGS or
            gate.get('numerical_replays_passed')!=c.PILOT_CASES or gate.get('prepared_countries')!=103):
            raise ValueError('Realized-CO2 pilot has not passed')
        if current['excluded_nodes']!=list(c.BAD_NODES):
            raise ValueError('Known storage-failure nodes are not excluded')
        for record in current['frozen_files']:
            if c.sha(record['path']) != record['sha256']:
                raise ValueError('Frozen input changed: ' + record['path'])
    def specification(current, country):
        base, ready, result = c.paths(current, country)
        stage = c.STAGES[country['stage']]
        manifest = ready / 'annual_configs/manifest.txt'
        if stage == 'prepare':
            return 'prepare.slurm', [Path(current['output']) / 'state.json', country['key']], 1, [0], 12
        if stage in ('annual_first', 'annual_rest'):
            if len(manifest.read_text().splitlines()) != 750:
                raise ValueError('Future manifest incomplete')
            indices = [0] if stage == 'annual_first' else list(range(1, 750))
            return 'worker.slurm', [Path(current['output']) / 'state.json', manifest, result / 'annual'], 4, indices, 24
        if stage == 'master':
            return 'merge.slurm', [Path(current['output']) / 'state.json', country['key']], 1, [0], 24
        raise ValueError(stage)
    def recover(current, country, index):
        if c.STAGES[country['stage']] not in ('annual_first', 'annual_rest'):
            return False
        _, ready, result = c.paths(current, country)
        cfg = Path((ready / 'annual_configs/manifest.txt').read_text().splitlines()[index])
        shard = result / 'annual/tasks' / cfg.stem
        try:
            audit = c.load(shard / 'audit.json')
            co2 = c.load(shard / 'co2_audit.json')
            return audit.get('status') == 'ok' and audit['source_config_sha256'] == c.sha(cfg) and co2.get('status') == 'ok'
        except FileNotFoundError:
            return False
    legacy.STAGES = c.STAGES
    legacy.ARRAY_STAGES = {'annual_first', 'annual_rest'}
    legacy.BURST_STAGES = {'annual_first', 'annual_rest'}
    # Same annual retry limits as the completed global dispatcher. Successful
    # and still-active indices are never retried; published audited shards are
    # recovered before a failed scheduler exit is counted as a model failure.
    legacy.ATTEMPT_LIMITS = {'prepare': 1, 'annual_first': 2, 'annual_rest': 2, 'master': 1}
    legacy.specification = specification
    legacy.check_frozen_inputs = check_inputs
    legacy.recover_published = recover
    return legacy


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('state', type=Path)
    p.add_argument('--status', action='store_true')
    a = p.parse_args()
    state = c.load(a.state)
    legacy = configure(state)
    if a.status:
        print(legacy.summary(state))
    else:
        legacy.run(a.state)


if __name__ == '__main__':
    main()
