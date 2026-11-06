"""Prepare verified current results and accepted xG for bounded research."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import gzip
import json
from pathlib import Path
import re
import tempfile
import urllib.request

from research.data import download_season
from research.performance import Performance
from research.xg_data import load_xg_matches, merge_xg

REPOSITORY = 'FoXsYsOfficial1909/Footpredict'
LATEST = f'https://raw.githubusercontent.com/{REPOSITORY}/xg-data-reports/latest.json'


def fetch_bytes(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'FootpredictResearch/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(3_000_001)
    if len(body) > 3_000_000:
        raise ValueError('Experiment input size limit exceeded')
    return body


def prepare(data_dir, output, *, as_of=None, fetch=fetch_bytes, download=download_season):
    as_of = as_of or datetime.now(timezone.utc).date()
    season = as_of.year if as_of.month >= 7 else as_of.year - 1
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    original, references = [], []
    for year in range(2015, season + 1):
        matches, source = download(year, data_dir, refresh=year == season)
        if year < season and len(matches) != 380:
            raise ValueError('An experiment closed season needs 380 verified results')
        if len({m.key for m in matches}) != len(matches):
            raise ValueError('Duplicate reference fixture')
        original.extend(Performance(m, None, None, None, None) for m in matches)
        references.append(source)
    latest_bytes = fetch(LATEST)
    latest = json.loads(latest_bytes)
    accepted = latest.get('lastAccepted') or {}
    checksum = accepted.get('normalizedMatchesSha256', '')
    if (latest.get('schemaVersion') != 1 or accepted.get('status') != 'success'
        or not re.fullmatch(r'[0-9a-f]{64}', checksum)):
        raise ValueError('Missing verified accepted xG snapshot')
    expected_url = f'https://raw.githubusercontent.com/{REPOSITORY}/xg-data-reports/datasets/{checksum}.json'
    if accepted.get('datasetUrl') != expected_url:
        raise ValueError('Accepted xG snapshot URL does not identify its immutable archive')
    body = fetch(expected_url)
    if hashlib.sha256(body).hexdigest() != checksum:
        raise ValueError('Accepted xG snapshot checksum mismatch')
    with tempfile.TemporaryDirectory(prefix='footpredict-experiment-input-') as temporary:
        temporary_path = Path(temporary) / 'xg.json'
        temporary_path.write_bytes(body)
        rows = load_xg_matches(temporary_path)
    if any(date.fromisoformat(row['date']) > as_of for row in rows):
        raise ValueError('Accepted xG snapshot contains future match observations')
    _, joined = merge_xg(original, rows)
    if joined['unmatchedArchiveMatches'] or len(rows) != accepted.get('matchCount'):
        raise ValueError('Accepted xG snapshot does not match the verified reference fixtures')
    if not rows or max(row['date'] for row in rows) != accepted.get('dataLastMatchDate'):
        raise ValueError('Accepted xG snapshot last-match date mismatch')
    now = datetime.now(timezone.utc).isoformat()
    current = joined['bySeason'].get(str(season), {'referenceMatches': 0, 'matched': 0, 'missing': 0})
    source_attempt = latest.get('latestAttempt') or {}
    source_last = accepted['dataLastMatchDate']
    freshness = {'sourceStatus': source_attempt.get('status', 'unknown'),
        'sourceCheckedAt': source_attempt.get('generatedAt'), 'lastAcceptedAt': accepted.get('acceptedAt'),
        'dataLastMatchDate': source_last, 'dataAgeDays': (as_of - date.fromisoformat(source_last)).days,
        'season': season, 'completedMatches': current['referenceMatches'], 'matchedXg': current['matched'],
        'missingXg': current['missing'], 'status': 'stale' if current['missing'] or
            (as_of - date.fromisoformat(source_last)).days > 14 else 'current'}
    manifest = {'schemaVersion': 1, 'normalizedMatchesSha256': checksum, 'preparedAt': now,
        'sourceUrl': expected_url, 'sourceReportSha256': hashlib.sha256(latest_bytes).hexdigest(),
        'acceptedSource': accepted, 'sourceFreshness': freshness, 'referenceSources': references,
        'join': joined, 'promotedToDefault': False}
    reference_body = (json.dumps([{'date': o.match.day.isoformat(), 'season': o.match.season,
        'home': o.match.home, 'away': o.match.away, 'homeGoals': o.match.home_goals,
        'awayGoals': o.match.away_goals} for o in original],
        ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode()
    compressed = gzip.compress(reference_body, mtime=0)
    manifest['referenceSnapshotSha256'] = hashlib.sha256(compressed).hexdigest()
    manifest['referenceSnapshotUncompressedSha256'] = hashlib.sha256(reference_body).hexdigest()
    manifest['referenceSnapshotRows'] = len(original)
    # Stage and validate all input before replacing any prior successful output.
    for name, data in [('xg-matches.json', body), ('reference-results.json.gz', compressed), ('xg-manifest.json',
            (json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode())]:
        target = output / name
        temporary_path = output / (name + '.tmp')
        temporary_path.write_bytes(data)
        temporary_path.replace(target)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--as-of', type=date.fromisoformat)
    args = parser.parse_args()
    result = prepare(args.data_dir, args.output, as_of=args.as_of)
    print(json.dumps(result['sourceFreshness'], ensure_ascii=False))
