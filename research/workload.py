"""Past-only club workload with explicit all-competition coverage requirements.

Event dates and coverage bounds are calendar dates. The same conservative fence
as the forecast research excludes the target day and the immediately prior day.
Missing coverage never turns an unobserved cup game into a zero workload count.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from research.data import canonical
from research.nightly import sha


def as_day(value):
    if isinstance(value, datetime):
        raise ValueError('Workload dates must be calendar dates')
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError('A workload calendar date is required')
    return date.fromisoformat(value)


def team_name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('A workload club name is required')
    return canonical(value)


def normalize_event(row):
    when = as_day(row['date'])
    home, away = team_name(row['home']), team_name(row['away'])
    competition = row.get('competition')
    if home == away or not isinstance(competition, str) or not competition.strip():
        raise ValueError('Invalid workload fixture identity')
    played = row.get('played')
    if played is not None and not isinstance(played, bool):
        raise ValueError('Workload played flag must be boolean')
    status = row.get('status')
    if status not in ('played', 'scheduled', 'FINISHED', 'SCHEDULED', 'TIMED', None):
        raise ValueError('Unsupported workload fixture status')
    status_played = status in ('played', 'FINISHED')
    if played is not None and status is not None and played != status_played:
        raise ValueError('Conflicting workload played/status fields')
    if played is None:
        if status is None:
            raise ValueError('An explicit played status is required')
        played = status_played
    minutes = row.get('minutes')
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int) or minutes not in (90, 120)):
        raise ValueError('Known workload minutes must be 90 or 120')
    minutes_known = row.get('minutes_known', minutes is not None)
    if not isinstance(minutes_known, bool) or minutes_known != (minutes is not None):
        raise ValueError('Conflicting workload minutes coverage')
    kickoff = row.get('kickoff')
    if kickoff is not None:
        parsed = datetime.fromisoformat(kickoff.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.astimezone(timezone.utc).date() != when:
            raise ValueError('Workload kickoff and UTC calendar date disagree')
        kickoff = parsed.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    return {'date': when.isoformat(), 'home': home, 'away': away,
            'competition': competition.strip(), 'played': played,
            'minutes': minutes, 'minutes_known': minutes_known,
            'source_id': row.get('source_id'), 'source_url': row.get('source_url'),
            'kickoff': kickoff}


def covers_window(intervals, start, end):
    cursor = start
    for left, right in sorted(intervals):
        if right < cursor:
            continue
        if left > cursor:
            return False
        cursor = max(cursor, right + timedelta(days=1))
        if cursor > end:
            return True
    return cursor > end


def team_coverage(coverage, club, start, end):
    required = coverage.get('requiredCompetitions') or []
    if not isinstance(required, list) or any(not isinstance(c, str) or not c.strip() for c in required):
        raise ValueError('Required workload competitions must be an explicit list')
    required = sorted(set(required))
    verified = coverage.get('requiredCompetitionsVerified') is True
    scopes = coverage.get('scopes') or []
    if not isinstance(scopes, list):
        raise ValueError('Workload coverage scopes must be a list')
    by_competition = {competition: [] for competition in required}
    references = []
    for scope in scopes:
        competition = scope.get('competition')
        scoped_club = scope.get('team')
        if competition not in by_competition or (scoped_club is not None and team_name(scoped_club) != club):
            continue
        left, right = as_day(scope['start']), as_day(scope['end'])
        if right < left:
            raise ValueError('Workload coverage interval is reversed')
        if scope.get('complete') is True:
            by_competition[competition].append((left, right))
            if left <= end and right >= start:
                references.append(dict(scope))
    missing = [competition for competition, intervals in by_competition.items()
               if not covers_window(intervals, start, end)]
    complete = verified and bool(required) and not missing
    return {'allCompetitionsComplete': complete, 'requiredCompetitionsVerified': verified,
            'requiredCompetitions': required, 'missingCompetitions': missing,
            'lookbackFrom': start.isoformat(), 'lookbackThrough': end.isoformat(),
            'scopeReferences': references}


def workload_state(events, home, away, target_day, coverage=None):
    """Extract rest/count features only when their complete observation is proven.

    Coverage contract::

      {"requiredCompetitions": ["PL", "FA", "LC", ...],
       "requiredCompetitionsVerified": true,
       "scopes": [{"competition": "PL", "start": "2026-07-01",
                   "end": "2027-06-30", "complete": true, "team": "Arsenal"}]}

    Scope end dates are inclusive; an omitted team applies to every club.
    The required-competition universe must itself be verified. A list of only
    fetched competitions is insufficient evidence for all-competition coverage.
    """
    target = as_day(target_day)
    cutoff = target - timedelta(days=1)
    home, away = team_name(home), team_name(away)
    if home == away:
        raise ValueError('Workload target clubs must differ')
    coverage = coverage or {}
    if not isinstance(coverage, dict):
        raise ValueError('Workload coverage must be a manifest object')
    prior, seen, source_ids = [], set(), set()
    for value in events:
        # Discard excluded dates before inspecting their statistics or labels.
        if as_day(value['date']) >= cutoff:
            continue
        row = normalize_event(value)
        key = (row['date'], row['home'], row['away'])
        if key in seen:
            raise ValueError('Duplicate workload fixture')
        seen.add(key)
        if row['source_id'] is not None:
            source_key = (row['competition'], str(row['source_id']))
            if source_key in source_ids:
                raise ValueError('Duplicate workload source fixture ID')
            source_ids.add(source_key)
        if row['played']:
            prior.append(row)
    prior.sort(key=lambda row: (row['date'], row['home'], row['away'], row['competition']))
    result = {'targetDate': target.isoformat(), 'cutoffExclusive': cutoff.isoformat(),
              'definition': 'Eligible played games only; target day and preceding calendar day excluded.',
              'coverageManifestSha256': sha(coverage), 'eligibleEventsSha256': sha(prior)}
    for side, club in (('home', home), ('away', away)):
        games = [row for row in prior if club in (row['home'], row['away'])]
        last = games[-1] if games else None
        recent14 = [row for row in games if as_day(row['date']) >= target - timedelta(days=14)]
        recent7 = [row for row in recent14 if as_day(row['date']) >= target - timedelta(days=7)]
        proof = team_coverage(coverage, club, target - timedelta(days=14), cutoff - timedelta(days=1))
        complete = proof['allCompetitionsComplete']
        minutes_complete = all(row['minutes_known'] for row in recent14)
        known_extra = sum(row['minutes'] == 120 for row in recent14)
        since_last = (target - as_day(last['date'])).days if last is not None else None
        result[side] = {
            'team': club, 'daysRest': min(14, since_last) if complete and since_last is not None else 14 if complete else None,
            'matchesLast7Days': len(recent7) if complete else None,
            'matchesLast14Days': len(recent14) if complete else None,
            'extraTimeGamesLast14Days': known_extra if complete and minutes_complete else None,
            'restCappedAt14': (since_last is None or since_last >= 14) if complete else None,
            'coverage': {**proof, 'observedMinutesComplete': minutes_complete,
                         'extraTimeCoverageComplete': complete and minutes_complete},
            'observedLowerBounds': {'matchesLast7Days': len(recent7), 'matchesLast14Days': len(recent14),
                                    'extraTimeGamesLast14Days': known_extra},
            'unknownMinutesGamesLast14Days': sum(not row['minutes_known'] for row in recent14),
            'daysSinceLastKnownMatch': since_last, 'lastKnownMatch': last,
            'eligibleGamesLast14Days': recent14,
        }
    pairs = {'restDifference': 'daysRest', 'matches7Difference': 'matchesLast7Days',
             'matches14Difference': 'matchesLast14Days', 'extraTime14Difference': 'extraTimeGamesLast14Days'}
    result['vector'] = {name: result['home'][key] - result['away'][key]
                        if result['home'][key] is not None and result['away'][key] is not None else None
                        for name, key in pairs.items()}
    return result
