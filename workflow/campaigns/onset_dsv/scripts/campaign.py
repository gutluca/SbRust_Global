"""Initialize and supervise one resumable, multi-account applications campaign."""
import argparse
from datetime import datetime,timezone
import getpass
import inspect
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import common as c
import world_onset_runtime as r

PHASES={'preflight':('prepare','pilot'),'production':('annual_first','annual_rest','master'),'analysis':('analysis',)}

def initialize():
    c.compute_only()
    if c.STATE.exists(): return c.load(c.STATE)
    if c.RESULTS.exists(): raise FileExistsError('New results path already exists without a campaign state')
    source=c.load(c.SOURCE)
    expected=c.load(c.PACK/'inputs/expected_masters.json')
    if source['status']!='finished' or len(source['countries'])!=103 or set(expected)!={v['country'] for v in source['countries']}:
        raise ValueError('Corrected production campaign is not finished for all 103 countries')
    if c.load(c.OLD/'complete.json')['status']!='completed': raise ValueError('Old applications campaign incomplete')
    c.verify(source['executable'],c.EXE_SHA)
    countries=[]
    for rec in source['countries']:
        if rec['status']!='completed': raise ValueError('Incomplete source country')
        stem=c.safe_name(rec['country']); folder=Path(source['results'])/rec['key']
        master=folder/('summary_master_'+stem+'.csv.gz'); receipt=folder/'complete.json'; info=c.load(receipt)
        if (info['status']!='ok' or info['output_sha256']!=expected[rec['country']]['sha256'] or
            info['points']!=rec['site_count'] or not info.get('historical_rows_copied_verbatim')):
            raise ValueError('Source differs from the locally verified final paper master: '+rec['country'])
        compact={k:v for k,v in rec.items() if k not in ('history','batches','error','stage','status')}
        compact.update(status='pending',stage=0,history={},batches=[],current_master=str(master),
                       current_master_sha256=info['output_sha256'],current_complete=str(receipt))
        countries.append(compact)
    c.GEN.mkdir(parents=True,exist_ok=True); c.RESULTS.mkdir(parents=True,exist_ok=False)
    files=[p for p in (c.PACK/'scripts').glob('*.py')]
    engine=Path(source['engine'])
    for name in ('prepare_scratch_config.py','materialize_weather_window.py','prepare_ggcmi_calendar.py',
                 'collate_summary.py','export_analysis_table.py'):
        files.append(engine/'scripts'/name)
    files += [engine/'templates/SBG00001.SBX',c.PACK/'inputs/old_sources.json',c.PACK/'inputs/expected_masters.json',
              Path(source['pack'])/'inputs/population.csv']
    for rec in countries:
        files.extend([Path(rec['current_complete']),Path(source['output'])/rec['key']/'production/reuse_audit.json'])
    for p in (c.PACK/'slurm').glob('*.slurm'): files.append(p)
    state=dict(version=1,root=str(c.ROOT),code=str(c.PACK),pack=str(c.PACK),output=str(c.GEN),results=str(c.RESULTS),
        source_output=source['output'],source_pack=source['pack'],source_results=source['results'],
        source_state_sha256=c.sha(c.SOURCE),engine=source['engine'],executable=source['executable'],executable_sha256=c.EXE_SHA,
        reference_co2=source['reference_co2'],countries=countries,phase='preflight',stages=list(PHASES['preflight']),
        budget_cpus={'wpavan':89,'hoogenboom':200,'wpavan-b':810},max_active_countries=12,status='pending',
        excluded_nodes=list(c.BAD_NODES),frozen_files=[dict(path=str(p),sha256=c.sha(p)) for p in files],
        policy=r.POLICY,created_utc=datetime.now(timezone.utc).isoformat())
    c.save(c.STATE,state); return state

def check_inputs(state):
    if state['version']!=1 or state['policy']!=r.POLICY or state['excluded_nodes']!=list(c.BAD_NODES):
        raise ValueError('Frozen campaign contract differs')
    c.verify(state['executable'],c.EXE_SHA)
    for record in state['frozen_files']: c.verify(record['path'],record['sha256'])

def specification(state,rec):
    stage=state['stages'][rec['stage']]; args=[c.STATE,rec['key'],stage]
    folder=c.ready(state,rec)
    if stage=='prepare': return 'job.slurm',args,1,[0],12
    if stage in ('pilot','annual_first','annual_rest'):
        info=c.load(folder/'complete.json')
        n=info['pilots'] if stage=='pilot' else info['bundles']
        indices=list(range(n)) if stage=='pilot' else [0] if stage=='annual_first' else list(range(1,n))
        return 'job.slurm',args,4,indices,24
    if stage in ('master','analysis'): return 'job.slurm',args,1,[0],24
    raise ValueError(stage)

def published(state,rec,index):
    from worker import valid
    folder=c.ready(state,rec); stage=state['stages'][rec['stage']]
    if stage=='prepare':
        from prepare import verify_prepared
        if not (folder/'complete.json').is_file(): return False
        verify_prepared(folder); return True
    if stage in ('pilot','annual_first','annual_rest'):
        name='pilots.json' if stage=='pilot' else 'bundles.json'
        if not (folder/name).is_file(): return False
        tasks=c.load(folder/name)[index]; kind='pilots' if stage=='pilot' else 'tasks'
        return all(valid(c.result(state,rec)/kind/t['name'],t,full=False) for t in tasks)
    marker=(c.result(state,rec)/'complete.json') if stage=='master' else Path(state['results'])/'analysis'/rec['key']/'complete.json'
    if not marker.is_file(): return False
    info=c.load(marker)
    return info.get('status')=='completed' and info['country']==rec['country']

def configure(state):
    import legacy_dispatcher as legacy
    import scheduler_recovery as recovery
    legacy.STAGES=tuple(state['stages'])
    legacy.ARRAY_STAGES={'pilot','annual_first','annual_rest'}
    legacy.BURST_STAGES={'pilot','annual_first','annual_rest'}
    legacy.ATTEMPT_LIMITS={v:2 for v in state['stages']}
    legacy.specification=specification; legacy.check_frozen_inputs=check_inputs; legacy.recover_published=published
    if not hasattr(legacy,'_native_submit_source'):
        legacy._native_submit_source=inspect.getsource(legacy.submit)
    native=legacy._native_submit_source
    needle='"--account=wpavan"'
    if native.count(needle)!=1: raise ValueError('Native account routing contract changed')
    scope=dict(legacy.submit.__globals__)
    exec(compile(native.replace(needle,'"--account=" + ("hoogenboom" if qos == "hoogenboom" else "wpavan")'),'<account-routing-only>','exec'),scope)
    legacy.submit=scope['submit']
    def account_reservations(current):
        raw=recovery.query(['squeue','-r','-h','-A','wpavan,hoogenboom','-o','%i|%q|%C'])
        rows={}
        for line in raw.splitlines():
            job,qos,cpus=[v.strip() for v in line.split('|')]
            if job==os.environ.get('SLURM_JOB_ID') or qos not in current['budget_cpus']: continue
            rows[job]=(qos,int(cpus))
        used={q:sum(v for q0,v in rows.values() if q0==q) for q in current['budget_cpus']}
        # Account-wide visible jobs plus own journaled tasks not yet visible.
        for rec in current['countries']:
            for batch in rec['batches']:
                for i in batch['indices']:
                    job=str(batch['job'])+('_'+str(i) if batch['array'] else '')
                    if not legacy.terminal(batch,i) and job not in rows: used[batch['qos']]+=batch['cpus']
        return used
    legacy.reservations=account_reservations
    os.environ['SBATCH_EXCLUDE']=','.join(c.BAD_NODES)
    return legacy,recovery

def transition(state):
    if state['status']!='finished': return False
    if state['phase']=='preflight':
        from prepare import verify_prepared
        from worker import valid
        if len(state['countries'])!=103: raise ValueError('Global pilot requires all 103 countries')
        pilots=0
        for rec in state['countries']:
            folder=c.ready(state,rec); info=verify_prepared(folder)
            for batch in c.load(folder/'pilots.json'):
                for task in batch:
                    if not valid(c.result(state,rec)/'pilots'/task['name'],task): raise ValueError('Global pilot gate failed')
                    pilots+=1
        c.save(c.GEN/'pilot_passed.json',dict(status='ok',countries=103,pilot_configurations=pilots,
            checks='every pilot: exact current Y0, planting, GM, weather cell; every realized daily CO2 value; full five plantings',
            policy=r.POLICY))
        next_phase='production'
    elif state['phase']=='production':
        prepare_analysis(state); next_phase='analysis'
    else:
        import analyze_world_onset as analysis
        analysis.report(Path(state['results'])/'analysis')
        countries=[c.load(c.result(state,rec)/'complete.json') for rec in state['countries']]
        if len(countries)!=103 or sum(x['rows'] for x in countries)!=76415200: raise ValueError('Global population differs')
        c.save(Path(state['results'])/'complete.json',dict(status='completed',countries=countries,policy=r.POLICY,
            rows=sum(x['rows'] for x in countries),historical_reused_rows=sum(x['historical_reused_rows'] for x in countries),
            historical_rerun_rows=sum(x['historical_rerun_rows'] for x in countries),
            analysis=str(Path(state['results'])/'analysis'),source_co2_campaign=state['source_results']))
        print('GLOBAL_COMPLETE',state['results'],flush=True); return False
    old_phase=state['phase']
    for rec in state['countries']:
        rec.setdefault('phase_history',{})[old_phase]=rec['history']; rec.update(status='pending',stage=0,history={},batches=[])
        rec.pop('error',None)
    state.update(phase=next_phase,stages=list(PHASES[next_phase]),status='pending',turn=0)
    c.save(c.STATE,state); print('PHASE',next_phase,flush=True); return True

def prepare_analysis(state):
    out=Path(state['results'])/'analysis'
    if (out/'plan.json').is_file(): return
    population={'retained':{},'excluded':{}}; countries=[]
    for rec in state['countries']:
        info=c.load(c.ready(state,rec)/'index/metadata.json')
        population['retained'][rec['country']]={cell:dict(latitude=values[2],longitude=values[3],
            harvest_area_ha=values[4]) for cell,values in info['static'].items()}
        population['excluded'][rec['country']]={}
        marker=c.result(state,rec)/'complete.json'; done=c.load(marker)
        countries.append(dict(country=rec['country'],folder=rec['key'],master=done['master'],sha256=done['sha256'],
            marker=str(marker),marker_sha256=c.sha(marker),points=done['points'],rows=done['rows']))
    c.save(out/'population.json',population)
    c.save(out/'plan.json',dict(source=state['results'],countries=countries,population_sha256=c.sha(out/'population.json'),
        scope='all 103 countries, corrected CO2, current fixed GM, unchanged application protocol'))

def run():
    import fcntl
    c.compute_only(); c.GEN.mkdir(parents=True,exist_ok=True)
    with (c.GEN/'controller.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state=initialize(); check_inputs(state)
        state.setdefault('controllers',[]).append(os.environ['SLURM_JOB_ID']); c.save(c.STATE,state)
        legacy,recovery=configure(state)
        while True:
            try:
                state=c.load(c.STATE)
                if not recovery.reconcile(state,lambda x:c.save(c.STATE,x)):
                    time.sleep(30); continue
                more=legacy.tick(state,c.STATE)
                if not more:
                    if transition(state): legacy,recovery=configure(state); continue
                    print(legacy.summary(state),flush=True); return
            except Exception as exc:
                current=c.load(c.STATE); current['controller_error']=str(exc); c.save(c.STATE,current)
                print('SUPERVISOR',type(exc).__name__,str(exc),flush=True)
                if not recovery.transient(exc): raise
            time.sleep(120)

def launch(resume=False):
    import fcntl
    import scheduler_recovery as recovery
    c.GEN.mkdir(parents=True,exist_ok=True); (c.ROOT/'logs').mkdir(exist_ok=True)
    with (c.GEN/'launch.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        intent=c.GEN/'launch.json'
        if intent.exists() and not resume:
            previous=c.load(intent); job=recovery.lookup(previous['name'])
            print('PREVIOUS_LAUNCH',job or 'submission unresolved; inspect launch receipt','no new submission'); return
        if resume:
            state=c.load(c.STATE)
            if state['status'] in ('finished_with_failures','finished'):
                raise ValueError('Inspect finished/failed phase before authorizing a targeted retry')
            controllers=set(state.get('controllers',[]))
            receipts=[p for p in c.GEN.glob('launch*.json') if re.fullmatch(r'launch(?:_resume_\d+)?\.json',p.name)]
            for path in receipts:
                previous=c.load(path); found=recovery.lookup(previous['name'])
                if not found: raise ValueError('Unresolved previous controller submission: '+previous['name'])
                controllers.add(found)
            for job in controllers:
                raw=recovery.query(['sacct','-X','-n','-P','-j',job,'--format=JobID%100,State%30'])
                matches=[line.split('|')[1].split()[0].rstrip('+') for line in raw.splitlines() if line.split('|')[0].strip()==job]
                if len(matches)!=1 or matches[0] not in recovery.TERMINAL: raise ValueError('A controller is active or accounting is unavailable')
            attempt=len(list(c.GEN.glob('launch_resume_*.json'))); intent=c.GEN/f'launch_resume_{attempt}.json'
        name='appsco2-'+str(int(time.time()))+'-'+str(os.getpid())
        cmd=['sbatch','--parsable','--account=wpavan','--qos=wpavan','--job-name='+name,'--cpus-per-task=1',
            '--mem=4G','--time=168:00:00','--no-requeue','--exclude='+','.join(c.BAD_NODES),
            '--chdir='+str(c.ROOT),'--output='+str(c.ROOT/'logs/appsco2-controller-%j.out'),
            '--error='+str(c.ROOT/'logs/appsco2-controller-%j.err'),str(c.PACK/'slurm/controller.slurm')]
        c.save(intent,dict(name=name,command=cmd,utc=datetime.now(timezone.utc).isoformat()))
        result=subprocess.run(cmd,capture_output=True,text=True,timeout=60)
        c.save(intent.with_suffix('.response.json'),dict(returncode=result.returncode,stdout=result.stdout,stderr=result.stderr))
        if result.returncode or not re.fullmatch(r'\d+(?:;[^\s]+)?',result.stdout.strip()):
            raise RuntimeError('Submission needs reconciliation: '+result.stdout+result.stderr)
        print('CONTROLLER_SUBMITTED',result.stdout.strip(),'automatic preflight -> production -> analysis',flush=True)

def status():
    if not c.STATE.is_file(): print('Campaign state not initialized yet'); return
    state=c.load(c.STATE); print('PHASE',state['phase'],'STATUS',state['status'])
    from collections import Counter
    print('COUNTRIES',dict(Counter(x['status'] for x in state['countries'])))
    for rec in state['countries']:
        stage=state['stages'][rec['stage']] if rec['stage']<len(state['stages']) else 'done'
        info=c.ready(state,rec)/'complete.json'; reuse=c.load(info)['historical_reused_rows'] if info.is_file() else '?'
        print(rec['country'],'|',rec['status'],'|',stage,'| historical reused',reuse,'|',rec.get('error',''))
    print('GLOBAL_COMPLETE',(Path(state['results'])/'complete.json').is_file())
    if state.get('controller_error'): print('CONTROLLER_ERROR',state['controller_error'])

def job(state_path,key,action,index):
    c.compute_only()
    if action=='prepare':
        from prepare import prepare
        prepare(state_path,key)
    elif action in ('pilot','annual_first','annual_rest'):
        from worker import bundle
        bundle(state_path,key,index,action=='pilot')
    elif action=='master':
        from merge import merge
        merge(state_path,key)
    elif action=='analysis':
        import analyze_world_onset as analysis
        state=c.load(state_path); out=Path(state['results'])/'analysis'; dest=out/key
        if (dest/'complete.json').is_file(): return
        if dest.exists(): dest.rename(out/(key+'.failed.'+os.environ['SLURM_JOB_ID']))
        i=next(i for i,x in enumerate(state['countries']) if x['key']==key); analysis.analyze(out,i)
    else: raise ValueError(action)

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('action',choices=['launch','resume','run','status','job'])
    parser.add_argument('arguments',nargs='*'); args=parser.parse_args()
    if args.action in ('launch','resume'): launch(args.action=='resume')
    elif args.action=='run': run()
    elif args.action=='status': status()
    else: job(Path(args.arguments[0]),args.arguments[1],args.arguments[2],int(args.arguments[3]))
