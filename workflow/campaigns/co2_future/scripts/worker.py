"""Reuse the previous Pythia/scratch/collation flow with an audited CO2 stage."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

import common as c
from co2_runtime import stage_co2, verify_co2


def call(script, *arguments, cwd=None):
    subprocess.run([sys.executable, str(script), *map(str, arguments)], check=True, cwd=cwd)


def execute(state_path, manifest, results, index, pilot=False):
    c.assert_allowed_node(socket.gethostname())
    if not os.environ.get('SLURM_JOB_ID'):
        raise RuntimeError('DSSAT execution is restricted to Slurm compute jobs')
    state = c.load(state_path)
    paths = [Path(p) for p in Path(manifest).read_text().splitlines() if p.strip()]
    source = paths[index]
    cfg = c.load(source)
    if c.sha(cfg['dssat']['executable']) != state['executable_sha256']:
        raise ValueError('Model executable changed')
    country = next(x for x in state['countries'] if x['country'] == cfg['pipeline']['country'])
    base, ready, _ = c.paths(state, country)
    receipt = c.load(Path(state['output']) / 'pilot_lock.json') if pilot else c.load(ready / 'reuse_audit.json')
    if receipt['config_sha256'].get(str(source)) != c.sha(source):
        raise ValueError('Prepared source config checksum differs')
    required_sites = {r.get('sites', cfg['default_setup'].get('sites', ''))[len('xy_from_vector::'):] for r in cfg['runs']}
    for path in required_sites:
        if receipt['sites_sha256'].get(path) != c.sha(path):
            raise ValueError('Frozen selected-GM site file changed: ' + path)
    if c.sha(country['disease_input']) != country['disease_input_sha256']:
        raise ValueError('Frozen regional disease input changed')
    results = Path(results)
    target = results / 'tasks' / cfg['name']
    if target.exists():
        raise FileExistsError('Completed shard exists; never rerun successful index: ' + str(target))
    scratch = os.environ.get('TMPDIR') or os.environ.get('SLURM_TMPDIR')
    if not scratch or not Path(scratch).is_dir():
        raise RuntimeError('Node-local scratch missing')
    engine = Path(state['engine']) / 'scripts'
    with tempfile.TemporaryDirectory(prefix='co2-future-', dir=scratch) as temporary:
        temp = Path(temporary)
        work = temp / 'work'
        execution = temp / (cfg['name'] + '.json')
        local_shard = temp / 'shard'
        weather_audit = temp / 'weather_window_audit.json'
        try:
            call(engine / 'prepare_scratch_config.py', source, execution, '--work-dir', work)
            call('-m', 'pythia', execution, '--setup', '--quiet', cwd=temp)
            actual = c.load(execution)
            staged = stage_co2(actual, work)
            call(engine / 'materialize_weather_window.py', execution, work, temp / 'weather', '--audit', weather_audit)
            if pilot:
                from replay import rendered_inputs
                fingerprints=rendered_inputs(actual,work)
            call('-m', 'pythia', execution, '--run-dssat', '--quiet', cwd=temp)
            co2_checks = temp / 'co2_checks'
            co2_audit = verify_co2(actual, work, co2_checks)
            if staged != co2_audit['checked_experiments']:
                raise ValueError('CO2-staged and checked run counts differ')
            call(engine / 'collate_annual_task.py', source, work, local_shard,
                 '--execution-config', execution, '--weather-window-audit', weather_audit,
                 '--job-id', os.environ['SLURM_JOB_ID'], '--array-task-id', str(index))
            if pilot:
                from replay import pilot_master
                pilot_master(state,country,cfg,local_shard,temp)
                c.save(local_shard/'pilot_inputs_sha256.json',fingerprints)
            for path in co2_checks.iterdir():
                shutil.copy2(path, local_shard / path.name)
            audit = c.load(local_shard / 'audit.json')
            audit['co2_forcing'] = co2_audit
            audit['co2_rerun'] = cfg['pipeline']['co2_rerun']
            audit['execution_node']=socket.gethostname()
            audit['slurm_nodelist']=os.environ.get('SLURM_JOB_NODELIST')
            c.save(local_shard / 'audit.json', audit)
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = results / '.staging' / (cfg['name'] + '.' + os.environ['SLURM_JOB_ID'])
            if staging.exists() or target.exists():
                raise FileExistsError('Staging or published shard already exists')
            staging.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(local_shard, staging)
            staging.rename(target)
            print('COMPLETE', cfg['name'], 'daily_CO2_values', co2_audit['checked_daily_values'], flush=True)
        except BaseException:
            failure = results / 'failures' / (cfg['name'] + '.' + os.environ['SLURM_JOB_ID'])
            failure.mkdir(parents=True, exist_ok=False)
            shutil.copy2(source, failure / 'source_config.json')
            if execution.exists():
                shutil.copy2(execution, failure / 'execution_config.json')
            # Small, bounded diagnostics survive scratch cleanup.
            selected = (list(temp.rglob('ERROR.OUT'))[:5] + list(temp.rglob('WARNING.OUT'))[:5] +
                        list(work.rglob('Weather.OUT'))[:3] + list(work.rglob('Summary.OUT'))[:3] +
                        list(work.rglob('SBG00001.SBX'))[:3] + list(work.rglob('CO2048.WDA'))[:1] +
                        list(temp.glob('pythia*.log'))[:2])
            for i, path in enumerate(selected):
                with path.open('rb') as stream:
                    size = path.stat().st_size
                    if size > 131072:
                        stream.seek(size - 131072)
                    (failure / f'{i:02d}_{path.name}').write_bytes(stream.read(131072))
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('state', type=Path)
    p.add_argument('manifest', type=Path)
    p.add_argument('results', type=Path)
    p.add_argument('--index', type=int, required=True)
    p.add_argument('--pilot', action='store_true')
    a = p.parse_args()
    execute(a.state, a.manifest, a.results, a.index, a.pilot)


if __name__ == '__main__':
    main()
