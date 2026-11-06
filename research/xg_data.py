"""Validate public Understat shots and join xG to verified match identities.

Archive xG is a post-match training observation, never a target-match input.
No score labels, dates or teams from the reference result data are replaced.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path

from research.data import canonical
from research.performance import load_performance

ARCHIVE_URL = 'https://github.com/JaseZiv/worldfootballR_data/releases/download/understat_shots/epl_shot_data.rds'


ALIASES = {
    'Manchester City': 'Man City', 'Manchester United': 'Man United',
    'Newcastle United': 'Newcastle', 'Nottingham Forest': "Nott'm Forest",
    'Tottenham Hotspur': 'Tottenham', 'West Ham United': 'West Ham',
    'Wolverhampton Wanderers': 'Wolves', 'West Bromwich Albion': 'West Brom',
    'Brighton and Hove Albion': 'Brighton', 'Brighton & Hove Albion': 'Brighton',
    'Leeds United': 'Leeds', 'Hull City': 'Hull', 'Coventry City': 'Coventry',
    'Leicester City': 'Leicester', 'Ipswich Town': 'Ipswich',
    'Norwich City': 'Norwich', 'Cardiff City': 'Cardiff',
    'Swansea City': 'Swansea', 'Queens Park Rangers': 'QPR',
    'Stoke City': 'Stoke', 'Huddersfield Town': 'Huddersfield',
    'Luton Town': 'Luton',
}


def team(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Missing xG team')
    name = canonical(value.strip())
    return ALIASES.get(name, name)


def integer(value, name, maximum=None):
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError('Invalid ' + name) from error
    if not math.isfinite(number) or number < 0 or number != int(number) or (maximum is not None and number > maximum):
        raise ValueError('Invalid ' + name)
    return int(number)


def day(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValueError('Missing xG date')
    # Understat dates are UTC timestamps; joining uses their calendar date.
    return date.fromisoformat(value[:10]).isoformat()


def xg_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError('Invalid xG value') from error
    if not math.isfinite(number) or not 0 <= number <= 15:
        raise ValueError('Invalid xG value')
    return number


def validate_match(row):
    result = {
        'date': day(row['date']), 'home': team(row['home']), 'away': team(row['away']),
        'home_goals': integer(row['home_goals'], 'home score', 30),
        'away_goals': integer(row['away_goals'], 'away score', 30),
        'home_xg': xg_number(row['home_xg']), 'away_xg': xg_number(row['away_xg']),
        'source_match_id': integer(row['source_match_id'], 'match ID') if row.get('source_match_id') is not None else None,
        'season': integer(row['season'], 'season', 2100),
    }
    if result['home'] == result['away']:
        raise ValueError('Identical xG teams')
    when = date.fromisoformat(result['date'])
    if not date(result['season'], 7, 1) <= when <= date(result['season'] + 1, 7, 31):
        raise ValueError('xG date outside declared season')
    return result


def aggregate_shots(records):
    """Deduplicate identical shot IDs, reject conflicts and sum each match side.

    A side with zero archived shots has zero xG only inside an otherwise valid
    match with consistent metadata. A completely absent match stays missing.
    """
    matches = {}; seen = {}; duplicates = 0; count = 0
    for row in records:
        count += 1
        if row.get('league', 'EPL') != 'EPL':
            raise ValueError('Non-EPL shot in EPL archive')
        match_id = integer(row['match_id'], 'match ID')
        shot_id = integer(row['id'], 'shot ID')
        side = row.get('h_a')
        alternate = row.get('home_away')
        if side is None or (isinstance(side, float) and math.isnan(side)):
            side = alternate
        elif alternate in ('h', 'a') and alternate != side:
            raise ValueError('Conflicting xG shot side fields')
        if side not in ('h', 'a'):
            raise ValueError('Invalid xG shot side')
        value = xg_number(row['xG'])
        if value > 1:
            raise ValueError('Shot xG exceeds one')
        metadata = {
            'date': day(row['date']), 'home': team(row['home_team']), 'away': team(row['away_team']),
            'home_goals': integer(row['home_goals'], 'home score', 30),
            'away_goals': integer(row['away_goals'], 'away score', 30),
            'season': integer(row['season'], 'season', 2100), 'source_match_id': match_id,
        }
        identity = (match_id, side, value, tuple(metadata.items()))
        if shot_id in seen:
            if seen[shot_id] != identity:
                raise ValueError('Conflicting duplicate xG shot ID: ' + str(shot_id))
            duplicates += 1
            continue
        seen[shot_id] = identity
        packet = matches.setdefault(match_id, {'metadata': metadata, 'h': [], 'a': []})
        if packet['metadata'] != metadata:
            raise ValueError('Conflicting xG match metadata: ' + str(match_id))
        packet[side].append(value)
    result = []; fixtures = set()
    for packet in matches.values():
        row = validate_match({**packet['metadata'], 'home_xg': math.fsum(packet['h']), 'away_xg': math.fsum(packet['a'])})
        key = (row['date'], row['home'], row['away'])
        if key in fixtures:
            raise ValueError('Multiple archive IDs for an xG fixture')
        fixtures.add(key); result.append(row)
    result.sort(key=lambda r: (r['date'], r['home'], r['away']))
    return result, {'inputShotRows': count, 'uniqueShots': len(seen), 'identicalDuplicateShots': duplicates,
                    'matches': len(result), 'firstDate': result[0]['date'] if result else None,
                    'lastDate': result[-1]['date'] if result else None}


def load_xg_matches(path):
    values = json.loads(Path(path).read_text())
    if isinstance(values, dict):
        values = values['matches']
    if not isinstance(values, list):
        raise ValueError('xG match archive must contain a list')
    return [validate_match(row) for row in values]


def merge_xg(observations, matches, *, strict=True):
    """Return observations using this source only and a complete join manifest.

    strict=False may audit conflicting fixtures, but still excludes them. Missing
    archive fixtures remain None, including existing xG from any other provider.
    """
    lookup = {}; used = set(); result = []; conflicts = []; seasons = {}
    for value in matches:
        row = validate_match(value); key = (row['date'], row['home'], row['away'])
        if key in lookup:
            raise ValueError('Duplicate normalized xG fixture')
        lookup[key] = row
    reference_keys = set()
    for observation in observations:
        match = observation.match; key = match.key
        if key in reference_keys:
            raise ValueError('Duplicate reference fixture')
        reference_keys.add(key)
        season = seasons.setdefault(str(match.season), {'referenceMatches': 0, 'matched': 0, 'missing': 0, 'scoreConflicts': 0})
        season['referenceMatches'] += 1
        archived = lookup.get(key)
        hxg = axg = None
        if archived is None:
            season['missing'] += 1
        elif (archived['home_goals'], archived['away_goals'], archived['season']) != (match.home_goals, match.away_goals, match.season):
            season['scoreConflicts'] += 1
            conflicts.append({'fixture': list(key), 'referenceScore': [match.home_goals, match.away_goals],
                              'archiveScore': [archived['home_goals'], archived['away_goals']],
                              'sourceMatchId': archived['source_match_id']})
        else:
            season['matched'] += 1; used.add(key)
            hxg, axg = archived['home_xg'], archived['away_xg']
        result.append(replace(observation, home_xg=hxg, away_xg=axg))
    manifest = {'source': 'Validated Understat match observations', 'providerMixing': False,
                'referenceMatches': len(result), 'archiveMatches': len(lookup), 'matched': len(used),
                'unmatchedArchiveMatches': len(set(lookup) - reference_keys),
                'scoreConflicts': conflicts, 'bySeason': seasons,
                'unmatchedArchiveSample': [list(k) for k in sorted(set(lookup) - reference_keys)[:20]]}
    # Show likely calendar-date disagreements for review; never move a source
    # observation to another date automatically, even when teams/scores agree.
    near_dates = []
    for match_key in sorted(set(lookup) - reference_keys):
        archived = lookup[match_key]
        source_day = date.fromisoformat(archived['date'])
        for observation in observations:
            match = observation.match
            if (match.home, match.away, match.season, match.home_goals, match.away_goals) == (
                    archived['home'], archived['away'], archived['season'], archived['home_goals'], archived['away_goals']):
                if abs((source_day - match.day).days) == 1:
                    near_dates.append({'archiveFixture': list(match_key), 'referenceFixture': list(match.key)})
    manifest['excludedNearDateCandidates'] = near_dates
    if strict and conflicts:
        raise ValueError('xG reference score/season conflict: ' + json.dumps(conflicts[:3]))
    return result, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rds', required=True, type=Path)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--first-season', type=int, default=2015)
    parser.add_argument('--last-season', type=int, default=2026)
    args = parser.parse_args()
    body = args.rds.read_bytes(); digest = hashlib.sha256(body).hexdigest()
    if digest != args.sha256:
        raise ValueError('Archive SHA-256 mismatch')
    import rdata
    import pandas as pd
    frame = rdata.read_rds(args.rds, default_encoding='latin1')
    columns = ['league', 'id', 'xG', 'h_a', 'home_away', 'season', 'match_id', 'home_team', 'away_team', 'home_goals', 'away_goals', 'date']
    records = (dict(zip(columns, (None if value is pd.NA else value for value in row)))
               for row in frame[columns].itertuples(index=False, name=None))
    matches, aggregation = aggregate_shots(records)
    observations, reference = load_performance(args.data_dir, range(args.first_season, args.last_season + 1))
    _, joined = merge_xg(observations, matches)
    args.output.mkdir(parents=True, exist_ok=True)
    normalized = (json.dumps(matches, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    (args.output / 'xg-matches.json').write_bytes(normalized)
    manifest = {'schemaVersion': 1, 'source': ARCHIVE_URL, 'archiveURL': ARCHIVE_URL,
                'archiveSha256': digest, 'archiveBytes': len(body), 'aggregation': aggregation,
                'normalizedMatchesSha256': hashlib.sha256(normalized).hexdigest(),
                'referenceSources': reference, 'join': joined}
    (args.output / 'xg-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'aggregation': aggregation, 'join': joined}, ensure_ascii=False))


if __name__ == '__main__':
    main()
