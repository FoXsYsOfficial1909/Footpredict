"""Check inputs repeatedly; fit fixed candidates only when their inputs change.

The date is part of the fingerprint because moving windows and decay use the
daily cutoff. An unchanged check is not a new training success or new model.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import urllib.error
import urllib.request

from research.data import DIVISIONS, download_season

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = 'continuous-training-1'
REPOSITORY = 'FoXsYsOfficial1909/Footpredict'
SCOPES = {
    'PL': [('training-reports', 'E0', 'goals')],
    'ELC': [('championship-training-reports', 'E1', 'goals'),
            ('championship-shots-training-reports', 'E1', 'shots_on_target')],
}
OWNED_FILES = ('model.json', 'model.mjs', 'report.json', 'report.md', 'evaluation.json',
               'training-data.json.gz', 'shots-data.json.gz', 'shots-manifest.json',
               'fits.json', 'attempt.json', 'check.json')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def configuration_hash(league):
    files = ['research/continuous_training.py', 'research/nightly.py', 'research/models.py',
             'research/evaluate.py', 'research/export_model.py', 'research/data.py',
             'requirements-research.txt']
    if league == 'PL':
        files += ['docs/research/selection.json']
    elif league == 'ELC':
        files += ['research/championship.py', 'research/championship_shots.py', 'research/performance.py',
                  'docs/research/championship-plan-2026-11-14.json',
                  'docs/research/championship-2026-11-14/selection.json',
                  'docs/research/championship-2026-11-14/report.json',
                  'docs/research/championship-shots-plan-2026-11-14.json',
                  'docs/research/championship-shots-2026-11-14/report.json']
    else:
        raise ValueError('Unsupported training league')
    return digest({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files})


def fingerprint(league, as_of, records, configuration):
    """The information fence applies here as well as in each fitter."""
    if league not in SCOPES:
        raise ValueError('Unsupported training league')
    cutoff = as_of - timedelta(days=1)
    prior = sorted((row for row in records if date.fromisoformat(row[0]) < cutoff),
                   key=lambda row: (row[0], row[2], row[3]))
    keys = [(row[0], row[2], row[3]) for row in prior]
    if len(set(keys)) != len(keys):
        raise ValueError('Duplicate input fixture')
    return digest({'protocol': PROTOCOL, 'league': league, 'asOf': as_of.isoformat(),
                   'trainingCutoffExclusive': cutoff.isoformat(), 'records': prior,
                   'configurationSha256': configuration})


def load_sources(league, directory, as_of):
    """Refresh a current CSV once; fitters subsequently reuse these exact files."""
    directory = Path(directory)
    season = as_of.year if as_of.month >= 7 else as_of.year - 1
    if league == 'PL':
        matches = []
        for year in range(2015, season + 1):
            rows, _ = download_season(year, directory, refresh=year == season)
            if year < season and len(rows) != DIVISIONS['E0']['closed_season_matches']:
                rows, _ = download_season(year, directory, refresh=True)
                if len(rows) != DIVISIONS['E0']['closed_season_matches']:
                    raise ValueError('A completed EPL season is incomplete')
            matches.extend(rows)
        return [[m.day.isoformat(), m.season, m.home, m.away, m.home_goals, m.away_goals]
                for m in sorted(matches, key=lambda m: m.key)]
    if league != 'ELC':
        raise ValueError('Unsupported training league')
    from research.championship import load_history
    from research.championship_shots import normalized
    from research.performance import parse_performance
    matches, sources = load_history(directory, as_of)
    observations = []
    cutoff = as_of - timedelta(days=1)
    for source in sources:
        raw = (directory / f"{source['season']}-E1.csv").read_bytes()
        if hashlib.sha256(raw).hexdigest() != source['sha256']:
            raise ValueError('A cached source changed during input verification')
        observations.extend(o for o in parse_performance(raw, source['season'], division='E1')
                            if o.match.day < cutoff)
    observations.sort(key=lambda o: o.match.key)
    if [o.match for o in observations] != matches:
        raise ValueError('Shot and goal input fixtures differ')
    return normalized(observations)


def previous_success(document, league, division, method):
    if document is None:
        return None
    if not isinstance(document, dict) or document.get('schemaVersion') != 1:
        raise ValueError('Invalid previous training archive')
    success = document.get('latestSuccessful')
    if success is None:
        return None
    if (not isinstance(success, dict) or success.get('status') != 'success'
            or success.get('league', 'PL') != league or success.get('division', 'E0') != division
            or success.get('method', 'goals') != method
            or success.get('promotedToDefault') is not False
            or not re.fullmatch('[0-9a-f]{64}', success.get('modelSha256', ''))):
        raise ValueError('Previous successful report has another scope or invalid identity')
    value = success.get('trainingFingerprint')
    if value is not None and not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Invalid previous training fingerprint')
    return success


def fetch_previous(repository, branch):
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or branch not in {scope[0] for scopes in SCOPES.values() for scope in scopes}):
        raise ValueError('Invalid training archive address')
    url = f'https://raw.githubusercontent.com/{repository}/{branch}/latest.json'
    request = urllib.request.Request(url, headers={'User-Agent': 'FootpredictResearch/1.0',
                                                  'Cache-Control': 'no-cache'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(500_001)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    if len(body) > 500_000:
        raise ValueError('Previous training archive exceeds its size limit')
    return json.loads(body)


def clear_output(output):
    output = Path(output).resolve()
    research = (ROOT / 'docs/research').resolve()
    if output == research or research in output.parents:
        raise ValueError('Continuous training cannot overwrite frozen research studies')
    output.mkdir(parents=True, exist_ok=True)
    for name in OWNED_FILES:
        (output / name).unlink(missing_ok=True)
    return output


def tag_training(output, value):
    """Metadata is separate from the immutable model bytes and its checksum."""
    for name in ('report.json', 'attempt.json'):
        path = Path(output) / name
        report = json.loads(path.read_text())
        if report.get('status') != 'success':
            raise ValueError('Training did not produce a successful candidate')
        report['trainingFingerprint'] = value
        write_json(path, report)
    return report


def run(league, directory, output, as_of, *, shots_output=None, repository=REPOSITORY):
    if league not in SCOPES or (league == 'ELC') != (shots_output is not None):
        raise ValueError('ELC requires a separate shots output; PL does not accept it')
    destinations = [Path(output)] + ([Path(shots_output)] if shots_output is not None else [])
    if len({p.resolve() for p in destinations}) != len(destinations):
        raise ValueError('Goal and shot outputs must be separate')
    # Validate every path before removing anything.
    research = (ROOT / 'docs/research').resolve()
    if any(p.resolve() == research or research in p.resolve().parents for p in destinations):
        raise ValueError('Continuous training cannot overwrite frozen research studies')
    outputs = [clear_output(p) for p in destinations]
    checked = datetime.now(timezone.utc).isoformat()
    checks = [dict(schemaVersion=1, protocol=PROTOCOL, checkedAt=checked, asOf=as_of.isoformat(),
                   league=league, division=division, method=method, promotedToDefault=False)
              for _, division, method in SCOPES[league]]
    done = set()
    try:
        records = load_sources(league, directory, as_of)
        value = fingerprint(league, as_of, records, configuration_hash(league))
        accepted = [r for r in records if date.fromisoformat(r[0]) < as_of - timedelta(days=1)]
        for check in checks:
            check.update(trainingFingerprint=value, inputMatches=len(accepted),
                         trainingCutoffExclusive=(as_of - timedelta(days=1)).isoformat(),
                         latestInputMatch=max((r[0] for r in accepted), default=None))
        old = [previous_success(fetch_previous(repository, branch), league, division, method)
               for branch, division, method in SCOPES[league]]
        if all(report and report.get('trainingFingerprint') == value for report in old):
            for check, report, path in zip(checks, old, outputs):
                check.update(status='unchanged', modelSha256=report['modelSha256'],
                             reason='inputs_and_daily_cutoff_unchanged')
                write_json(path / 'check.json', check)
            return checks
        if league == 'PL':
            from research.nightly import run as fit
            fit(directory, outputs[0], as_of, offline=True)
            report = tag_training(outputs[0], value)
            checks[0].update(status='trained', modelSha256=report['modelSha256'],
                             reason='inputs_settings_code_or_daily_cutoff_changed')
            write_json(outputs[0] / 'check.json', checks[0]); done.add(0)
        else:
            from research.championship import refresh as fit_goals
            from research.championship_shots import refresh as fit_shots
            fit_goals(directory, outputs[0], as_of, offline=True)
            report = tag_training(outputs[0], value)
            checks[0].update(status='trained', modelSha256=report['modelSha256'],
                             reason='inputs_settings_code_or_daily_cutoff_changed')
            write_json(outputs[0] / 'check.json', checks[0]); done.add(0)
            fit_shots(directory, outputs[0] / 'report.json', outputs[1], as_of)
            report = tag_training(outputs[1], value)
            checks[1].update(status='trained', modelSha256=report['modelSha256'],
                             reason='inputs_settings_code_or_daily_cutoff_changed')
            write_json(outputs[1] / 'check.json', checks[1]); done.add(1)
        return checks
    except Exception as error:
        for i, (check, path) in enumerate(zip(checks, outputs)):
            if i not in done:
                check.update(status='failed', reason='input_check_or_training_failed', errorType=type(error).__name__)
                write_json(path / 'check.json', check)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--league', choices=tuple(SCOPES), required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--shots-output', type=Path)
    parser.add_argument('--repository', default=REPOSITORY)
    parser.add_argument('--as-of', type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    args = parser.parse_args()
    result = run(args.league, args.data_dir, args.output, args.as_of,
                 shots_output=args.shots_output, repository=args.repository)
    print(json.dumps(result, ensure_ascii=False))
