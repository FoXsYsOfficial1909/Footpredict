"""Compare two history half-lives on the frozen BTTS cohort."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import gzip
import json
from pathlib import Path
import subprocess

import numpy as np

from research.goal_market_calibration import ROOT, inputs, paired_log_loss, predict, scores
from research.goal_market_dixon import both_score
from research.goal_market_opponents import checked_bytes


PLAN = ROOT / 'docs/research/btts-half-life-plan-2026-11-24.json'


def past_games(day, history, plan):
    # The date-only replay assigns a common kickoff hour. Its 28-hour cutoff
    # excludes the target day and preceding day.
    cutoff_hours = plan['leadHours'] + plan['resultSafetyHours']
    last_day = day - timedelta(days=(cutoff_hours + 23) // 24)
    return [game for game in history if game[0] <= last_day]


def weighted_signals(target, history, plan, half_life_days):
    day, season, host, visitor = target[1:5]
    day = date.fromisoformat(day)
    earlier = past_games(day, history, plan)
    goals = np.array([game[4:6] for game in earlier])
    ages = np.array([(day - game[0]).days for game in earlier])
    settings = plan['settings']
    season_weights = [1. if game[1] == season else settings['priorSeasonWeight'] for game in earlier]
    weights = np.exp(-np.log(2) * ages / half_life_days) * season_weights
    means = np.average(goals, axis=0, weights=weights)
    shrinkage = settings['shrinkageGames']

    def smooth(mask):
        return (weights[mask] @ goals[mask] + shrinkage * means) / (weights[mask].sum() + shrinkage)

    host_rates = smooth(np.array([game[2] == host for game in earlier]))
    visitor_rates = smooth(np.array([game[3] == visitor for game in earlier]))
    rates = np.clip(host_rates * visitor_rates / means, *plan['rateBounds'])
    # Match the Worker's one pseudo-observation for each binary outcome.
    frequency = (1 + weights[(goals > 0).all(axis=1)].sum()) / (2 + weights.sum())
    return rates, float(frequency)


def evaluate(rows, completed, weights, plan):
    poisson, base, actual = inputs(rows, plan['market'])
    poisson_long = np.array([row['longPoisson'] for row in rows])
    base_long = np.array([row['longFrequency'] for row in rows])
    variants = {'base': base, 'poisson': poisson, 'calibrated120': predict(poisson, base, weights),
                'base365': base_long, 'poisson365': poisson_long,
                'scoring365Only': predict(poisson_long, base, weights),
                'frequency365Only': predict(poisson, base_long, weights),
                'calibrated365': predict(poisson_long, base_long, weights)}
    metrics, losses = {}, {}
    for name, probabilities in variants.items():
        metrics[name], losses[name] = scores(probabilities, actual)
    days = [row['day'] for row in rows]
    paired = {name: paired_log_loss(days, losses, name, ('calibrated120',), plan)['calibrated120']
              for name in ('scoring365Only', 'frequency365Only', 'calibrated365')}
    return {'from': min(days), 'to': max(days), 'completed': completed, 'checked': len(rows),
            'coverage': len(rows) / completed, 'frequency': float(actual.mean()),
            'models': metrics, 'pairedLogLossAgainst120': paired}


def load_replay(plan):
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
    return replay, fixtures, seasons, calibration['models'][plan['market']]


def run():
    plan = json.loads(PLAN.read_bytes())
    replay, fixtures, seasons, weights = load_replay(plan)
    market = plan['market']

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
            rates, frequency = weighted_signals(target, history, plan, plan['settings']['halfLifeDays'])
            if (not np.allclose(rates, row['expectedGoals'], rtol=0, atol=1e-12)
                    or abs(frequency - row['base'][market]) > 1e-12
                    or abs(both_score(*rates.tolist(), 0., plan['scoreLimit']) - row['poisson'][market]) > 1e-12):
                raise ValueError('The 120-day control does not reproduce the Worker')
            rates, frequency = weighted_signals(target, history, plan, plan['candidateHalfLifeDays'])
            row['longPoisson'] = both_score(*rates.tolist(), 0., plan['scoreLimit'])
            row['longFrequency'] = frequency
        periods.append({'season': season, **evaluate(rows, period['completed'], weights, plan)})
        pooled.extend(rows)
    if [period['season'] for period in periods] != plan['seasons']:
        raise ValueError('Unexpected season coverage')
    summary = evaluate(pooled, sum(period['completed'] for period in periods), weights, plan)
    models = summary['models']
    passed = (summary['pairedLogLossAgainst120']['calibrated365']['ci95'][1] < 0
              and models['calibrated365']['brier'] < models['calibrated120']['brier']
              and models['calibrated365']['logLoss'] < models['base']['logLoss'])
    return {'protocol': plan, 'source': replay['source'], 'prospective': False, 'historicalGatePassed': passed,
            'periods': periods, 'pooled': summary}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n', encoding='utf-8')
