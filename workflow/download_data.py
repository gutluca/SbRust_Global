"""Download unchanged country masters and verify every original SHA-256."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib
import json
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request,urlopen


def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def main():
    repo=Path(__file__).resolve().parents[1]
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,default=repo/'data/raw')
    p.add_argument('--countries',nargs='+',help='Exact country names; omit to download all 103 countries')
    p.add_argument('--workers',type=int,default=4)
    args=p.parse_args()
    if not 1<=args.workers<=16:p.error('--workers must be between 1 and 16')
    files=json.loads((repo/'provenance/master_files.json').read_text(encoding='utf-8'))
    if args.countries:
        unknown=set(args.countries)-{a['country'] for a in files}
        if unknown:p.error(f'Unknown countries: {sorted(unknown)}')
        files=[a for a in files if a['country'] in args.countries]
    release=json.loads((repo/'provenance/raw_data_release.json').read_text(encoding='utf-8'))
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)

    def download(entry):
        target=(out/entry['path']).resolve()
        if not target.is_relative_to(out):raise ValueError('Unexpected country path')
        if target.exists():
            if target.stat().st_size==entry['bytes'] and sha(target)==entry['sha256']:return entry['country']
            raise ValueError(f'Existing file differs from the frozen manifest: {target}')
        target.parent.mkdir(parents=True,exist_ok=True)
        partial=target.with_name(target.name+'.partial')
        if partial.exists():raise FileExistsError(f'Previous partial download exists: {partial}')
        url=entry.get('download_url') or release['asset_base_url']+quote(target.name)
        request=Request(url,headers={'User-Agent':'SbRust-Global-data-download'})
        with urlopen(request,timeout=120) as source,partial.open('xb') as sink:
            for chunk in iter(lambda:source.read(4*1024*1024),b''):sink.write(chunk)
        if partial.stat().st_size!=entry['bytes'] or sha(partial)!=entry['sha256']:
            raise ValueError(f'Country download differs from the frozen checksum: {entry["country"]}')
        partial.rename(target)
        return entry['country']

    completed=0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(download,a) for a in files]):
            country=future.result();completed+=1
            print(f'Verified {completed}/{len(files)} {country}',flush=True)
    print(f'PASS: {len(files)} country masters and {sum(a["rows"] for a in files):,} raw paired records verified in {out}')


if __name__=='__main__':main()
