"""Checksums identify frozen research assets; Git versions editable code/docs."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path, PurePosixPath


def is_frozen_asset(relative):
    path=PurePosixPath(relative)
    parts=path.parts
    if not parts or '__pycache__' in parts or path.suffix.lower() in {'.md','.rst'}:
        return False
    if parts[0]=='data':
        return len(parts)>1 and parts[1]!='raw'
    if parts[0]=='model':
        return len(parts)>1 and (parts[1] in {'bin','source','runtime'} or path.name=='executable.json')
    if parts[0]=='examples':
        return len(parts)>1 and (parts[1]=='data' or str(path)=='examples/expected.json')
    return parts[0]=='provenance' and path.suffix=='.json' and path.name!='repository_files.json'


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def asset_paths(repo):
    return sorted(p for name in ('data','model','examples','provenance')
                  for p in (repo/name).rglob('*')
                  if p.is_file() and is_frozen_asset(p.relative_to(repo).as_posix()))


def update_inventory(repo):
    items=[{'path':p.relative_to(repo).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)}
           for p in asset_paths(repo)]
    inventory=repo/'provenance/repository_files.json'
    inventory.parent.mkdir(parents=True,exist_ok=True)
    inventory.write_text(json.dumps(items,indent=2)+'\n',encoding='utf-8',newline='\n')
    paths=asset_paths(repo)+[inventory]
    (repo/'SHA256SUMS.txt').write_text(''.join(sha(p)+'  '+p.relative_to(repo).as_posix()+'\n'
                                             for p in sorted(paths)),encoding='utf-8',newline='\n')
    return len(items)


def verify_inventory(repo):
    inventory=repo/'provenance/repository_files.json'
    items=json.loads(inventory.read_text(encoding='utf-8'))
    expected={item['path'] for item in items}
    actual={p.relative_to(repo).as_posix() for p in asset_paths(repo)}
    if expected!=actual:
        raise AssertionError(f'Frozen asset inventory differs: missing={sorted(expected-actual)}, unregistered={sorted(actual-expected)}')
    for item in items:
        path=repo/item['path']
        if path.stat().st_size!=item['bytes'] or sha(path)!=item['sha256']:
            raise AssertionError(f'Frozen asset checksum differs: {item["path"]}')
