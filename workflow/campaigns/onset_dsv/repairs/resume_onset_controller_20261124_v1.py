"""Resume the existing applications campaign with fresh dispatcher globals.

The immutable release and all scientific inputs/workers remain unchanged.
Only the operational submission function is rebound at each phase transition.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import types
from datetime import datetime, timezone

ROOT = Path('/blue/wpavan/gustavo.luca/SBrust_world')
PACK = ROOT / 'releases/onset_dsv_co2_20261123_v1'
GEN = ROOT / 'generated/world/onset_dsv_co2_20261123_v1'
NAME = 'appsco2-resume-fix1'

def repair_binding(legacy, state):
    """The prior function snapshot retained preflight STAGES during production."""
    if tuple(legacy.STAGES) != tuple(state['stages']):
        raise ValueError('Dispatcher phase differs from durable state')
    previous = legacy.submit
    # Preserve the already adapted account-routing bytecode; bind its globals
    # to the CURRENT dispatcher module, not a snapshot from the previous phase.
    repaired = types.FunctionType(previous.__code__, vars(legacy), previous.__name__,
                                  previous.__defaults__, previous.__closure__)
    repaired.__kwdefaults__ = previous.__kwdefaults__
    legacy.submit = repaired
    return legacy

def install(campaign):
    original = campaign.configure
    def configure(state):
        legacy, recovery = original(state)
        return repair_binding(legacy, state), recovery
    campaign.configure = configure
    if hasattr(campaign, 'initialize'):
        original_initialize = campaign.initialize
        def initialize():
            state = original_initialize()
            state['controller_fix1'] = dict(
                helper=str(Path(__file__).resolve()),
                description='submission function uses current dispatcher globals at every phase',
                prior_controller_error=state.get('controller_error'),
                resume_utc=datetime.now(timezone.utc).isoformat())
            if state.get('controller_error') == 'tuple index out of range':
                state.pop('controller_error')
            return state
        # Called inside campaign.run's exclusive controller lock. The existing
        # error is archived above, and any new failure is reported normally.
        campaign.initialize = initialize

def imports():
    sys.path.insert(0, str(PACK / 'scripts'))
    import common as c
    import campaign
    import scheduler_recovery as recovery
    return c, campaign, recovery

def run():
    c, campaign, recovery = imports()
    c.compute_only()
    install(campaign)
    # campaign.run takes the existing controller lock, validates every frozen
    # input, reconciles previous submissions and refreshes all existing jobs.
    # No completed-country or task history is cleared.
    campaign.run()

def launch():
    import fcntl
    c, campaign, recovery = imports()
    with (GEN / 'launch.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = c.load(c.STATE)
        if state['status'] not in ('running', 'pending'):
            raise ValueError('Inspect campaign status before resuming: ' + state['status'])
        if state['phase'] not in campaign.PHASES or tuple(state['stages']) != campaign.PHASES[state['phase']]:
            raise ValueError('Unexpected phase/stage mapping')
        receipt = GEN / 'controller_resume_fix1_submission.json'
        if receipt.exists():
            found = recovery.lookup(NAME)
            print('PREVIOUS_FIX_LAUNCH', found or 'unresolved; inspect receipt', 'NO NEW SUBMISSION')
            return
        if recovery.lookup(NAME):
            raise RuntimeError('Repair controller already exists; no duplicate submitted')
        # Both live queue and accounting must confirm all registered controllers
        # have ended. Existing workers, if any, are retained and adopted.
        controllers = set(map(str, state.get('controllers', [])))
        if not controllers:
            raise ValueError('Missing controller history')
        queue = recovery.query(['squeue', '-r', '-h', '-u', __import__('getpass').getuser(), '-o', '%i|%j'])
        for line in queue.splitlines():
            fields = line.strip().split('|')
            if fields[0].split('_')[0] in controllers or len(fields)>1 and fields[1].startswith('appsco2-'):
                raise RuntimeError('A campaign controller is still live: ' + line)
        acct = recovery.query(['sacct', '-X', '-n', '-P', '-j', ','.join(sorted(controllers)),
                               '--format=JobID%100,State%30'])
        statuses = {}
        for line in acct.splitlines():
            fields = [v.strip() for v in line.split('|')]
            if len(fields)>=2 and fields[0] in controllers and fields[1]:
                statuses[fields[0]] = fields[1].split()[0].rstrip('+')
        if any(statuses.get(job) not in recovery.TERMINAL for job in controllers):
            raise RuntimeError('Controller termination not confirmed by accounting')
        campaign.check_inputs(state)
        helper = Path(__file__).resolve()
        helper_sha = c.sha(helper)
        backup = GEN / 'state.before_controller_fix1.json'
        with backup.open('x', encoding='utf-8') as f:
            json.dump(state, f, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
        script = GEN / 'controller_resume_fix1.slurm'
        body = ('#!/bin/bash\nset -euo pipefail\nmodule purge\nmodule load gcc/11.4.0\n'
                'module load python/3.11\nsource '+str(ROOT)+'/venvs/pythia/bin/activate\n'
                'echo '+shlex.quote(helper_sha+'  '+str(helper))+' | sha256sum -c -\n'
                'exec python '+shlex.quote(str(helper))+' --run\n')
        with script.open('x', encoding='utf-8', newline='\n') as f: f.write(body)
        cmd = ['sbatch', '--parsable', '--account=wpavan', '--qos=wpavan', '--job-name='+NAME,
               '--cpus-per-task=1', '--mem=4G', '--time=168:00:00', '--no-requeue',
               '--exclude=c0706a-s23,c0706a-s27', '--chdir='+str(ROOT),
               '--output='+str(ROOT/'logs/appsco2-fix1-%j.out'),
               '--error='+str(ROOT/'logs/appsco2-fix1-%j.err'), str(script)]
        c.save(receipt, dict(name=NAME,command=cmd,helper_sha256=helper_sha,
                            intent_utc=datetime.now(timezone.utc).isoformat(),controllers_confirmed_ended=statuses))
        # Exactly one submission. A timeout leaves the intent for reconciliation.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        c.save(GEN/'controller_resume_fix1_response.json',
               dict(returncode=result.returncode,stdout=result.stdout,stderr=result.stderr))
        if result.returncode or not re.fullmatch(r'\d+(?:;[^\s]+)?',result.stdout.strip()):
            raise RuntimeError('Submission requires reconciliation: '+result.stdout+result.stderr)
        print('FIX_CONTROLLER_SUBMITTED',result.stdout.strip())
        print('Existing state, completed bundles and active workers retained. Scientific code unchanged.')

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--launch',action='store_true');g.add_argument('--run',action='store_true')
    a=p.parse_args()
    launch() if a.launch else run()
