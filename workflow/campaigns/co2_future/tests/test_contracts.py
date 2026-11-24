import copy
import csv
from datetime import date, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

PACK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACK / 'scripts'))
import common as c
from co2_runtime import stage_co2, read_daily_weather
from merge import combine
from native_finalize import validator_code, finite_raw_yield


def source_config(executable):
    runs = []
    for p in c.PLANTS:
        for arm in ('disease', 'control'):
            site = '/original/GM_5.gpkg'
            runs.append(dict(name=f'{arm}_{p}_GM_5', planting_label=p, run_label=f'{arm}_{p}',
                dises='Y' if arm == 'disease' else 'N', ingeno='990005', cname='M GROUP 5', zone_id='GM_5',
                sites='xy_from_vector::' + site, wsta='vector::' + site + '::WTH_CELLID',
                pfrst='vector::' + site + '::PLANT_DOY::-1::0', startYear=2069))
    return dict(name='original', workDir='/old/work', templateDir='/old/templates',
        weatherDir='/orange/hoogenboom/weather/ssp585/GFDL-ESM4/GFDL-ESM4_ssp585_WTH',
        ghr_root='/same/soils', dssat={'executable': str(executable)}, threads=4, cores=4,
        default_setup={'nyers': 1, 'include': ['/same/SIMULATION.CDE', '/old/disease_parameters.txt'],
                       'icsw%': 25, 'irrig': 'N', 'startYear': 2069}, runs=runs,
        pipeline=dict(country='Brazil', annual=True, simulation_year=2069, period='future_mid',
            scenario='ssp585', gcm='GFDL-ESM4', planting_labels=list(c.PLANTS),
            disease_parameters_snapshot='/old/disease_parameters.txt',
            calendar={'mode': 'annual_scenario_reference', 'year': 2069, 'source': '/same/calendar.nc'}))


class ForcingContracts(unittest.TestCase):
    def test_wda_columns_match_fortran_header_endpoints_and_published_values(self):
        provenance = c.load(PACK / 'inputs/co2/provenance.json')
        for ssp, record in provenance['files'].items():
            path = PACK / record['relative_path']
            self.assertEqual(c.sha(path), record['sha256'])
            lines = path.read_text().splitlines()
            i = next(i for i, s in enumerate(lines) if s.startswith('@ YEAR'))
            # Fortran PARSE_HEADERS uses field endpoints, not whitespace split.
            endpoints = [m.end() for m in re.finditer(r'\S+', lines[i])]
            self.assertEqual(endpoints, [1, 6, 12, 24])
            values = {}
            for line in lines[i + 1:]:
                year = int(line[:6]); doy = int(line[6:12]); value = float(line[12:24])
                self.assertEqual(doy, 1)
                self.assertAlmostEqual(value, record['annual_ppm'][str(year)], places=5)
                values[year] = value
            self.assertEqual(set(values), set(range(2015, 2101)))
        self.assertAlmostEqual(provenance['files']['ssp126']['annual_ppm']['2050'], 469.309967, places=5)
        self.assertAlmostEqual(provenance['files']['ssp370']['annual_ppm']['2050'], 540.580933, places=5)
        self.assertAlmostEqual(provenance['files']['ssp585']['annual_ppm']['2069'], 732.647888, places=5)

    def test_realized_daily_co2_across_new_year_and_leap_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'Weather.OUT'
            path.write_text('@YEAR DOY DAS PRED CO2D\n 2020 366 1 0.0 415.2\n 2021 001 2 1.0 417.7\n')
            check = read_daily_weather(path, {'2020': 415.154968, '2021': 417.67})
            self.assertEqual(check['days'], 2)
            self.assertEqual(check['last_date'], '2021-01-01')
            for bad in (
                '@YEAR DOY DAS PRED CO2D\n 2069 100 1 0.0 425.4\n',
                '@YEAR DOY DAS PRED CO2D\n 2069 100 1 0.0 732.6\n 2069 102 2 0.0 732.6\n',
                '@YEAR DOY DAS PRED CO2D\n 2070 100 1 0.0 732.6\n',
                '@YEAR DOY DAS PRED CO2D\n 2069 366 1 0.0 732.6\n',
                '@YEAR DOY DAS PRED CO2D\n 2069 100 1 0.0 nan\n',
            ):
                path.write_text(bad)
                with self.assertRaises(ValueError):
                    read_daily_weather(path, {'2069': 732.647888})

    def test_staging_checks_rendered_switch_modifier_and_never_changes_shared_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); work = root / 'work'; leaf = work / 'control_P01_GM_5/site'
            leaf.mkdir(parents=True)
            forcing = c.load(PACK / 'inputs/co2/provenance.json')['files']['ssp585']
            cfg = {'runs': [{'name': 'control_P01_GM_5'}], 'pipeline': {'scenario': 'ssp585',
                'co2_forcing': dict(forcing, path=str(PACK / forcing['relative_path']), scenario='ssp585', switch='M')}}
            sbx = leaf / 'SBG00001.SBX'
            rendered = ('@E ODATE EDAY ERAD EMAX EMIN ERAIN ECO2 EDEW EWIND ENVNAME\n'
                        ' 1 69001 A 0 A 0 A 0 A 0 A 0 A 0 A 0 A 0 global\n'
                        '@N OPTIONS WATER NITRO SYMBI PHOSP POTAS DISES CHEM TILL CO2\n'
                        ' 1 OP Y N N N N N N N M\n')
            sbx.write_text(rendered)
            shared = root / 'SHARED_DEFAULT.WDA'; shared.write_text('425.37 shared unchanged\n')
            before = c.sha(shared)
            local = leaf / 'CO2048.WDA'
            try:
                local.symlink_to(shared)
            except OSError:
                local.write_text(shared.read_text())
            try:
                self.assertEqual(stage_co2(cfg, work), 1)
                self.assertEqual(c.sha(local), forcing['sha256'])
                self.assertEqual(c.sha(shared), before)
                self.assertFalse(local.is_symlink())
                sbx.write_text(rendered.replace('N N M\n', 'N N D\n'))
                with self.assertRaises(ValueError):
                    stage_co2(cfg, work)
                sbx.write_text(rendered.replace('A 0 A 0 A 0 global', 'A 9 A 0 A 0 global'))
                with self.assertRaises(ValueError):
                    stage_co2(cfg, work)
            finally:
                if local.exists():
                    local.chmod(0o666)

    def test_clone_preserves_forcing_calendar_management_and_uses_frozen_gms(self):
        src = source_config(PACK / 'dummy_binary')
        before = copy.deepcopy(src)
        zones = [dict(gm='7', path=PACK / 'fixed_GM_7.gpkg', count=3, ingeno='990007', cname='M GROUP 7'),
                 dict(gm='8', path=PACK / 'fixed_GM_8.gpkg', count=2, ingeno='990008', cname='M GROUP 8')]
        state = dict(pack=str(PACK), executable=str(PACK / 'dummy_binary'), output='/new/output', results='/new/results')
        cfg = c.clone_config(src, zones, state, {'key': 'Brazil_key'}, PACK, '/new/disease_parameters.txt', 'd'*64, '/new/template')
        self.assertEqual(src, before)
        self.assertEqual(cfg['weatherDir'], src['weatherDir'])
        self.assertEqual(cfg['pipeline']['calendar'], src['pipeline']['calendar'])
        self.assertEqual(cfg['default_setup']['icsw%'], 25)
        self.assertEqual(len(cfg['runs']), 20)
        self.assertEqual(cfg['pipeline']['co2_rerun']['expected_points'], 5)
        self.assertEqual({r['ingeno'] for r in cfg['runs']}, {'990007', '990008'})
        self.assertTrue(all('/original/GM_5.gpkg' not in json.dumps(r) for r in cfg['runs']))
        self.assertTrue(all(r['run_label'] in [a + '_' + p for a in ('disease', 'control') for p in c.PLANTS] for r in cfg['runs']))
        src['runs'][0]['dises'] = 'N'
        with self.assertRaises(ValueError):
            c.clone_config(src, zones, state, {'key': 'Brazil_key'}, PACK, '/new/disease_parameters.txt', 'd'*64, '/new/template')

    def test_frozen_publication_population(self):
        population = c.population(PACK)
        expected = c.load(PACK / 'inputs/current_sources.json')
        self.assertEqual(len(population), 16612)
        self.assertEqual({x[0] for x in population}, set(expected))
        self.assertEqual(len(expected), 103)
        self.assertEqual(sum(x['points'] for x in expected.values()), 16612)

    def test_postprocessing_retains_raw_values_and_mechanical_errors(self):
        source = ('def check(disease_lower_count, paired):\n'
                  '    if not paired: raise ValueError("pair broken")\n'
                  '    if disease_lower_count == 0: raise ValueError("zero effect")\n'
                  '    return True\n')
        ns = {}; exec(validator_code(source, 'test'), ns)
        self.assertTrue(ns['check'](0, True))
        with self.assertRaises(ValueError): ns['check'](1, False)
        with self.assertRaises(ValueError): validator_code('pass', 'test')
        with self.assertRaises(ValueError): finite_raw_yield('-99', 'control', 1)
        with self.assertRaises(ValueError): finite_raw_yield('nan', 'control', 1)


FIELDS = ['SIMULATION_ID','COUNTRY','CELL_ID','MATURITY_GROUP','PERIOD','SIM_YEAR','SCENARIO','GCM','PLANTING_LABEL','YIELD_NO_DISEASE_KG_HA']


def rows(historical, gm='7'):
    scenarios = ('historical',) if historical else c.SSPS
    for year in range(1981, 2015) if historical else range(2020, 2070):
        period = '1981-2014' if historical else '2020-2039' if year < 2040 else '2040-2069'
        for ssp in scenarios:
            for gcm in sorted(c.GCMS):
                for plant in c.PLANTS:
                    record = dict(COUNTRY='Brazil', CELL_ID='123', MATURITY_GROUP=gm, PERIOD=period,
                        SIM_YEAR=str(year), SCENARIO=ssp, GCM=gcm, PLANTING_LABEL=plant,
                        YIELD_NO_DISEASE_KG_HA='0.00000000')
                    record['SIMULATION_ID'] = '|'.join(record[k] for k in ('COUNTRY','CELL_ID','PERIOD','SIM_YEAR','SCENARIO','GCM','PLANTING_LABEL'))
                    yield record


class MasterContracts(unittest.TestCase):
    def write(self, path, records):
        with gzip.open(path, 'wt', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS, delimiter=';')
            writer.writeheader(); writer.writerows(records)

    def test_complete_merge_preserves_every_baseline_line_and_zero_yield(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old = root/'old.gz'; future = root/'future.gz'; destination = root/'master.gz'
            self.write(old, list(rows(True)) + list(rows(False)))
            self.write(future, rows(False))
            audit = combine(old, future, destination, 'Brazil', {'123': '7'})
            self.assertEqual(audit['historical_rows_preserved'], 850)
            self.assertEqual(audit['corrected_future_rows'], 3750)
            with gzip.open(old, 'rt', newline='') as stream:
                next(stream); original = [line for line in stream if ';historical;' in line]
            with gzip.open(destination, 'rt', newline='') as stream:
                next(stream); combined = list(stream)
            self.assertEqual(len(combined), 4600)
            self.assertEqual([line for line in combined if ';historical;' in line], original)
            self.assertEqual(audit['historical_rows_sha256_uncompressed'], hashlib.sha256(''.join(original).encode()).hexdigest())

    def test_merge_rejects_missing_future_and_changed_fixed_gm(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); old = root/'old.gz'; future = root/'future.gz'
            self.write(old, rows(True))
            self.write(future, list(rows(False))[1:])
            with self.assertRaises(ValueError): combine(old, future, root/'missing.gz', 'Brazil', {'123':'7'})
            self.write(future, rows(False, '8'))
            with self.assertRaises(ValueError): combine(old, future, root/'changed.gz', 'Brazil', {'123':'7'})


class SpatialContracts(unittest.TestCase):
    def test_existing_gm_overlay_is_selected_by_executed_master(self):
        import fiona
        from prepare import frozen_sites
        schema = {'geometry': 'Point', 'properties': {'CellID':'int', 'WTH_CELLID':'int', 'GM_WIN':'str',
                  'INGENO':'str', 'CNAME':'str', 'HAREA':'float'}}
        catalog = {r['gm']:r for r in c.load(PACK/'inputs/generic_maturity_candidates.json')['cultivars']}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def write(path, records):
                with fiona.open(path, 'w', driver='GPKG', layer='sites', crs='EPSG:4326', schema=schema) as dst:
                    for cell, gm in records:
                        dst.write({'type':'Feature', 'geometry':{'type':'Point','coordinates':(-50.,-20.)},
                            'properties': dict(CellID=cell,WTH_CELLID=cell,GM_WIN=gm,INGENO=catalog[gm]['ingeno'],
                                              CNAME=catalog[gm]['cname'], HAREA=100.)})
            write(root/'original.gpkg', [(1,'10'),(2,'8')]); write(root/'overlay.gpkg',[(1,'7')])
            metadata = {'1':('7','1','-20','-50','100'), '2':('8','2','-20','-50','100')}
            keep = {cell: {'HARVEST_AREA_HA':'100'} for cell in metadata}
            features, _, _, inputs = frozen_sites(root/'original.gpkg',[root/'overlay.gpkg'],metadata,keep,root/'fixed.gpkg',catalog)
            self.assertEqual({cell:f['properties']['GM_WIN'] for cell,f in features.items()}, {'1':'7','2':'8'})
            self.assertEqual(len(inputs),2)
            metadata['1'] = ('6','1','-20','-50','100')
            with self.assertRaises(ValueError):
                frozen_sites(root/'original.gpkg',[root/'overlay.gpkg'],metadata,keep,root/'bad.gpkg',catalog)


if __name__ == '__main__': unittest.main()
