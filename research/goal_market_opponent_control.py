"""Measure the effect of opponent strength in the existing weighted goal model."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import gzip
import json
from pathlib import Path
import subprocess

import numpy as np

from research.goal_market_calibration import ROOT
from research.goal_market_dixon import both_score
from research.goal_market_opponents import checked_bytes, evaluate


PLAN = ROOT / 'docs/research/btts-opponent-control-plan-2026-11-22.json'


def team_factors(goals, weights, home, away, size, league_mean, pseudo_games):
    """Regress weighted scoring and conceding rates toward the league mean.

    With neutral exposures, this is the Worker smoothing formula. The same
    pseudo-games keep exposure and expected scoring positive in the joint fit.
    Alternating Poisson updates adjust each team's exposure for its opponents.
    """
    scaled_goals = weights * goals / league_mean
    home_total = np.bincount(home, weights=scaled_goals, minlength=size) + pseudo_games
    away_total = np.bincount(away, weights=scaled_goals, minlength=size) + pseudo_games

    def exposures(teams, opponents):
        return np.bincount(teams, weights=weights * opponents, minlength=size) + pseudo_games

    neutral = np.ones_like(weights)
    home_raw = home_total / exposures(home, neutral)
    away_raw = away_total / exposures(away, neutral)
    home_factor, away_factor = home_raw, away_raw
    for _ in range(500):
        next_home = home_total / exposures(home, away_factor[away])
        next_away = away_total / exposures(away, next_home[home])
        change = max(np.max(np.abs(next_home - home_factor)), np.max(np.abs(next_away - away_factor)))
        if change < 1e-11:
            return (home_raw, away_raw), (next_home, next_away)
        home_factor, away_factor = next_home, next_away
    raise RuntimeError('Opponent exposure fit did not converge')


def expected_goals(target, history, plan):
    day, season, host, visitor = target[1:5]
    day = date.fromisoformat(day)
    # The replay uses a common kickoff hour and excludes games within 28 hours.
    cutoff_hours = plan['leadHours'] + plan['resultSafetyHours']
    last_day = day - timedelta(days=(cutoff_hours + 23) // 24)
    earlier = [game for game in history if game[0] <= last_day]
    teams = sorted({team for game in earlier for team in game[2:4]} | {host, visitor})
    index = {team: i for i, team in enumerate(teams)}
    home = np.array([index[game[2]] for game in earlier])
    away = np.array([index[game[3]] for game in earlier])
    goals = np.array([game[4:6] for game in earlier])
    settings = plan['settings']
    age = np.array([(day - game[0]).days for game in earlier])
    season_weight = [1. if game[1] == season else settings['priorSeasonWeight'] for game in earlier]
    weights = np.exp(-np.log(2) * age / settings['halfLifeDays']) * season_weight
    means = np.average(goals, axis=0, weights=weights)
    home_rates = team_factors(goals[:, 0], weights, home, away, len(teams), means[0], settings['shrinkageGames'])
    away_rates = team_factors(goals[:, 1], weights, home, away, len(teams), means[1], settings['shrinkageGames'])
    host_index, visitor_index = index[host], index[visitor]
    return [np.clip([means[0] * home_factor[host_index] * away_defence[visitor_index],
                     means[1] * away_factor[visitor_index] * home_defence[host_index]], *plan['rateBounds'])
            for (home_factor, away_defence), (home_defence, away_factor) in zip(home_rates, away_rates, strict=True)]


def run():
    plan = json.loads(PLAN.read_bytes())
    raw = subprocess.check_output(['node', 'scripts/replay_goal_markets.mjs', '--score-rows'], cwd=ROOT)
    replay = json.loads(checked_bytes(raw, plan['replaySha256']))
    for key in ('settings', 'leadHours', 'resultSafetyHours'):
        if replay[key] != plan[key]:
            raise ValueError(f'Mismatched replay {key}')
    source = replay['source']
    calendar = gzip.decompress(checked_bytes((ROOT / source['file']).read_bytes(), source['sha256']))
    fixtures = [game for game in json.loads(calendar) if game[0] == 'E0']
    seasons = {}
    for _, day, year, home, away, hg, ag, *_ in fixtures:
        seasons.setdefault(year, []).append((date.fromisoformat(day), year, home, away, hg, ag))
    artifact = ROOT / 'docs/research/goal-markets-2026-11-19/calibration.json'
    calibration = json.loads(checked_bytes(artifact.read_bytes(), plan['calibrationSha256']))
    weights = calibration['models'][plan['market']]

    periods, pooled = [], []
    for period in replay['periods']:
        season = period['season']
        if season not in plan['seasons']:
            continue
        history = seasons[season] + seasons[season - 1]
        rows = period['rows']
        for row in rows:
            target = fixtures[row['fixture'] - 1]
            if target[1:3] != [row['day'], season] or target[5:7] != [row['score']['home'], row['score']['away']]:
                raise ValueError('Different fixture identity or score')
            neutral, adjusted = expected_goals(target, history, plan)
            if not np.allclose(neutral, row['expectedGoals'], rtol=0, atol=1e-12):
                raise ValueError('Neutral scoring rates do not reproduce the Worker rates')
            row['opponent'] = both_score(*adjusted.tolist(), 0., plan['scoreLimit'])
        periods.append({'season': season, **evaluate(rows, period['completed'], weights, plan)})
        pooled.extend(rows)
    if [period['season'] for period in periods] != plan['seasons']:
        raise ValueError('Unexpected season coverage')
    summary = evaluate(pooled, sum(period['completed'] for period in periods), weights, plan)
    models = summary['models']
    passed = (summary['pairedLogLoss']['opponentCalibrated']['calibrated']['ci95'][1] < 0
              and models['opponentCalibrated']['brier'] < models['calibrated']['brier']
              and models['opponentCalibrated']['logLoss'] < models['base']['logLoss'])
    return {'protocol': plan, 'source': source, 'prospective': False, 'historicalGatePassed': passed,
            'periods': periods, 'pooled': summary}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n', encoding='utf-8')
