"""Package original compressed masters for an archival data repository.

Input is read-only. Only files listed in the public manifest are archived.
Every SHA-256 is checked before packaging; existing archives are not replaced.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import tarfile


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    repo=Path(__file__).resolve().parents[2]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    if args.output.exists():raise FileExistsError(args.output)
    files=json.loads((repo/'provenance/master_files.json').read_text(encoding='utf-8'))
    for i,entry in enumerate(files,1):
        source=args.data_root/entry['path']
        if source.stat().st_size!=entry['bytes'] or sha(source)!=entry['sha256']:
            raise ValueError(f"Master differs from frozen manifest: {entry['path']}")
        print(f"Verified {i}/{len(files)} {entry['country']}",flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    partial=args.output.with_name(args.output.name+'.partial')
    if partial.exists():raise FileExistsError(partial)
    with tarfile.open(partial,'x') as archive:
        for entry in files:
            archive.add(args.data_root/entry['path'],arcname=entry['path'],recursive=False)
        archive.add(repo/'provenance/master_files.json',arcname='master_files.json',recursive=False)
        archive.add(repo/'data/master_columns.csv',arcname='master_columns.csv',recursive=False)
    partial.rename(args.output)
    receipt=dict(file=args.output.name,sha256=sha(args.output),bytes=args.output.stat().st_size,
                 countries=len(files),paired_rows=sum(a['rows'] for a in files),format='tar of unchanged gzip CSVs')
    args.output.with_suffix(args.output.suffix+'.json').write_text(json.dumps(receipt,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(receipt,indent=2))


if __name__=='__main__':
    main()
