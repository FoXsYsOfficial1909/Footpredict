"""Fixed, past-only features for a small residual network over Poisson v1.

Histories and Elo are local to a division.  No statistic of the target match
enters its predictors.  The target label is stored separately for later fitting.
The first match in a league is retained with uniform prior frequencies, unknown
team indicators, the existing fixed smoothed form priors, and zero coverage.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
import math

from research.data import DIVISIONS
from research.evaluate import base_distribution
from research.models import v1_statistics
from research.performance import FEATURES as PERFORMANCE_FEATURES, feature_state, vector


PROTOCOL = 'neural-features-1'
DIVISION_ORDER = ('E0', 'E1', 'E2')
COVERAGE_STATS = ('shots_for', 'shots_against', 'sot_for', 'sot_against')
FEATURE_NAMES = tuple(PERFORMANCE_FEATURES) + tuple(
    f'{side}_{stat}_coverage_{window}'
    for window in (5, 15) for side in ('home', 'away') for stat in COVERAGE_STATS
) + tuple(f'division_{d}' for d in DIVISION_ORDER) + (
    'log_base_home', 'log_base_draw', 'log_base_away', 'v1_available',
)


def _validate(games):
    """Reject ambiguous league inputs before computing any rolling state."""
    fixtures, club_days = set(), set()
    for game in games:
        observation = game.observation; match = observation.match
        if (game.division not in DIVISION_ORDER or type(match.day) is not date
                or type(match.season) is not int
                or not date(match.season, 7, 1) <= match.day <= date(match.season+1, 7, 31)
                or any(not isinstance(t, str) or not t for t in (match.home, match.away))
                or match.home == match.away
                or any(type(v) is not int or not 0 <= v <= 30
                       for v in (match.home_goals, match.away_goals))):
            raise ValueError('Invalid neural feature division, fixture or regulation score')
        key = (game.division, *match.key)
        if key in fixtures:
            raise ValueError('Duplicate neural feature fixture')
        fixtures.add(key)
        for team in (match.home, match.away):
            club_day = (game.division, match.day, team)
            if club_day in club_days:
                raise ValueError('A club cannot play twice in one division calendar day')
            club_days.add(club_day)
        values = (observation.home_shots, observation.away_shots,
                  observation.home_sot, observation.away_sot)
        if (any(v is not None and (type(v) not in (int, float) or not math.isfinite(v)
                                  or not 0 <= v <= 100) for v in values)
                or any(total is not None and on_target is not None and on_target > total
                       for total, on_target in ((observation.home_shots, observation.home_sot),
                                                (observation.away_shots, observation.away_sot)))):
            raise ValueError('Invalid neural feature shot statistic; preserve missing markers')


def _coverage(state, home, away):
    values = []
    for window in (5, 15):
        for team in (home, away):
            history = state['histories'].get(team, [])[-window:]
            for stat in COVERAGE_STATS:
                # A zero observation is covered; an absent observation is not.
                values.append(sum(row[stat] is not None for row in history)/len(history)
                              if history else 0.)
    return values


def _probabilities(values):
    if (len(values) != 3 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in values)
            or abs(math.fsum(values)-1) > 1e-8):
        raise ValueError('Invalid past-only base probability distribution')
    return values


def build_rows(games):
    """Return one chronological feature row per supplied completed league match.

    The source loader must verify the pinned corpus before calling this function.
    ``base`` is exact v1 where its required past home/away history exists; otherwise
    it is ``frequency``, the same smoothed, weighted past-league frequencies.
    No fixture is dropped because v1 is unavailable.  ``features`` has 56 fixed
    columns described by ``FEATURE_NAMES``.  Shot/form priors are the existing
    performance.vector constants, not estimates filled from later data.

    Every rolling state uses dates strictly before target date minus one day.
    ``history_counts`` counts all available earlier games within the division;
    ``recent_history_counts`` is capped at 15 because the form features use 5/15.
    ``latest_input_match`` and the explicit cutoff are provenance, not predictors.
    """
    games = list(games); _validate(games)
    result = []
    for division in DIVISION_ORDER:
        observations = sorted((g.observation for g in games if g.division == division),
                              key=lambda o: o.match.key)
        matches = [o.match for o in observations]
        previous_cutoff = None; state = None; counts = None
        frequency_cache = {}
        for observation in observations:
            match = observation.match; cutoff = match.day-timedelta(days=1)
            if cutoff != previous_cutoff:
                state = feature_state(observations, cutoff)
                counts = Counter(team for prior in matches if prior.day < cutoff
                                 for team in (prior.home, prior.away))
                previous_cutoff = cutoff
            if state['latestMatch'] is not None and state['latestMatch'] >= cutoff.isoformat():
                raise ValueError('Feature provenance crossed its historical information fence')
            cache_key = cutoff, match.season
            if cache_key not in frequency_cache:
                frequency_cache[cache_key] = _probabilities(base_distribution(matches, cutoff, match.season))
            frequency = list(frequency_cache[cache_key])
            old = v1_statistics(matches, cutoff, match.season, match.home, match.away)
            v1 = _probabilities(old['probabilities'].tolist()) if old is not None else None
            base = list(v1) if v1 is not None else list(frequency)
            values = list(vector(state, match.home, match.away, match.day))
            values.extend(_coverage(state, match.home, match.away))
            values.extend(float(d == division) for d in DIVISION_ORDER)
            values.extend(math.log(max(value, 1e-12)) for value in base)
            values.append(float(v1 is not None))
            if len(values) != len(FEATURE_NAMES) or not all(math.isfinite(v) for v in values):
                raise ValueError('Invalid fixed neural feature vector')
            result.append({
                'date': match.day.isoformat(), 'cutoff_date': cutoff.isoformat(),
                'season': match.season, 'division': division, 'league': DIVISIONS[division]['league'],
                'home': match.home, 'away': match.away, 'actual': match.outcome,
                'base': base, 'v1': v1, 'frequency': frequency, 'v1_available': v1 is not None,
                'features': [float(v) for v in values],
                'latest_input_match': state['latestMatch'],
                'history_counts': {'home': counts[match.home], 'away': counts[match.away]},
                'recent_history_counts': {side: len(state['histories'].get(team, []))
                                          for side, team in (('home', match.home), ('away', match.away))},
                'unknown_teams': [team for team in (match.home, match.away)
                                  if team not in state['histories']],
            })
    return sorted(result, key=lambda r: (r['date'], r['division'], r['home'], r['away']))
