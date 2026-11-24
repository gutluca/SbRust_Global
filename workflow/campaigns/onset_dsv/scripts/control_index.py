"""Full unfiltered current master in a compact exact-key index (14 bytes/row)."""
from array import array
from collections import Counter
import csv
from datetime import date
import gzip
import math
from pathlib import Path
import struct
import common as c
import onset_dsv as o
import ensemble3_global as e

RECORD = struct.Struct('<Ihd')
STATIC = ('MATURITY_GROUP','WEATHER_SOURCE_CELL_ID','LATITUDE','LONGITUDE','HARVEST_AREA_HA')

def position(row, indices, country):
    cell=str(int(row['CELL_ID'])); year=int(row['SIM_YEAR'])
    group=o.a.group_index(row['PERIOD'],row['SCENARIO'],year)
    if row['COUNTRY']!=country or row['SIMULATION_ID']!=e.identity(country,cell,group,year,row['GCM'],row['PLANTING_LABEL']):
        raise ValueError('Invalid full simulation identity')
    return cell, group, indices[cell]*e.N+e.slot(group,year,row['GCM'],row['PLANTING_LABEL'])

def export(master, expected, country, keep, folder):
    c.verify(master,expected)
    cells=sorted(keep,key=int); indices={v:i for i,v in enumerate(cells)}; n=len(cells)*e.N
    plants=array('I',[0])*n; daes=array('h',[-32768])*n; yields=array('d',[0.])*n
    meta={}; stats=Counter()
    with gzip.open(master,'rt',encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f,delimiter=';'):
            cell=str(int(row['CELL_ID']))
            if cell not in indices: raise ValueError('Current master has an unexpected cell')
            cell,group,i=position(row,indices,country)
            if plants[i]: raise ValueError('Duplicate master simulation')
            static=[row[k] for k in STATIC]
            if cell in meta and meta[cell]!=static: raise ValueError('GM/site metadata varies within cell')
            meta[cell]=static
            for key in ('LATITUDE','LONGITUDE','HARVEST_AREA_HA'):
                if not math.isclose(float(row[key]),float(keep[cell][key]),rel_tol=1e-8,abs_tol=1e-4):
                    raise ValueError('Population differs from frozen publication population')
            plants[i]=date.fromisoformat(row['PLANT_DATE']).toordinal()
            daes[i]=o.onset_value(row['DISEASE_ONSET_DAE'])
            yields[i]=float(row['YIELD_NO_DISEASE_KG_HA'])
            if not math.isfinite(yields[i]): raise ValueError('Nonfinite raw reference yield')
            stats['historical' if group==0 else 'future']+=1
    if 0 in plants or -32768 in daes: raise ValueError('Incomplete 4600-row/cell current matrix')
    folder.mkdir(parents=True,exist_ok=True)
    path=folder/'controls.bin.gz'
    with gzip.open(path,'wb',compresslevel=1) as f:
        block=bytearray()
        for values in zip(plants,daes,yields):
            block.extend(RECORD.pack(*values))
            if len(block)>=1048576: f.write(block); block.clear()
        f.write(block)
    info=dict(country=country,cells=cells,static=meta,static_fields=STATIC,records=n,
              record_format='<Ihd',source=str(master),source_sha256=expected,sha256=c.sha(path),rows=dict(stats))
    c.save(folder/'metadata.json',info)
    return Index(folder)

class Index:
    def __init__(self,folder):
        folder=Path(folder); self.meta=c.load(folder/'metadata.json'); self.cells={v:i for i,v in enumerate(self.meta['cells'])}
        path=folder/'controls.bin.gz'; c.verify(path,self.meta['sha256'])
        with gzip.open(path,'rb') as f: self.data=f.read()
        if len(self.data)!=self.meta['records']*RECORD.size: raise ValueError('Truncated control index')

    def record(self,row):
        upper={'CELL_ID':row['cell_id'],'COUNTRY':row['country'],'SIM_YEAR':row['simulation_year'],
               'PERIOD':row['period'],'SCENARIO':row['scenario'],'GCM':row['gcm'],
               'PLANTING_LABEL':row['planting_label'],'SIMULATION_ID':row['simulation_id']}
        cell,group,i=position(upper,self.cells,self.meta['country'])
        return RECORD.unpack_from(self.data,i*RECORD.size)

    def get(self,row):
        plant,dae,yield0=self.record(row)
        if date.fromordinal(plant).isoformat()!=row['plant_date']: raise ValueError('Current master planting differs')
        return dae,o.onset_date(date.fromordinal(plant),dae)

    def controls(self,cfg,cells):
        p=cfg['pipeline']; year=int(p['simulation_year']); group=o.a.group_index(p['period'],p['scenario'],year)
        output={}
        for cell in sorted(cells,key=int):
            gm,weather,lat,lon,area=self.meta['static'][cell]
            for plant in c.PLANTS:
                identity=e.identity(self.meta['country'],cell,group,year,p['gcm'],plant)
                i=self.cells[cell]*e.N+e.slot(group,year,p['gcm'],plant)
                ordinal,dae,y0=RECORD.unpack_from(self.data,i*RECORD.size)
                output[cell,plant]=dict(SIMULATION_ID=identity,COUNTRY=self.meta['country'],PERIOD=p['period'],
                    SIM_YEAR=year,SCENARIO=p['scenario'],GCM=p['gcm'],CELL_ID=cell,PLANTING_LABEL=plant,
                    PLANT_DATE=date.fromordinal(ordinal).isoformat(),YIELD_NO_DISEASE_KG_HA=y0,
                    MATURITY_GROUP=gm,WEATHER_SOURCE_CELL_ID=weather,LATITUDE=lat,LONGITUDE=lon,HARVEST_AREA_HA=area)
        return output

    def compatible_old(self,row):
        cell,group,i=position(row,self.cells,self.meta['country'])
        ordinal,dae,y0=RECORD.unpack_from(self.data,i*RECORD.size)
        return ([row[k] for k in STATIC]==self.meta['static'][cell]
                and ordinal==date.fromisoformat(row['PLANT_DATE']).toordinal()
                and dae==o.onset_value(row['DISEASE_ONSET_DAE'])
                and y0==float(row['YIELD_NO_DISEASE_KG_HA']))

def changed_historical(index,old_master,expected_sha):
    c.verify(old_master,expected_sha); dirty=set(); seen=bytearray(len(index.cells)*850)
    with gzip.open(old_master,'rt',encoding='utf-8-sig',newline='') as f:
        for row in csv.DictReader(f,delimiter=';'):
            if row['SCENARIO']!='historical': continue
            cell=str(int(row['CELL_ID']))
            if cell not in index.cells: continue
            cell,group,i=position(row,index.cells,index.meta['country'])
            j=index.cells[cell]*850+i% e.N
            if group!=0 or seen[j]: raise ValueError('Duplicate or invalid old historical reference')
            seen[j]=1
            if not index.compatible_old(row): dirty.add(cell)
    if 0 in seen: raise ValueError('Old historical source matrix incomplete')
    return dirty
