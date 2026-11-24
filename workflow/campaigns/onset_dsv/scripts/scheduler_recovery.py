"""Two-account operational supervisor; frozen workers and science unchanged."""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import inspect
import hashlib

ROOT = Path('/blue/wpavan/gustavo.luca/SBrust_world')
GEN = ROOT / 'generated/world/co2_future_global_20261119_v2'
CODE = ROOT / 'releases/co2_future_global_20261119_v2'
NAME = 'co2v2-controller-multi1'
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'NODE_FAIL',
            'OUT_OF_MEMORY', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE', 'REVOKED'}


def configure_accounts(state, legacy):
    old = {'wpavan': 89, 'wpavan-b': 810}
    new = {'wpavan': 89, 'hoogenboom': 200, 'wpavan-b': 810}
    if state['budget_cpus'] not in (old, new):
        raise ValueError('Unexpected CPU budgets')
    source = inspect.getsource(legacy.submit)
    needle = '"--account=wpavan"'
    if source.count(needle) != 1:
        raise ValueError('Native submit implementation differs; adaptation refused')
    adapted = source.replace(needle,
        '"--account=" + ("hoogenboom" if qos == "hoogenboom" else "wpavan")')
    # Only account selection changes. Journal-before-sbatch, retries, arguments,
    # exclusions, worker scripts and simulation inputs retain native behavior.
    namespace = dict(legacy.submit.__globals__)
    exec(compile(adapted, '<account-routing-only>', 'exec'), namespace)
    legacy.submit = namespace['submit']
    state['budget_cpus'] = new
    state['account_routing_audit'] = {
        'native_submit_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'adapted_submit_sha256': hashlib.sha256(adapted.encode()).hexdigest(),
        'only_replacement': needle+' -> account selected from QOS',
        'hoogenboom_burst_enabled': False}


def handoff(old, controller):
    if not re.fullmatch(r'\d+', controller) or old[-1] != controller:
        raise ValueError('Replacement must match most recent registered controller')
    output = query(['squeue', '-h', '-u', getpass.getuser(), '-o', '%i|%u|%j|%T'])
    rows = [line.split('|') for line in output.splitlines()
            if line.split('|')[0].strip() == controller]
    if rows:
        if len(rows) != 1 or rows[0][:3] != [controller, getpass.getuser(), 'co2v2-controller-resume1']:
            raise ValueError('Controller identity does not match expected resume1 supervisor')
        receipt = GEN/'multi1_handoff.json'
        with receipt.open('x') as stream:
            json.dump({'controller_only': controller, 'workers_cancelled': False,
                       'utc': datetime.now(timezone.utc).isoformat()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        query(['scancel', controller])
        for attempt in range(12):
            live = query(['squeue', '-h', '-u', getpass.getuser(), '-o', '%i'])
            if controller not in {line.strip() for line in live.splitlines()}:
                break
            time.sleep(5)
        else:
            raise RuntimeError('Old controller has not stopped; no new controller submitted')
    print('HANDOFF: only previous controller stopped; workers retained.', flush=True)


def query(args):
    return subprocess.run(args, check=True, capture_output=True,
                          text=True, timeout=60).stdout


def lookup(name):
    # Both sources must respond successfully. Absence from one is insufficient.
    queue = query(['squeue', '-h', '-u', getpass.getuser(), '--name='+name,
                   '-o', '%F|%j'])
    acct = query(['sacct', '-X', '-n', '-P', '-S', '2026-11-19',
                  '-u', getpass.getuser(), '--name='+name,
                  '--format=JobID%100,JobName%150'])
    roots = set()
    for line in (queue+'\n'+acct).splitlines():
        fields = line.strip().split('|')
        if len(fields) >= 2 and fields[1].strip() == name:
            match = re.fullmatch(r'(\d+)(?:_.*)?', fields[0].strip())
            if not match:
                raise ValueError('Unrecognized scheduler ID: '+line)
            roots.add(match.group(1))
    if len(roots) > 1:
        raise ValueError('Multiple jobs share submission name: '+name)
    return next(iter(roots), None)


def rejected_connection(batch):
    return (not batch.get('submission_stdout', '').strip()
            and 'Batch job submission failed: Unable to contact slurm controller (connect failure)'
            in batch.get('submission_error', ''))


def reconcile(state, save):
    unresolved = False
    for country in state['countries']:
        for batch in list(country.get('batches', [])):
            if batch.get('job'):
                continue
            found = lookup(batch['name'])
            if found:
                batch['job'] = found
                batch['recovery_resolution'] = 'exact_name_found_in_scheduler'
                save(state)
                print('RECONCILED', batch['name'], found, flush=True)
                continue
            # Never recycle timeouts or other ambiguous submission responses.
            # An explicit connect rejection additionally needs two successful,
            # separated queue/accounting checks and an aged submission intent.
            stamp = datetime.fromisoformat(batch['submission_started_utc'])
            age = (datetime.now(timezone.utc)-stamp).total_seconds()
            checks = batch.setdefault('recovery_absence_checks', [])
            now = time.time()
            if not checks or now-checks[-1] >= 120:
                checks.append(now)
                save(state)
            if (rejected_connection(batch) and age >= 600 and len(checks) >= 2
                    and checks[-1]-checks[0] >= 120):
                country.setdefault('rejected_submissions', []).append({
                    **batch, 'resolution': 'connect_rejection_reconciled_queue_and_accounting',
                    'reconciled_utc': datetime.now(timezone.utc).isoformat()})
                country['batches'].remove(batch)
                save(state)
                print('REJECTED INTENT ARCHIVED', batch['name'], flush=True)
            else:
                print('WAITING RECONCILIATION', batch['name'], flush=True)
                unresolved = True
    return not unresolved


def transient(exc):
    if isinstance(exc, (subprocess.SubprocessError, ConnectionError)):
        return True
    message = str(exc).lower()
    return any(text in message for text in (
        'sbatch failed', 'unresolved sbatch submission', 'unresolved submission intent',
        'unable to contact slurm', 'socket timed out', 'slurm_load_jobs error'))


def restore_pending(state):
    roots = {str(b['job']) for c in state['countries']
             for b in c.get('batches', []) if b.get('job')}
    output = query(['squeue', '-r', '-h', '-u', getpass.getuser(),
                    '-t', 'PENDING', '-o', '%i|%l'])
    for line in output.splitlines():
        fields = line.strip().split('|')
        if len(fields) != 2:
            continue
        job, limit = fields
        if (re.fullmatch(r'\d+_\d+', job) and job.split('_')[0] in roots
                and limit in {'1:00:00', '2:00:00', '3:00:00',
                              '01:00:00', '02:00:00', '03:00:00'}):
            result = subprocess.run(['scontrol', 'update', 'JobId='+job,
                                     'TimeLimit=1440'], capture_output=True,
                                    text=True, timeout=60)
            print('RESTORE_PENDING_24H', job, result.returncode,
                  result.stderr.strip(), flush=True)
            if result.returncode:
                raise RuntimeError('Pending walltime restoration failed; inspect '+job)


def run():
    import fcntl
    if not os.environ.get('SLURM_JOB_ID'):
        raise RuntimeError('Use --launch; never run controller loop on login node')
    sys.path.insert(0, str(CODE/'scripts'))
    import common as c
    from controller import configure
    with c.STATE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = c.load(c.STATE)
        legacy = configure(state)
        legacy.check_frozen_inputs(state)
        backup = GEN/('state.before_multi1.'+os.environ['SLURM_JOB_ID']+'.json')
        with backup.open('x') as stream:
            json.dump(state, stream, indent=2)
        configure_accounts(state, legacy)
        state.setdefault('controllers', []).append(os.environ['SLURM_JOB_ID'])
        c.save(c.STATE, state)
        restored = False
        while True:
            state = c.load(c.STATE)
            try:
                if not reconcile(state, lambda s: c.save(c.STATE, s)):
                    time.sleep(30)
                    continue
                if not restored:
                    restore_pending(state)
                    restored = True
                state.pop('controller_error', None)
                c.save(c.STATE, state)
                more = legacy.tick(state, c.STATE)
                if not more:
                    print(legacy.summary(state), flush=True)
                    return
            except Exception as exc:
                # tick journals before submission. Reload rather than overwrite
                # that durable record with a stale in-memory state.
                current = c.load(c.STATE)
                current['controller_error'] = str(exc)
                c.save(c.STATE, current)
                print('SUPERVISOR', type(exc).__name__, str(exc), flush=True)
                if not transient(exc):
                    raise
            time.sleep(120)


def launch(replace_controller=None):
    import fcntl
    with (GEN/'production_submission.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = json.loads((GEN/'state.json').read_text())
        proof = json.loads((GEN/'hoogenboom_access_preflight_v1.json').read_text())
        age = (datetime.now(timezone.utc)-datetime.fromisoformat(proof['utc'])).total_seconds()
        if (proof.get('status') != 'ok' or proof.get('account') != 'hoogenboom'
                or proof.get('uid') != os.getuid() or not 0 <= age <= 86400
                or proof.get('results') != state['results']
                or proof.get('helper_sha256') != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                or proof.get('executable_sha256') != state['executable_sha256']
                or len(proof.get('countries', [])) != len(state['countries'])):
            raise ValueError('Missing, stale or incompatible hoogenboom compute-node preflight')
        old = state.get('controllers', [])
        if not old:
            raise ValueError('Missing previous controller history')
        if replace_controller:
            handoff(old, replace_controller)
        output = query(['sacct', '-X', '-n', '-P', '-j', ','.join(old),
                        '--format=JobID%100,State%30'])
        statuses = {}
        for line in output.splitlines():
            fields = line.strip().split('|')
            if len(fields) >= 2 and fields[1].strip():
                statuses[fields[0].strip()] = fields[1].split()[0].rstrip('+')
        if any(statuses.get(job) not in TERMINAL for job in old):
            raise RuntimeError('Previous controller active or accounting unavailable')
        if lookup(NAME):
            raise RuntimeError('Resume controller already exists; do not duplicate')
        helper = Path(__file__).resolve()
        body = ('set -euo pipefail\nmodule purge\nmodule load gcc/11.4.0\n'
                'module load python/3.11\n'
                'source /blue/wpavan/gustavo.luca/SBrust_world/venvs/pythia/bin/activate\n'
                'exec python '+shlex.quote(str(helper))+' --run\n')
        script = GEN/'controller_multi1.slurm'
        with script.open('x') as stream:
            stream.write('#!/bin/bash\n'+body)
        cmd = ['sbatch', '--parsable', '--account=wpavan', '--qos=wpavan',
               '--no-requeue', '--job-name='+NAME, '--cpus-per-task=1',
               '--mem=4G', '--time=168:00:00', '--exclude=c0706a-s23,c0706a-s27',
               '--chdir='+str(ROOT),
               '--output='+str(ROOT/'logs/co2v2-controller-%j.out'),
               '--error='+str(ROOT/'logs/co2v2-controller-%j.err'), str(script)]
        receipt = GEN/'controller_multi1_submission.json'
        with receipt.open('x') as stream:
            json.dump({'command': cmd, 'intent_utc': datetime.now(timezone.utc).isoformat()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        # No retry if acceptance is unknown. Preserve receipt and script.
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        with (GEN/'controller_multi1_response.json').open('x') as stream:
            json.dump({'returncode': result.returncode, 'stdout': result.stdout,
                       'stderr': result.stderr}, stream, indent=2)
        if result.returncode or not re.fullmatch(r'\d+(?:;[^\s]+)?', result.stdout.strip()):
            raise RuntimeError('Submission requires reconciliation: '+result.stderr+result.stdout)
        print('RESUME_SUBMITTED', result.stdout.strip())
        print('Budgets: wpavan 89 + controller 1; hoogenboom 200; wpavan-b 810.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--launch', action='store_true')
    group.add_argument('--run', action='store_true')
    parser.add_argument('--replace-controller')
    args = parser.parse_args()
    launch(args.replace_controller) if args.launch else run()
