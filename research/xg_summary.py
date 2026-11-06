"""Read pinned Understat league-summary CSVs without summing individual shots.

Only completed rows with provider data become training observations. External
features/models are never read, and source goals never replace reference labels.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import urllib.request

from research.performance import load_performance
from research.xg_data import integer, merge_xg, team, validate_match


REPOSITORY = 'elliot-gautier-129/footy-prediction'
REVISION = '90233c5edc2cd4a07011ea642b27ea479d106bf5'
EXPECTED_FILES = {f'eng_{year % 100:02d}{(year + 1) % 100:02d}.csv' for year in range(2019, 2027)}
RAW_COLUMNS = {'league', 'season', 'season_id', 'game_id', 'date', 'home_team', 'away_team',
               'home_goals', 'away_goals', 'home_xg', 'away_xg', 'is_result', 'has_data', 'url'}


def boolean(value):
    if value not in ('True', 'False'):
        raise ValueError('Invalid Understat summary boolean')
    return value == 'True'


def parse_summary_csv(body, season):
    """Return normalized completed matches and skip counts for one season."""
    reader = csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
    if not RAW_COLUMNS.issubset(reader.fieldnames or []):
        raise ValueError('Understat summary is missing raw match columns')
    result = []; seen_ids = set(); seen_fixtures = set()
    stats = {'inputRows': 0, 'completedMatches': 0, 'futureFixturesSkipped': 0, 'missingDataSkipped': 0}
    expected_season = f'{season % 100:02d}{(season + 1) % 100:02d}'
    for row in reader:
        stats['inputRows'] += 1
        if row['league'] != 'ENG-Premier League' or integer(row['season_id'], 'season', 2100) != season or row['season'] != expected_season:
            raise ValueError('Understat summary league/season mismatch')
        match_id = integer(row['game_id'], 'match ID')
        if row['url'] != f'https://understat.com/match/{match_id}':
            raise ValueError('Understat summary URL/ID mismatch')
        when = date.fromisoformat(row['date'][:10])
        if not date(season, 7, 1) <= when <= date(season + 1, 7, 31):
            raise ValueError('Understat summary date outside declared season')
        fixture = (when.isoformat(), team(row['home_team']), team(row['away_team']))
        if fixture[1] == fixture[2]:
            raise ValueError('Identical Understat summary teams')
        if match_id in seen_ids or fixture in seen_fixtures:
            raise ValueError('Duplicate Understat summary fixture or ID')
        seen_ids.add(match_id); seen_fixtures.add(fixture)
        completed, available = boolean(row['is_result']), boolean(row['has_data'])
        if not completed:
            stats['futureFixturesSkipped'] += 1
            continue
        if not available:
            stats['missingDataSkipped'] += 1
            continue
        result.append(validate_match({'date': fixture[0], 'home': fixture[1], 'away': fixture[2],
                                      'home_goals': row['home_goals'], 'away_goals': row['away_goals'],
                                      'home_xg': row['home_xg'], 'away_xg': row['away_xg'],
                                      'source_match_id': match_id, 'season': season}))
    result.sort(key=lambda r: (r['date'], r['home'], r['away']))
    stats['completedMatches'] = len(result)
    return result, stats


def _manifest_entries(source_manifest):
    if source_manifest is None:
        return None
    manifest = json.loads(Path(source_manifest).read_text()) if isinstance(source_manifest, (str, Path)) else source_manifest
    if manifest.get('repo') != REPOSITORY or manifest.get('revision') != REVISION:
        raise ValueError('Understat summary source revision is not registered')
    result = {}
    for entry in manifest['files']:
        if entry['path'] == 'README.md':
            continue  # Documentation is not a raw-data input.
        path = Path(entry['path'])
        if path.parent.as_posix() != 'data/raw/understat/eng' or path.name not in EXPECTED_FILES or path.name in result:
            raise ValueError('Invalid pinned Understat summary path')
        expected_url = f'https://raw.githubusercontent.com/{REPOSITORY}/{REVISION}/' + path.as_posix()
        if entry.get('url') != expected_url:
            raise ValueError('Understat summary URL is not pinned to the registered revision')
        result[path.name] = entry
    if set(result) != EXPECTED_FILES:
        raise ValueError('Pinned Understat summary file set is incomplete')
    return result


def _verify(body, entry):
    digest = hashlib.sha256(body).hexdigest()
    blob = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
    if len(body) != entry['size'] or digest != entry['sha256'] or blob != entry['sha']:
        raise ValueError('Pinned Understat summary file hash or size mismatch')
    return digest, blob


def download_pinned(directory, source_manifest):
    entries = _manifest_entries(source_manifest)
    if entries is None:
        raise ValueError('Downloading requires a pinned source manifest')
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    for name, entry in entries.items():
        request = urllib.request.Request(entry['url'], headers={'User-Agent': 'FootpredictResearch/1.0'})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError('Understat summary size limit exceeded')
        _verify(body, entry)
        temporary = directory / (name + '.tmp'); temporary.write_bytes(body)
        temporary.replace(directory / name)


def load_summary(directory, reference=None, source_manifest=None):
    paths = sorted(Path(directory).glob('eng_*.csv'))
    entries = _manifest_entries(source_manifest)
    if not paths or (entries is not None and {path.name for path in paths} != set(entries)):
        raise ValueError('Understat summary CSV set is missing or differs from the pinned manifest')
    result = []; inputs = []; seen_ids = set(); seen_fixtures = set()
    for path in paths:
        season = 2000 + int(path.stem.split('_')[1][:2])
        if path.name != f'eng_{season % 100:02d}{(season + 1) % 100:02d}.csv':
            raise ValueError('Invalid Understat summary season filename')
        body = path.read_bytes()
        if entries is not None:
            digest, blob = _verify(body, entries[path.name])
        else:
            digest = hashlib.sha256(body).hexdigest()
            blob = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
        rows, counts = parse_summary_csv(body, season)
        for row in rows:
            fixture = (row['date'], row['home'], row['away'])
            if fixture in seen_fixtures or row['source_match_id'] in seen_ids:
                raise ValueError('Duplicate fixture/ID across Understat summary files')
            seen_fixtures.add(fixture); seen_ids.add(row['source_match_id'])
        result.extend(rows)
        inputs.append({'file': path.name, 'season': season, 'sha256': digest, 'gitBlobSha1': blob,
                       'bytes': len(body), **counts})
    result.sort(key=lambda r: (r['date'], r['home'], r['away']))
    joined = None
    if reference is not None:
        _, joined = merge_xg(reference, result)
        if joined['unmatchedArchiveMatches']:
            raise ValueError('Summary fixture lacks an exact reference date/team match')
    manifest = {'repository': REPOSITORY, 'revision': REVISION,
                'sourceUrl': f'https://github.com/{REPOSITORY}/tree/{REVISION}/data/raw/understat/eng',
                'source': 'Understat league-summary xG cached by soccerdata; not raw shot xG sums',
                'pinnedFilesVerified': entries is not None, 'referenceScoresVerified': reference is not None,
                'inputFiles': inputs, 'matches': len(result),
                'firstDate': result[0]['date'] if result else None, 'lastDate': result[-1]['date'] if result else None,
                'futureFixturesSkipped': sum(entry['futureFixturesSkipped'] for entry in inputs),
                'missingDataSkipped': sum(entry['missingDataSkipped'] for entry in inputs),
                'bySeason': {str(year): sum(row['season'] == year for row in result) for year in sorted({r['season'] for r in result})},
                'usedColumns': sorted(RAW_COLUMNS), 'join': joined,
                'availabilityCaveat': 'Pinned snapshot supports retrospective research, not historical 24-hour availability.'}
    return result, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--source-manifest', required=True, type=Path)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    if args.download:
        download_pinned(args.directory, args.source_manifest)
    reference, _ = load_performance(args.data_dir, range(2015, 2027))
    rows, manifest = load_summary(args.directory, reference, args.source_manifest)
    normalized = (json.dumps(rows, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    manifest['normalizedMatchesSha256'] = hashlib.sha256(normalized).hexdigest()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'xg-summary-matches.json').write_bytes(normalized)
    (args.output / 'xg-summary-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({k: v for k, v in manifest.items() if k not in ('inputFiles', 'join')}))


if __name__ == '__main__':
    main()
