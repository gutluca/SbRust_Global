"""Download, verify and extract the unchanged 103-country raw master archive."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import time
from urllib.request import Request,urlopen


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    repo=Path(__file__).resolve().parents[1]
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=repo/'data/raw')
    p.add_argument('--archive-only',action='store_true')
    args=p.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    entry=json.loads((repo/'provenance/paired_master_archive.json').read_text(encoding='utf-8'))
    url=entry.get('download_url')
    if not url:raise RuntimeError('The public archive download URL has not been registered.')
    archive=out/entry['file']
    if not archive.exists():
        partial=archive.with_name(archive.name+'.partial')
        if partial.exists():raise FileExistsError(f'Previous partial download exists: {partial}')
        request=Request(url,headers={'User-Agent':'SbRust-Global-data-download'})
        count=0;last=time.monotonic()
        with urlopen(request,timeout=120) as source,partial.open('xb') as target:
            for chunk in iter(lambda:source.read(8*1024*1024),b''):
                target.write(chunk);count+=len(chunk)
                if time.monotonic()-last>15:
                    print(f'Downloaded {count:,}/{entry["bytes"]:,} bytes',flush=True);last=time.monotonic()
        if partial.stat().st_size!=entry['bytes'] or sha(partial)!=entry['sha256']:
            raise ValueError('Archive size/checksum mismatch; the partial file was not installed.')
        partial.rename(archive)
    if archive.stat().st_size!=entry['bytes'] or sha(archive)!=entry['sha256']:
        raise ValueError('The local archive differs from the registered checksum.')
    print('Archive SHA-256 verified.',flush=True)
    if args.archive_only:return
    if (out/'countries').exists():raise FileExistsError('Country output already exists; use another extraction directory.')
    files=json.loads((repo/'provenance/master_files.json').read_text(encoding='utf-8'))
    allowed={a['path'] for a in files}|{'master_files.json','data_dictionary.md'}
    with tarfile.open(archive,'r:') as source:
        members=source.getmembers()
        if len(members)!=len(allowed) or {m.name for m in members}!=allowed or not all(m.isfile() for m in members):
            raise ValueError('The archive member list differs from the frozen data manifest.')
        source.extractall(out,members=members,filter='data')
    for a in files:
        path=out/a['path']
        if path.stat().st_size!=a['bytes'] or sha(path)!=a['sha256']:
            raise ValueError(f'Country checksum mismatch: {a["country"]}')
    print(f'PASS: {len(files)} original country masters extracted and verified in {out}')


if __name__=='__main__':main()
