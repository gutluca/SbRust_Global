"""Submit validation first; production requires passing actual replay results.

The user has authorized this campaign. Journal each submission BEFORE sbatch;
never retry an ambiguous submission or overwrite existing campaign outputs.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import getpass
from pathlib import Path
import re
import subprocess

import common as c

JOURNAL = c.BASE / 'logs' / (c.VERSION + '_submission.json')


def submit(journal, label, script, dependency=None, array=None, cpus=1, memory='4G', hours=2):
    if label in journal['jobs']:
        raise ValueError('Submission intent already exists: ' + label)
    name = 'co2v2-' + label
    command = ['sbatch', '--parsable', '--account=wpavan', '--qos=wpavan', '--no-requeue',
               '--job-name=' + name, '--cpus-per-task=' + str(cpus), '--mem=' + memory,
               '--exclude='+c.EXCLUDED_NODES,
               '--time=' + str(hours) + ':00:00']
    if dependency:
        command += ['--dependency=afterok:' + dependency, '--kill-on-invalid-dep=yes']
    if array:
        command += ['--array=' + array]
    command += [str(c.PACK / 'slurm' / script)]
    journal['jobs'][label] = {'job': None, 'command': command, 'intent_utc': datetime.now(timezone.utc).isoformat()}
    c.save(JOURNAL, journal)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        journal['jobs'][label]['error'] = 'Acceptance unknown after timeout; reconcile by recorded job name before retry'
        c.save(JOURNAL, journal)
        raise
    if result.returncode:
        journal['jobs'][label]['error'] = result.stderr
        journal['jobs'][label]['stdout'] = result.stdout
        c.save(JOURNAL, journal)
        raise RuntimeError('sbatch rejected submission: ' + result.stderr)
    job = result.stdout.strip().split(';')[0]
    if not re.fullmatch(r'[0-9]+', job):
        raise RuntimeError('Ambiguous scheduler response; acceptance must be reconciled')
    journal['jobs'][label]['job'] = job
    c.save(JOURNAL, journal)
    print('SUBMITTED', label, job, flush=True)
    return job


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--status', action='store_true')
    p.add_argument('--production',action='store_true')
    a = p.parse_args()
    if a.status:
        for name, entry in c.load(JOURNAL)['jobs'].items():
            print(name, entry['job'] or 'UNRESOLVED', entry.get('error', ''))
        return
    if a.production:
        state=c.load(c.STATE)
        from controller import configure
        configure(state).check_frozen_inputs(state)
        journal=c.load(JOURNAL)
        if 'controller' in journal['jobs']:
            raise FileExistsError('Production submission already recorded; do not duplicate')
        job=submit(journal,'controller','controller.slurm',dependency=journal['jobs']['gate']['job'],memory='4G',hours=168)
        journal['status']='production_submitted';c.save(JOURNAL,journal)
        print('PRODUCTION_SUBMITTED',job)
        return
    if JOURNAL.exists() or c.GEN.exists() or c.RESULTS.exists():
        raise FileExistsError('Existing campaign or submission journal; inspect instead of submitting again')
    for record in c.load(c.PACK / 'package_manifest.json')['files']:
        if c.sha(c.PACK / record['path']) != record['sha256']:
            raise ValueError('Package checksum mismatch: ' + record['path'])
    # Read-only inventory; do not cancel or alter any other job.
    queue = subprocess.run(['squeue', '-h', '-u', getpass.getuser(), '-o', '%i %j %q %C %T'],
                           check=True, capture_output=True, text=True, timeout=30).stdout
    if 'co2v2-' in queue or 'co2v1-' in queue:
        raise RuntimeError('A CO2 campaign job already exists; reconcile before launch')
    journal = {'version': c.VERSION, 'jobs': {}, 'queue_before_launch': queue, 'authorization': 'user requested global CO2 correction and submission'}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with JOURNAL.open('x', encoding='utf-8') as stream:
        __import__('json').dump(journal, stream, indent=2)
    bootstrap = submit(journal, 'bootstrap', 'bootstrap.slurm', memory='16G', hours=4)
    preflight=submit(journal,'preflight','preflight.slurm',dependency=bootstrap,array='0-102%16',memory='16G',hours=12)
    pilot = submit(journal, 'pilot', 'pilot.slurm', dependency=preflight, array=f'0-{c.PILOT_CONFIGS-1}%8', cpus=4, memory='16G', hours=4)
    gate = submit(journal, 'gate', 'gate.slurm', dependency=pilot, memory='4G', hours=1)
    journal['status'] = 'validation_submitted'
    c.save(JOURNAL, journal)
    print('VALIDATION_SUBMITTED', 'bootstrap=' + bootstrap,'preflight='+preflight, 'pilot=' + pilot, 'gate=' + gate)
    print('No global production submitted. Inspect real replay and CO2 audits before --production.')


if __name__ == '__main__':
    main()
