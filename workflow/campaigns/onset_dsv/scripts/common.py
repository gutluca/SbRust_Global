"""Immutable applications campaign; shared paths and atomic receipts."""
import hashlib
import json
import os
from pathlib import Path
import re
import socket

VERSION = 'onset_dsv_co2_20261123_v1'
ROOT = Path('/blue/wpavan/gustavo.luca/SBrust_world')
PACK = Path(__file__).resolve().parents[1]
GEN = ROOT / 'generated/world' / VERSION
RESULTS = ROOT / 'results/world' / VERSION
STATE = GEN / 'state.json'
SOURCE = ROOT / 'generated/world/co2_future_global_20261119_v2/state.json'
OLD = ROOT / 'results/onset_dsv_world_20261110T020100Z_1360179'
EXE_SHA = '811a53c4d85d2f47ecf7815d9c46bc75fbc5fe5dbd2dd26aae3fce606f2ba41e'
BAD_NODES = ('c0706a-s23', 'c0706a-s27')
GCMS = ('GFDL-ESM4','IPSL-CM6A-LR','MPI-ESM1-2-HR','MRI-ESM2-0','UKESM1-0-LL')
PLANTS = tuple(f'P{i:02d}' for i in range(1,6))
CORE = ('Brazil','Bolivia','United States')

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for b in iter(lambda: stream.read(1048576), b''): h.update(b)
    return h.hexdigest()

def load(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def save(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.partial.' + str(os.getpid()))
    with tmp.open('w', encoding='utf-8', newline='\n') as f:
        json.dump(value, f, indent=2, allow_nan=False); f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def verify(path, expected):
    if sha(path) != expected: raise ValueError('Changed frozen file: ' + str(path))

def signature(value): return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def safe_name(name): return re.sub(r'[^A-Za-z0-9_-]+','_',name).strip('_')
def ready(state, rec): return Path(state['output']) / rec['key'] / 'production'
def result(state, rec): return Path(state['results']) / rec['key']
def country(state,key): return next(x for x in state['countries'] if x['key']==key)
def compute_only():
    if not os.environ.get('SLURM_JOB_ID'): raise RuntimeError('Use Slurm compute jobs')
    if socket.gethostname().split('.')[0] in BAD_NODES: raise RuntimeError('Excluded storage-failure node')

def replace(value, mapping):
    if isinstance(value,dict): return {k:replace(v,mapping) for k,v in value.items()}
    if isinstance(value,list): return [replace(v,mapping) for v in value]
    if isinstance(value,str):
        for a,b in mapping.items(): value=value.replace(a,b)
    return value
