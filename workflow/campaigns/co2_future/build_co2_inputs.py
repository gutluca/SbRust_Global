"""Extract annual global CO2 from the authors' published supplementary tables.

Read-only XLSX analysis; the original supplement is preserved. Run once with
the bundled Python containing openpyxl. Produces DSSAT fixed-width WDA files
with a value on January 1 of every year 2015--2100, no extrapolation.
"""
import csv
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
from zipfile import ZipFile

from openpyxl import load_workbook


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    root = Path(__file__).resolve().parent
    raw = root / 'inputs/raw/gmd-13-3571-2020-supplement.zip'
    member = 'SUPPLEMENT_DataTables_Meinshausen_6May2020.xlsx'
    with ZipFile(raw) as archive:
        data = archive.read(member)
    book = load_workbook(BytesIO(data), read_only=True, data_only=True)
    tables = {'ssp126': 'T4 -  SSP1-2.6 ', 'ssp370': 'T6 - SSP3-7.0 ', 'ssp585': 'T11 - SSP5-8.5 '}
    out = root / 'inputs/co2'
    out.mkdir(exist_ok=False)
    provenance = {
        'source_url': 'https://gmd.copernicus.org/articles/13/3571/2020/gmd-13-3571-2020-supplement.zip',
        'source_doi': '10.5194/gmd-13-3571-2020',
        'source_zip_sha256': digest(raw.read_bytes()), 'source_xlsx_member': member,
        'source_xlsx_sha256': digest(data), 'variable': 'CO2', 'unit': 'ppm',
        'region': 'World', 'temporal_resolution': 'annual mean',
        'implementation': 'annual step value effective January 1; no extrapolation; future runs only',
        'historical_baseline': 'preserved from the existing paired master; no historical input changed',
        'files': {},
    }
    all_rows = []
    for scenario, sheet_name in tables.items():
        sheet = book[sheet_name]
        assert sheet['B9'].value == 'CO2' and sheet['B10'].value == 'ppm'
        assert sheet['B11'].value == 'World' and sheet['A12'].value == 'Year'
        values = {}
        for year, value in sheet.iter_rows(min_row=13, max_col=2, values_only=True):
            if not isinstance(year, (int, float)) or not 2015 <= year <= 2100:
                continue
            if int(year) != year or int(year) in values:
                raise ValueError('Duplicate or noninteger source year')
            if not isinstance(value, (int, float)) or not math.isfinite(value) or not 300 < value < 2000:
                raise ValueError('Invalid source concentration')
            values[int(year)] = float(value)
        assert set(values) == set(range(2015, 2101))
        folder = out / scenario
        folder.mkdir()
        path = folder / 'CO2048.WDA'
        header = '@' + f"{'YEAR':>5}{'DOY':>6}{'CO2':>12}"
        lines = ['*Atmospheric CO2 SSP annual global mean',
                 '! Meinshausen et al. 2020 DOI 10.5194/gmd-13-3571-2020',
                 '! Source sheet: ' + sheet_name, '! Global mean, ppm; effective January 1 of each year',
                 '@CO2BAS', '   380.', header]
        for year, value in sorted(values.items()):
            line = f'{year:6d}{1:6d}{value:12.6f}'
            # Independent of split(): these are the exact columns parsed by
            # DSSAT PARSE_HEADERS for the header above.
            assert int(line[:6]) == year and int(line[6:12]) == 1
            assert abs(float(line[12:24]) - value) < 0.000001
            lines.append(line)
            all_rows.append({'scenario': scenario, 'year': year, 'co2_ppm': value})
        path.write_text('\n'.join(lines) + '\n', encoding='ascii', newline='\n')
        provenance['files'][scenario] = {'relative_path': str(path.relative_to(root)).replace('\\', '/'),
            'sha256': digest(path.read_bytes()), 'source_sheet': sheet_name, 'source_column': 'B',
            'years': [2015, 2100], 'records': len(values), 'annual_ppm': values}
        print(scenario, {y: round(values[y], 6) for y in (2020, 2030, 2050, 2069, 2100)})
    # Published Table 5 reports these global-mean 2050 values to 0.1 ppm.
    rounded_2050 = {r['scenario']: round(r['co2_ppm'], 1) for r in all_rows if r['year'] == 2050}
    assert rounded_2050['ssp126'] == 469.3 and rounded_2050['ssp370'] == 540.6
    assert len(set(rounded_2050.values())) == 3
    with (out / 'annual_co2_ppm.csv').open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['scenario', 'year', 'co2_ppm'])
        writer.writeheader()
        writer.writerows(all_rows)
    (out / 'provenance.json').write_text(json.dumps(provenance, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
