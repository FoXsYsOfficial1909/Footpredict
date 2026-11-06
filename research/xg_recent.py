"""Validate a pinned secondary cache of recent Understat match xG.

Only raw match facts are read. External features, forecasts and model weights
are ignored. The primary archive wins overlapping fixture entries.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
import hashlib
import io
import json
from pathlib import Path
import urllib.parse
import urllib.request

from research.performance import load_performance
from research.xg_data import integer, team, validate_match


REPOSITORY = 'CameronNichol47/epl-match-prediction-dashboard'
REVISION = '647420c6bb756db992374b04a5ba0ee6235a2624'
EXPECTED_TEAMS = {'Arsenal', 'Aston Villa', 'Bournemouth', 'Brentford', 'Brighton',
                  'Chelsea', 'Coventry', 'Crystal Palace', 'Everton', 'Fulham',
                  'Hull', 'Ipswich', 'Leeds United', 'Liverpool', 'Manchester City',
                  'Manchester Utd', 'Newcastle', 'Nottingham', 'Sunderland', 'Tottenham'}
RAW_COLUMNS = {'Date', 'Comp', 'Venue', 'GF', 'GA', 'Opponent', 'XG_for', 'XG_against'}


def recent_team(value):
    return team({'Manchester Utd': 'Manchester United', 'Nottingham': 'Nottingham Forest'}.get(value, value))


def key(row):
    return row['date'], row['home'], row['away']


def parse_recent_csv(body, team_name):
    reporting = recent_team(team_name)
    reader = csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
    if not RAW_COLUMNS.issubset(reader.fieldnames or []):
        raise ValueError('Recent xG CSV is missing raw match columns')
    if len(reader.fieldnames) != len(set(reader.fieldnames)):
        raise ValueError('Duplicate recent xG CSV header')
    result = []; seen = set()
    for row in reader:
        if row.get('Comp') != 'Premier League':
            raise ValueError('Non-EPL row in recent xG cache')
        when = date.fromisoformat(row['Date'])
        if when.isoformat() != row['Date']:
            raise ValueError('Recent xG date must be an ISO calendar date')
        venue = integer(row['Venue'], 'venue', 1)
        opponent = recent_team(row['Opponent'])
        gf = integer(row['GF'], 'goals for', 30)
        ga = integer(row['GA'], 'goals against', 30)
        home, away = (reporting, opponent) if venue else (opponent, reporting)
        hg, ag = (gf, ga) if venue else (ga, gf)
        hx, ax = (row['XG_for'], row['XG_against']) if venue else (row['XG_against'], row['XG_for'])
        item = validate_match({'date': row['Date'], 'home': home, 'away': away,
                               'home_goals': hg, 'away_goals': ag,
                               'home_xg': hx, 'away_xg': ax,
                               'season': when.year if when.month >= 7 else when.year - 1,
                               'source_match_id': None})
        if key(item) in seen:
            raise ValueError('Duplicate fixture within one recent team CSV')
        seen.add(key(item)); result.append(item)
    return result


def same_observation(first, second, tolerance):
    identity = ('date', 'home', 'away', 'home_goals', 'away_goals', 'season')
    return all(first[name] == second[name] for name in identity) and all(
        abs(first[name] - second[name]) <= tolerance for name in ('home_xg', 'away_xg'))


def _manifest_entries(source_manifest):
    if source_manifest is None:
        return None
    entries = json.loads(Path(source_manifest).read_text()) if isinstance(source_manifest, (str, Path)) else source_manifest
    if not isinstance(entries, list):
        raise ValueError('Recent source manifest must contain a list')
    result = {}
    for entry in entries:
        path = Path(entry['path'])
        if path.parent.as_posix() != 'data/processed' or path.suffix != '.csv' or path.name in result:
            raise ValueError('Invalid recent source manifest path')
        expected_url = f'https://raw.githubusercontent.com/{REPOSITORY}/{REVISION}/' + urllib.parse.quote(path.as_posix())
        if entry.get('url') != expected_url:
            raise ValueError('Recent source is not pinned to the registered revision')
        result[path.name] = entry
    if {Path(name).stem for name in result} != EXPECTED_TEAMS:
        raise ValueError('Pinned manifest does not contain the expected twenty team CSVs')
    return result


def download_pinned(directory, source_manifest):
    """Download only the twenty registered files, verifying before replacement."""
    entries = _manifest_entries(source_manifest)
    if entries is None:
        raise ValueError('Downloading requires a pinned source manifest')
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    for name, entry in entries.items():
        request = urllib.request.Request(entry['url'], headers={'User-Agent': 'FootpredictResearch/1.0'})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(1_000_001)
        if len(body) != entry['size'] or len(body) > 1_000_000:
            raise ValueError('Pinned recent source size mismatch')
        digest = hashlib.sha256(body).hexdigest()
        blob = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
        if digest != entry['sha256'] or blob != entry['sha']:
            raise ValueError('Pinned recent source hash mismatch')
        temporary = directory / (name + '.tmp'); temporary.write_bytes(body)
        temporary.replace(directory / name)


def load_recent(directory, reference=None, source_manifest=None):
    """Read raw team CSVs, verify reverse copies and optionally reference scores.

    A source manifest enables exact pinned-file verification. Without one the
    return value explicitly remains unverified (useful for offline unit tests).
    """
    paths = sorted(Path(directory).glob('*.csv'))
    entries = _manifest_entries(source_manifest)
    if not paths:
        raise ValueError('No recent xG team CSVs')
    if entries is not None and {p.name for p in paths} != set(entries):
        raise ValueError('Recent CSV file set differs from pinned manifest')
    lookup = {}; reporting_teams = {}; inputs = []; total_rows = 0
    for path in paths:
        body = path.read_bytes(); digest = hashlib.sha256(body).hexdigest()
        blob = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
        if entries is not None:
            entry = entries[path.name]
            if digest != entry['sha256'] or blob != entry['sha'] or len(body) != entry['size']:
                raise ValueError('Recent CSV does not match pinned source: ' + path.name)
        rows = parse_recent_csv(body, path.stem); total_rows += len(rows)
        inputs.append({'file': path.name, 'sha256': digest, 'gitBlobSha1': blob,
                       'bytes': len(body), 'rawRows': len(rows)})
        for row in rows:
            fixture = key(row)
            if recent_team(path.stem) in reporting_teams.get(fixture, set()):
                raise ValueError('Duplicate recent xG reporting-team observation')
            if fixture in lookup and not same_observation(lookup[fixture], row, 1e-6):
                raise ValueError('Conflicting home/away copies of recent xG fixture: ' + str(fixture))
            lookup.setdefault(fixture, row)
            reporting_teams.setdefault(fixture, set()).add(recent_team(path.stem))
    rows = [lookup[k] for k in sorted(lookup)]
    if reference is not None:
        original = {}
        for observation in reference:
            match = observation.match
            if match.key in original:
                raise ValueError('Duplicate original fixture')
            original[match.key] = match
        for row in rows:
            match = original.get(key(row))
            if match is None:
                raise ValueError('Recent xG has no exact reference date/team fixture: ' + str(key(row)))
            if (row['home_goals'], row['away_goals'], row['season']) != (match.home_goals, match.away_goals, match.season):
                raise ValueError('Recent xG disagrees with original score or season: ' + str(key(row)))
    manifest = {'repository': REPOSITORY, 'revision': REVISION,
                'sourceUrl': f'https://github.com/{REPOSITORY}/tree/{REVISION}/data/processed',
                'pipelineUrl': f'https://github.com/{REPOSITORY}/blob/{REVISION}/src/scraping.py',
                'source': 'Secondary Understat-derived CSV cache; not a direct provider response',
                'pinnedFilesVerified': entries is not None, 'inputTeamRows': total_rows,
                'matches': len(rows), 'bothTeamCopies': sum(len(names) == 2 for names in reporting_teams.values()),
                'firstDate': rows[0]['date'] if rows else None, 'lastDate': rows[-1]['date'] if rows else None,
                'referenceScoresVerified': reference is not None,
                'bySeason': {str(y): sum(r['season'] == y for r in rows) for y in sorted({r['season'] for r in rows})},
                'usedColumns': sorted(RAW_COLUMNS), 'inputFiles': inputs,
                'availabilityCaveat': 'Pinned October 2026 snapshot supports retrospective research; it does not prove historical 24-hour availability.'}
    return rows, manifest


def merge_sources(archive, recent, tolerance=1e-4):
    """Prefer the primary archive and reject score or xG overlap disagreements."""
    if not 0 <= tolerance <= .01:
        raise ValueError('Invalid archive agreement tolerance')
    combined = {}; recent_lookup = {}; overlaps = []; maximum_difference = 0.
    for source, target in ((archive, combined), (recent, recent_lookup)):
        for value in source:
            row = validate_match(value)
            if key(row) in target:
                raise ValueError('Duplicate fixture inside an xG source')
            target[key(row)] = row
    for fixture, row in recent_lookup.items():
        if fixture in combined:
            original = combined[fixture]
            if not same_observation(original, row, tolerance):
                raise ValueError('Primary/recent xG disagreement: ' + str(fixture))
            maximum_difference = max(maximum_difference, *(abs(original[c] - row[c]) for c in ('home_xg', 'away_xg')))
            overlaps.append({'fixture': list(fixture), 'primarySourceMatchId': original['source_match_id'],
                             'primaryXg': [original['home_xg'], original['away_xg']],
                             'recentXg': [row['home_xg'], row['away_xg']]})
        else:
            combined[fixture] = row
    values = [combined[k] for k in sorted(combined)]
    return values, {'primaryMatches': len(combined) - len(recent_lookup) + len(overlaps),
                    'recentMatches': len(recent_lookup), 'overlappingMatches': len(overlaps),
                    'recentOnlyMatches': len(recent_lookup) - len(overlaps), 'combinedMatches': len(values),
                    'xgAgreementTolerance': tolerance, 'maximumOverlapDifference': maximum_difference,
                    'overlapEvidence': overlaps, 'overlapPreference': 'primary archive',
                    'recentSourceMatchIds': 'Unavailable; never fabricated as Understat IDs'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--source-manifest', required=True, type=Path)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--download', action='store_true', help='Fetch the exact files in the pinned source manifest first')
    args = parser.parse_args()
    if args.download:
        download_pinned(args.directory, args.source_manifest)
    reference, _ = load_performance(args.data_dir, range(2015, 2027))
    rows, manifest = load_recent(args.directory, reference, args.source_manifest)
    archive_packet = json.loads(args.archive.read_text())
    archive = archive_packet['matches'] if isinstance(archive_packet, dict) else archive_packet
    combined, merge = merge_sources(archive, rows)
    args.output.mkdir(parents=True, exist_ok=True)
    manifest['merge'] = merge
    normalized = (json.dumps(combined, indent=2, allow_nan=False) + '\n').encode()
    manifest['normalizedMatchesSha256'] = hashlib.sha256(normalized).hexdigest()
    manifest['primaryArchiveSha256'] = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    (args.output / 'xg-combined.json').write_bytes(normalized)
    (args.output / 'xg-recent-manifest.json').write_text(json.dumps(manifest, indent=2, allow_nan=False) + '\n')
    print(json.dumps({k: v for k, v in manifest.items() if k not in ('inputFiles', 'merge')}))
    print(json.dumps({k: v for k, v in merge.items() if k != 'overlapEvidence'}))


if __name__ == '__main__':
    main()
