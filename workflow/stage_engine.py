"""Assemble the archived scripts/templates/config layout for model execution."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path,help='New engine directory')
    args=parser.parse_args()
    source=Path(__file__).resolve().parent
    target=args.output.resolve()
    if target.exists():raise FileExistsError(target)
    if target.is_relative_to(source):raise ValueError('Use an engine directory outside workflow/')
    pairs=[(p,Path('scripts')/p.name) for p in sorted((source/'production_reference').glob('*.py'))]
    pairs += [(p,p.relative_to(source)) for folder in ('templates','config')
              for p in sorted((source/folder).rglob('*')) if p.is_file()]
    target.mkdir(parents=True)
    records=[]
    for path,relative in pairs:
        destination=target/relative;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,destination)
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        with destination.open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==digest
        records.append({'path':relative.as_posix(),'sha256':digest})
    (target/'ENGINE_FILES.json').write_text(json.dumps(records,indent=2)+'\n',encoding='utf-8',newline='\n')
    print(f'Prepared {len(records)} engine files in {target}')


if __name__=='__main__':main()
