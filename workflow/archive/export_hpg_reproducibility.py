"""Read-only export of a bounded production snapshot from the original HPG project.

Does not submit, cancel or alter campaign jobs. Requires Python 3.9+.
The application campaign must be finished before using --include-applications.
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import tarfile

VERSION='co2_future_global_20261119_v2'
APPS='onset_dsv_co2_20261123_v1'
EXE_SHA='811a53c4d85d2f47ecf7815d9c46bc75fbc5fe5dbd2dd26aae3fce606f2ba41e'


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(4*1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('/blue/wpavan/gustavo.luca/SBrust_world'))
    parser.add_argument('--output',type=Path,required=True,help='New archive path, outside production directories')
    parser.add_argument('--include-applications',action='store_true')
    parser.add_argument('--include-soil-files',action='store_true',help='Also package source country soil-profile files; may substantially increase archive size')
    args=parser.parse_args();root=args.root.resolve();out=args.output.resolve()
    gen=root/'generated/world'/VERSION
    state_path=gen/'state.json'
    state=json.loads(state_path.read_text())
    if state.get('status')!='finished':raise ValueError('Yield campaign is not finished')
    exe=Path(state['executable']).resolve()
    if not exe.is_relative_to(root) or sha(exe)!=EXE_SHA:raise ValueError('Unexpected executable or checksum')
    if out.exists() or out.with_name(out.name+'.partial').exists():raise FileExistsError(out)
    if out.is_relative_to(gen) or out.is_relative_to(root/'results'):raise ValueError('Use a separate deposit directory')
    files={};missing=[];calendar_proofs={};external_inputs=[]

    def add(path,required=False):
        p=Path(path)
        if not p.is_absolute():p=root/p
        if not p.is_file():
            missing.append(str(p.relative_to(root)) if p.is_relative_to(root) else p.name)
            if required:raise FileNotFoundError(p)
            return
        resolved=p.resolve()
        if not resolved.is_relative_to(root):raise ValueError(f'File resolves outside the project: {p.name}')
        files[p.relative_to(root).as_posix()]=p

    def tree(path,suffixes=None):
        p=Path(path)
        if not p.is_absolute():p=root/p
        if not p.is_dir():missing.append(str(p.relative_to(root)));return
        for child in sorted(p.rglob('*')):
            if any(part in ('__pycache__','.git','.venv','work') for part in child.relative_to(p).parts):continue
            if child.is_file() and (suffixes is None or child.suffix.lower() in suffixes):add(child)

    add(exe,True)
    tree(Path(state['engine']))
    tree(root/'releases'/VERSION, {'.py','.sh','.slurm','.json','.wda','.txt','.csv','.j2'})
    tree('config/regional_inputs_20261022')
    for rel in ['apps/dssat-dismo-4.8/DSSATPRO.L48','apps/dssat-dismo-4.8/license.txt',
                'apps/dssat-dismo-4.8/NOTICE.md','apps/dssat-dismo-4.8/DETAIL.CDE',
                'apps/dssat-dismo-4.8/MODEL.ERR']:
        add(rel)
    for rel in ['data/dssat48/Genotype','data/dssat48/StandardData']:
        tree(rel,{'.cul','.eco','.spe','.cde','.sol','.SOL','.txt','.inp','.l48'})
    soil_root=root/'data/spatial/eGHR'
    if args.include_soil_files:
        tree(soil_root,{'.sol','.txt'})
    elif soil_root.is_dir():
        external_inputs.extend({'kind':'soil_profile','path':p.relative_to(root).as_posix(),
                                'bytes':p.stat().st_size,'included':False}
                               for p in sorted(soil_root.rglob('*')) if p.is_file() and p.suffix.lower()=='.sol')
    for c in state['countries']:
        base=gen/c['key']
        for name in ['project.json','production/calibrated.gpkg','production/gm_frozen.csv',
                     'production/reuse_audit.json']:
            add(base/name)
        tree(base/'production/zone_sites',{'.gpkg'})
        audit_path=base/'production/reuse_audit.json'
        if audit_path.is_file():
            audit=json.loads(audit_path.read_text())
            for path,digest in audit.get('executed_calendar_sha256',{}).items():
                if path in calendar_proofs and calendar_proofs[path]!=digest:
                    raise ValueError('Inconsistent executed calendar checksum')
                calendar_proofs[path]=digest
        original=c.get('original',{})
        if original.get('project'):add(Path(original['project']))
        if c.get('final_project'):add(Path(c['final_project']))
        # Capture the submitted annual configuration JSONs, not scratch/output trees.
        tree(base/'production/annual_configs',{'.json','.txt'})
    if args.include_applications:
        complete=root/'results/world'/APPS/'complete.json'
        record=json.loads(complete.read_text())
        if record.get('status') not in ('completed','finished'):raise ValueError('Application campaign is not complete')
        tree(root/'releases'/APPS,{'.py','.sh','.slurm','.json','.txt','.j2'})
        add(complete,True)
    environment={'python':platform.python_version(),'platform':platform.platform(),'pip_freeze':None}
    proc=subprocess.run([sys.executable,'-m','pip','freeze'],text=True,capture_output=True)
    if proc.returncode==0:environment['pip_freeze']=proc.stdout.splitlines()
    manifest=[]
    out.parent.mkdir(parents=True,exist_ok=True)
    partial=out.with_name(out.name+'.partial')
    with tarfile.open(partial,'x:gz') as archive:
        for i,(rel,p) in enumerate(sorted(files.items()),1):
            digest=sha(p);manifest.append({'path':rel,'bytes':p.stat().st_size,'sha256':digest})
            archive.add(p,arcname=rel,recursive=False)
            if i%1000==0:print(f'Archived {i}/{len(files)} files',flush=True)
        payload=json.dumps({'campaign':VERSION,'executable_sha256':EXE_SHA,'files':manifest,
                            'missing_optional_files':missing,'export_environment':environment,
                            'executed_calendar_checksums':calendar_proofs,'external_inputs_not_packaged':external_inputs,
                            'note':'Export environment is not necessarily the original model build environment. Raw weather/raster/calendar source catalog and exact model commit still require documentation.'},indent=2).encode()
        info=tarfile.TarInfo('EXPORT_MANIFEST.json');info.size=len(payload);archive.addfile(info,io.BytesIO(payload))
    partial.rename(out)
    receipt={'archive':out.name,'bytes':out.stat().st_size,'sha256':sha(out),'files':len(files),
             'missing_optional_files':missing,'status':'exported_not_published'}
    out.with_name(out.name+'.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt,indent=2))


if __name__=='__main__':
    main()
