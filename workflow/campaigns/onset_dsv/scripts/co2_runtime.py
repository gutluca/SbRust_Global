"""Force scenario CO2 in each Pythia run directory and verify DAILY realized CO2."""
from __future__ import annotations
import csv
from bisect import bisect_right
from datetime import date, timedelta
import gzip
import math
import os
from pathlib import Path
import shutil

from common import sha, save


def dated_co2(path):
    """Read positive records from the ORIGINAL M file; retain its step semantics."""
    result = []
    active = False
    for line in Path(path).read_text().splitlines():
        fields = line.split()
        if fields[:4] == ['@', 'YEAR', 'DOY', 'CO2']:
            active = True
            continue
        if not active or not fields or line.lstrip().startswith(('!', '*', '@')):
            continue
        year, doy, value = int(fields[0]), int(fields[1]), float(fields[2])
        if value < 1:
            continue
        stamp = date(year,1,1) + timedelta(days=doy-1)
        if stamp.year != year or not math.isfinite(value):
            raise ValueError('Invalid original CO2 record')
        if result and stamp.isoformat() <= result[-1][0]:
            raise ValueError('Original CO2 dates are not strictly ordered')
        result.append([stamp.isoformat(),value])
    if not result:
        raise ValueError('Original CO2 M series is empty')
    return result


def expected_co2(annual, stamp):
    if 'dated_ppm' in annual:
        records = annual['dated_ppm']
        index = max(0,bisect_right([r[0] for r in records],stamp.isoformat())-1)
        return float(records[index][1])
    if str(stamp.year) not in annual:
        raise ValueError('CO2 series misses simulated year ' + str(stamp.year))
    return float(annual[str(stamp.year)])


def leaf_directories(config, work):
    work = Path(work).resolve()
    directories = []
    for run in config['runs']:
        branch = work / run['name']
        if not branch.is_dir():
            raise ValueError('Pythia run branch missing: ' + str(branch))
        leaves = sorted(p.parent for p in branch.rglob('*.SBX'))
        if not leaves:
            raise ValueError('Pythia created no experiment inputs: ' + str(branch))
        for leaf in leaves:
            try:
                leaf.resolve().relative_to(work)
            except ValueError:
                raise ValueError('Experiment directory escapes scratch') from None
            directories.append((run, leaf))
    if len({str(p) for _, p in directories}) != len(directories):
        raise ValueError('Duplicate experiment directory')
    return directories


def stage_co2(config, work):
    forcing = config['pipeline']['co2_forcing']
    if forcing['scenario'] != config['pipeline']['scenario'] or forcing['switch'] != 'M':
        raise ValueError('CO2 scenario/switch mismatch')
    source = Path(forcing['path'])
    if source.name != 'CO2048.WDA' or sha(source) != forcing['sha256']:
        raise ValueError('Frozen CO2 input changed')
    count = 0
    for run, leaf in leaf_directories(config, work):
        experiment = leaf / 'SBG00001.SBX'
        lines = experiment.read_text().splitlines()
        options = next(i for i, s in enumerate(lines) if s.startswith('@N OPTIONS') and 'CO2' in s)
        if lines[options + 1].split()[-1] != 'M':
            raise ValueError('Rendered experiment does not select CO2=M')
        env = next(i for i, s in enumerate(lines) if s.startswith('@E ') and 'ECO2' in s)
        # Standard template has seven modifier pairs. ECO2 is the sixth pair.
        tokens = lines[env + 1].split()
        headers = lines[env].split()
        # The source template is hash-locked globally; detect any non-zero CO2
        # adjustment here as an additional rendered-input check.
        modifier = 2 + 2 * (headers.index('ECO2') - 2) if 'ECO2' in headers else -1
        if modifier < 0 or len(tokens) <= modifier + 1 or tokens[modifier] != 'A' or float(tokens[modifier + 1]) != 0:
            raise ValueError('Rendered environmental CO2 adjustment differs from A 0')
        destination = leaf / 'CO2048.WDA'
        # Never follow/overwrite a link to the shared default file.
        if destination.is_symlink():
            destination.unlink()
        elif destination.exists():
            if not destination.is_file():
                raise ValueError('Unexpected local CO2 target')
            destination.unlink()
        shutil.copyfile(source, destination)
        if sha(destination) != forcing['sha256']:
            raise ValueError('CO2 staging checksum mismatch')
        destination.chmod(0o444)
        count += 1
    return count


def read_daily_weather(path, annual, tolerance=0.051):
    """Validate every printed CO2D value, including seasons crossing January 1."""
    header = None
    n = 0
    max_error = 0.
    first = previous = None
    records = []
    for line in Path(path).read_text(errors='strict').splitlines():
        if line.startswith('@YEAR'):
            header = line.lstrip('@').split()
            if not {'YEAR', 'DOY', 'CO2D', 'PRED'} <= set(header):
                raise ValueError('Weather.OUT lacks daily CO2D or rain')
            continue
        fields = line.split()
        if header is None or not fields or not fields[0].isdigit():
            continue
        year, doy = int(fields[header.index('YEAR')]), int(fields[header.index('DOY')])
        stamp = date(year, 1, 1) + timedelta(days=doy - 1)
        if doy < 1 or stamp.year != year or previous is not None and stamp != previous + timedelta(days=1):
            raise ValueError('Invalid/non-daily Weather.OUT sequence')
        observed = float(fields[header.index('CO2D')])
        expected = expected_co2(annual, stamp)
        error = abs(observed - expected)
        if not math.isfinite(observed) or error > tolerance:
            raise ValueError(f'Wrong REALIZED CO2 at {stamp}: {observed} ppm; expected {expected}')
        rain = float(fields[header.index('PRED')])
        if not math.isfinite(rain) or rain < 0:
            raise ValueError('Invalid printed rain')
        max_error = max(max_error, error)
        n += 1
        first = first or stamp
        previous = stamp
        records.append((stamp, rain))
    if not n:
        raise ValueError('No daily realized CO2 records')
    return {'days': n, 'first_date': first.isoformat(), 'last_date': previous.isoformat(),
            'maximum_absolute_error_ppm': max_error}


def verify_co2(config, work, output):
    forcing = config['pipeline']['co2_forcing']
    checks = []
    for run, leaf in leaf_directories(config, work):
        if sha(leaf / 'CO2048.WDA') != forcing['sha256']:
            raise ValueError('Local forcing changed during model execution')
        expected = forcing.get('annual_ppm') or {'dated_ppm': forcing['dated_ppm']}
        result = read_daily_weather(leaf / 'Weather.OUT', expected)
        result.update(run=run['name'], directory=str(leaf.relative_to(work)))
        checks.append(result)
    expected = config['pipeline']['co2_rerun']['expected_points'] * 10
    if len(checks) != expected:
        raise ValueError(f'CO2 outputs incomplete: {len(checks)}/{expected}')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with gzip.open(output / 'realized_co2_checks.csv.gz', 'wt', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(checks[0]))
        writer.writeheader()
        writer.writerows(checks)
    audit = {'status': 'ok', 'scenario': forcing['scenario'], 'forcing_sha256': forcing['sha256'],
             'checked_experiments': len(checks), 'checked_daily_values': sum(c['days'] for c in checks),
             'maximum_absolute_error_ppm': max(c['maximum_absolute_error_ppm'] for c in checks),
             'validation': 'every daily Weather.OUT CO2D value agrees with scenario annual input within output rounding',
             'tolerance_ppm': 0.051}
    save(output / 'co2_audit.json', audit)
    return audit
