"""Normalize public official-competition match histories for workload research.

ESPN snapshots are evidence of recorded games, not proof of complete coverage.
Coverage is declared separately and remains partial until independently checked.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import urllib.request

from research.performance import load_performance
from research.xg_data import integer, team


COMPETITIONS = (
    'eng.1', 'eng.2', 'eng.fa', 'eng.league_cup', 'eng.charity',
    'uefa.champions', 'uefa.europa', 'uefa.europa.conf',
    'uefa.champions_qual', 'uefa.europa_qual', 'uefa.europa.conf_qual',
    'uefa.super_cup', 'fifa.cwc', 'fifa.intercontinental_cup',
)
INCEPTION = {'uefa.europa.conf': 2021, 'uefa.europa.conf_qual': 2021,
             'fifa.intercontinental_cup': 2024}
EXCLUDED_COMPETITIONS = {'fifa.intercontinental.cup': 'Separate national-team tournament, not the FIFA club Intercontinental Cup'}
FINAL_STATUSES = {'STATUS_FULL_TIME', 'STATUS_FINAL_AET', 'STATUS_FINAL_PEN'}
MAX_RESPONSE = 12_000_000


def download_snapshots(directory, years, now=None):
    """Fetch bounded public snapshots; failed refreshes preserve stored files.

    Returns a source index, including unavailable queries. A caller must persist
    this index with the raw artifacts; successful retrieval alone sets no scope
    completeness flags. Only this explicit function performs network requests.
    """
    directory = Path(directory); directory.mkdir(exist_ok=True, parents=True)
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        raise ValueError('Snapshot timestamp must include a timezone')
    stamp = stamp.astimezone(timezone.utc)
    years = sorted({integer(year, 'calendar year', 2100) for year in years})
    if not years or any(year < 2000 or year > stamp.year for year in years):
        raise ValueError('Invalid workload download years')
    queries = [(comp, year) for year in years for comp in COMPETITIONS if year >= INCEPTION.get(comp, 2000)]
    def fetch(query):
        comp, year = query; url = scoreboard_url(comp, year)
        name = f'{comp}-{year}.json.gz'; path = directory / name
        entry = {'competition': comp, 'calendarYear': year, 'sourceUrl': url}
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'FootpredictResearch/1.0'})
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read(MAX_RESPONSE + 1)
            _, stats = parse_scoreboard(body, comp, year)
            entry.update(ok=True, rawSha256=hashlib.sha256(body).hexdigest(), rawBytes=len(body),
                         events=stats['inputEvents'], file=name,
                         retrievedAt=stamp.astimezone(timezone.utc).isoformat())
            temporary = directory / (name + '.tmp')
            temporary.write_bytes(gzip.compress(body, mtime=0)); temporary.replace(path)
            metadata = directory / f'{comp}-{year}.meta.json'
            metadata.write_text(json.dumps(entry, indent=2, allow_nan=False) + '\n')
        except Exception as error:
            entry.update(ok=False, error=type(error).__name__ + ': ' + str(error)[:240],
                         previouslyStoredFilePreserved=path.exists())
        return entry
    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(fetch, queries))
    return {'provider': 'ESPN public scoreboard', 'retrievedAt': stamp.astimezone(timezone.utc).isoformat(), 'queries': entries}


def scoreboard_url(competition, year):
    if competition not in COMPETITIONS and competition not in EXCLUDED_COMPETITIONS:
        raise ValueError('Unregistered workload competition')
    if not 2000 <= year <= 2100:
        raise ValueError('Invalid workload calendar year')
    return f'https://site.api.espn.com/apis/site/v2/sports/soccer/{competition}/scoreboard?dates={year}&limit=1000'


def status_minutes(status):
    """Known regulation duration; penalties alone never imply extra time."""
    kind = status['type']['name']
    if kind == 'STATUS_FINAL_AET':
        return 120
    if kind == 'STATUS_FULL_TIME' and status.get('period') == 2:
        return 90
    return None


def parse_scoreboard(body, competition, year):
    if len(body) > MAX_RESPONSE:
        raise ValueError('Workload response size limit exceeded')
    packet = json.loads(body)
    leagues = packet.get('leagues', [])
    if len(leagues) != 1 or leagues[0].get('slug') != competition:
        raise ValueError('Workload scoreboard league mismatch')
    events = packet.get('events')
    if not isinstance(events, list) or len(events) >= 1000:
        raise ValueError('Missing workload events or possible limit truncation')
    result = []; seen = set(); states = Counter(); excluded = []
    for event in events:
        identifier = str(integer(event['id'], 'event ID'))
        if identifier in seen:
            raise ValueError('Duplicate workload source event ID')
        seen.add(identifier)
        contests = event['competitions']
        if len(contests) != 1:
            raise ValueError('Ambiguous workload event competition')
        contest = contests[0]
        if str(contest['id']) != identifier:
            raise ValueError('Workload event/contest ID mismatch')
        status = contest['status']; kind = status['type']['name']; states[kind] += 1
        if status['type'].get('completed') is not True or kind not in FINAL_STATUSES:
            excluded.append({'source_id': 'ESPN:' + identifier, 'status': kind})
            continue
        if status['type'].get('state') != 'post':
            raise ValueError('Completed workload event is not in the past state')
        stamp = event['date']
        when = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        if when.tzinfo is None:
            raise ValueError('Workload kickoff lacks a timezone')
        when = when.astimezone(timezone.utc)
        if when.year != year:
            raise ValueError('Workload event is outside the requested calendar year')
        participants = contest['competitors']
        if len(participants) != 2 or {side['homeAway'] for side in participants} != {'home', 'away'}:
            raise ValueError('Workload event lacks two distinct home/away sides')
        sides = {side['homeAway']: side for side in participants}
        home = team(sides['home']['team']['displayName']); away = team(sides['away']['team']['displayName'])
        if home == away:
            raise ValueError('Identical workload clubs')
        minutes = status_minutes(status)
        result.append({'date': when.date().isoformat(), 'kickoff': when.isoformat().replace('+00:00', 'Z'),
                       'home': home, 'away': away, 'competition': competition,
                       'season': when.year if when.month >= 7 else when.year - 1,
                       'home_goals': integer(sides['home']['score'], 'home score', 30),
                       'away_goals': integer(sides['away']['score'], 'away score', 30),
                       'status': 'played', 'played': True, 'minutes': minutes, 'minutes_known': minutes is not None,
                       'source_id': 'ESPN:' + identifier,
                       'source_url': scoreboard_url(competition, year),
                       'event_url': f'https://www.espn.com/soccer/match/_/gameId/{identifier}',
                       'neutral_venue': contest.get('neutralSite') if isinstance(contest.get('neutralSite'), bool) else None,
                       'duration_evidence': {'status': kind, 'period': status.get('period'),
                                             'clock': status.get('clock'), 'displayClock': status.get('displayClock')}})
    result.sort(key=lambda row: (row['kickoff'], row['source_id']))
    return result, {'inputEvents': len(events), 'playedEvents': len(result), 'excludedEvents': excluded,
                    'statuses': dict(states), 'limit': 1000, 'limitReached': False,
                    'coverageIndependentlyVerified': False}


def verify_reference(events, reference):
    """Cross-check all available EPL dates and final scores without changing them."""
    original = {observation.match.key: observation.match for observation in reference}
    if len(original) != len(reference):
        raise ValueError('Duplicate reference match')
    matched = set(); conflicts = []; counts = Counter()
    for event in events:
        if event['competition'] != 'eng.1':
            continue
        key = (event['date'], event['home'], event['away'])
        match = original.get(key)
        if match is None or (event['home_goals'], event['away_goals']) != (match.home_goals, match.away_goals):
            conflicts.append({'fixture': list(key), 'source_id': event['source_id'],
                              'sourceScore': [event['home_goals'], event['away_goals']],
                              'referenceScore': [match.home_goals, match.away_goals] if match else None})
        else:
            matched.add(key); counts[str(match.season)] += 1
    return {'matched': len(matched), 'conflicts': conflicts, 'matchedBySeason': dict(sorted(counts.items())),
            'missingBySeason': {str(year): sum(observation.match.season == year and observation.match.key not in matched
                                                  for observation in reference)
                                for year in sorted({r.match.season for r in reference})}}


def load_snapshots(directory, source_index, reference=None):
    directory = Path(directory)
    index = json.loads(Path(source_index).read_text()) if isinstance(source_index, (str, Path)) else source_index
    result = []; inputs = []; failed = []; unique = {}; fixtures = set(); excluded_queries = []; queries = set()
    for entry in index['queries']:
        competition = entry['competition']; year = integer(entry['calendarYear'], 'calendar year', 2100)
        if (competition, year) in queries:
            raise ValueError('Duplicate workload snapshot query')
        queries.add((competition, year))
        expected_url = scoreboard_url(competition, year)
        if entry.get('sourceUrl') != expected_url:
            raise ValueError('Unregistered workload snapshot URL')
        if not entry.get('ok'):
            failed.append(entry); continue
        filename = f'{competition}-{year}.json.gz'
        if entry['file'] != filename:
            raise ValueError('Invalid workload snapshot file path')
        with gzip.open(directory / filename, 'rb') as stream:
            body = stream.read(MAX_RESPONSE + 1)
        if hashlib.sha256(body).hexdigest() != entry['rawSha256'] or len(body) != entry['rawBytes']:
            raise ValueError('Workload snapshot hash or size mismatch')
        rows, stats = parse_scoreboard(body, competition, year)
        if entry['events'] != stats['inputEvents']:
            raise ValueError('Workload snapshot event count mismatch')
        if competition in EXCLUDED_COMPETITIONS:
            excluded_queries.append({**entry, 'reason': EXCLUDED_COMPETITIONS[competition]})
            continue
        for row in rows:
            identifier = row['source_id']
            fixture = (row['competition'], row['date'], row['home'], row['away'])
            if identifier in unique or fixture in fixtures:
                raise ValueError('Duplicate workload event across snapshots')
            unique[identifier] = row; fixtures.add(fixture)
        result.extend(rows)
        inputs.append({**entry, 'normalization': stats})
    result.sort(key=lambda row: (row['kickoff'], row['source_id']))
    reference_check = verify_reference(result, reference) if reference is not None else None
    if reference_check and reference_check['conflicts']:
        raise ValueError('EPL workload disagrees with reference: ' + json.dumps(reference_check['conflicts'][:3]))
    counts = Counter((row['competition'], row['season']) for row in result)
    coverage = {'requiredCompetitions': list(COMPETITIONS), 'requiredCompetitionsVerified': False,
                'scopes': [], 'source': 'ESPN public official-competition scoreboards',
                'coverageCaveat': 'Complete query retrieval and EPL identity verification do not establish all-competition completeness.',
                'trackedCompetitions': list(COMPETITIONS), 'allCompetitionsComplete': False,
                'competitionSeasonCounts': [{'competition': comp, 'season': season, 'played': count}
                                             for (comp, season), count in sorted(counts.items())],
                'inputSnapshots': inputs, 'failedQueries': failed, 'referenceCheck': reference_check,
                'excludedCompetitionQueries': excluded_queries,
                'matches': len(result), 'unknownDurationMatches': sum(not row['minutes_known'] for row in result),
                'firstDate': result[0]['date'] if result else None, 'lastDate': result[-1]['date'] if result else None,
                'missingCompetitionBeforeInception': INCEPTION,
                'availabilityCaveat': 'Historical snapshots do not prove the exact source was available 24 hours before historical fixtures.'}
    for entry in inputs:
        comp, year = entry['competition'], entry['calendarYear']
        coverage['scopes'].append({'competition': comp, 'start': f'{year}-01-01', 'end': f'{year}-12-31',
                                   'complete': False, 'source_url': entry['sourceUrl'],
                                   'rawSha256': entry['rawSha256']})
    return result, coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--source-index', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--download', action='store_true', help='Explicitly refresh public raw snapshots before validation')
    parser.add_argument('--first-year', type=int, default=2019)
    parser.add_argument('--last-year', type=int, default=datetime.now(timezone.utc).year)
    args = parser.parse_args()
    if args.download:
        index = download_snapshots(args.directory, range(args.first_year, args.last_year + 1))
        args.source_index.parent.mkdir(exist_ok=True, parents=True)
        args.source_index.write_text(json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    reference, _ = load_performance(args.data_dir, range(2015, 2027))
    events, manifest = load_snapshots(args.directory, args.source_index, reference)
    normalized = (json.dumps(events, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    manifest['normalizedMatchesSha256'] = hashlib.sha256(normalized).hexdigest()
    args.output.mkdir(exist_ok=True, parents=True)
    (args.output / 'workload-events.json').write_bytes(normalized)
    (args.output / 'workload-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({key: value for key, value in manifest.items() if key not in ('inputSnapshots', 'scopes', 'competitionSeasonCounts')}))


if __name__ == '__main__':
    main()
