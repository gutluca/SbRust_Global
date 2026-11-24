"""Lightweight, read-only campaign status; safe on a login node."""
from collections import Counter
import getpass
from pathlib import Path
import subprocess
import common as c


def main():
    journal_path = c.BASE / 'logs' / (c.VERSION + '_submission.json')
    if not journal_path.exists():
        print('No submission journal exists yet.'); return
    journal = c.load(journal_path)
    jobs = journal['jobs']
    print('CAMPAIGN', c.VERSION)
    for label, entry in jobs.items():
        print('JOB', label, entry.get('job') or 'UNRESOLVED', entry.get('error', ''))
    ids = ','.join(str(e['job']) for e in jobs.values() if e.get('job'))
    if ids:
        result = subprocess.run(['sacct','-X','-n','-P','-j',ids,'--format=JobID,JobName%30,State%32,ExitCode,Elapsed,NodeList%64'],
                                capture_output=True,text=True,timeout=30)
        tally=Counter();failures=[]
        for line in result.stdout.splitlines():
            fields=line.split('|')
            if len(fields)<6:continue
            job,name,status,exitcode,elapsed,node=map(str.strip,fields[:6])
            tally[(name,status)]+=1
            if status.split()[0] in ('FAILED','CANCELLED','TIMEOUT','OUT_OF_MEMORY','NODE_FAIL','BOOT_FAIL'):
                failures.append((job,name,status,exitcode,node))
        for key,count in sorted(tally.items()): print('SLURM',*key,'records',count)
        for failure in failures[:12]: print('FAILED_JOB',*failure)
        if result.returncode:print(result.stderr)
    queue = subprocess.run(['squeue','-r','-h','-u',getpass.getuser(),'-o','%q|%C|%T'],
                           capture_output=True,text=True,timeout=30)
    counts, cpus = Counter(), Counter()
    for line in queue.stdout.splitlines():
        qos, count, status = line.split('|')
        counts[(qos,status)] += 1; cpus[(qos,status)] += int(count)
    for key in sorted(counts):
        print('USER_QUEUE', *key, 'tasks', counts[key], 'requested_cpus', cpus[key])
    if c.STATE.exists():
        state = c.load(c.STATE)
        print('GLOBAL_STATUS',state['status'],dict(Counter(x['status'] for x in state['countries'])))
        prepared=sum((Path(state['output'])/x['key']/'production/reuse_audit.json').is_file() for x in state['countries'])
        print('PREPARED_COUNTRIES',prepared,'/',len(state['countries']))
        folders=list((c.RESULTS/'pilot/tasks').glob('*'))
        print('PILOT_SHARDS',len(folders),'/',c.PILOT_CONFIGS,
              'REPLAY_AUDITS',sum((p/'replay_audit.json').exists() for p in folders),'/',c.PILOT_CASES)
        for country in state['countries']:
            if country.get('error'):
                print('COUNTRY_ERROR',country['country'],country['error'])
        if state.get('controller_error'):
            print('CONTROLLER_ERROR',state['controller_error'])
    for name in ('bootstrap_complete.json','pilot_passed.json'):
        path = c.GEN / name
        if path.exists(): print(name,c.load(path))
    # Failed startup evidence is bounded; raw model failures have dedicated files.
    for label in ('bootstrap','gate','controller'):
        job = jobs.get(label,{}).get('job')
        if not job: continue
        path = c.BASE / 'logs' / f'co2v2-{label}-{job}.err'
        if path.exists() and path.stat().st_size:
            print('ERROR_LOG',path)
            with path.open('rb') as stream:
                stream.seek(max(0,path.stat().st_size-6000))
                print(stream.read().decode(errors='replace'))
    shown=0
    for label in ('preflight','pilot'):
        job=jobs.get(label,{}).get('job')
        if not job:continue
        for path in sorted((c.BASE/'logs').glob(f'co2v2-{label}-{job}_*.err')):
            if path.stat().st_size and shown<5:
                shown+=1;print('ARRAY_ERROR_LOG',path)
                with path.open('rb') as stream:
                    stream.seek(max(0,path.stat().st_size-3000));print(stream.read().decode(errors='replace'))


if __name__ == '__main__': main()
