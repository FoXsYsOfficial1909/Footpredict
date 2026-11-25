"""Compare two smoothing strengths on the frozen BTTS cohort."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
from pathlib import Path

import numpy as np

from research.goal_market_calibration import ROOT, inputs, paired_log_loss, predict, scores
from research.goal_market_dixon import both_score
from research.goal_market_half_life import load_replay, past_games, weighted_signals


PLAN = ROOT / 'docs/research/btts-shrinkage-plan-2026-11-25.json'


def has_limited_recent_history(target, history, plan):
    day = date.fromisoformat(target[1])
    bounds = plan['recentHistory']
    start = day - timedelta(days=bounds['windowDays'])
    recent = [game for game in past_games(day, history, plan) if game[0] >= start]
    home_games = sum(game[2] == target[3] for game in recent)
    away_games = sum(game[3] == target[4] for game in recent)
    return min(home_games, away_games) <= bounds['maximumGamesOnEitherSide']


def evaluate(rows, weights, plan):
    poisson, base, actual = inputs(rows, plan['market'])
    stronger = np.array([row['smoothedPoisson'] for row in rows])
    variants = {'base': base, 'poisson': poisson, 'poisson10': stronger,
                'calibrated5': predict(poisson, base, weights), 'calibrated10': predict(stronger, base, weights)}
    metrics, losses = {}, {}
    for name, probabilities in variants.items():
        metrics[name], losses[name] = scores(probabilities, actual)
    days = [row['day'] for row in rows]
    difference = paired_log_loss(days, losses, 'calibrated10', ('calibrated5',), plan)['calibrated5']
    return {'from': min(days), 'to': max(days), 'checked': len(rows), 'frequency': float(actual.mean()),
            'models': metrics, 'pairedLogLossAgainst5': difference}


def run():
    plan = json.loads(PLAN.read_bytes())
    replay, fixtures, seasons, weights = load_replay(plan)
    market = plan['market']
    half_life = plan['settings']['halfLifeDays']
    candidate = {**plan, 'settings': {**plan['settings'], 'shrinkageGames': plan['candidateShrinkageGames']}}
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
            rates, frequency = weighted_signals(target, history, plan, half_life)
            if (not np.allclose(rates, row['expectedGoals'], rtol=0, atol=1e-12)
                    or abs(frequency - row['base'][market]) > 1e-12
                    or abs(both_score(*rates.tolist(), 0., plan['scoreLimit']) - row['poisson'][market]) > 1e-12):
                raise ValueError('The 5-game control does not reproduce the Worker')
            rates, other_frequency = weighted_signals(target, history, candidate, half_life)
            if frequency != other_frequency:
                raise ValueError('Smoothing changed the league frequency')
            row['smoothedPoisson'] = both_score(*rates.tolist(), 0., plan['scoreLimit'])
            row['limitedRecentHistory'] = has_limited_recent_history(target, history, plan)
        completed = period['completed']
        periods.append({'season': season, 'completed': completed, 'coverage': len(rows) / completed,
                        **evaluate(rows, weights, plan)})
        pooled.extend(rows)
    if [period['season'] for period in periods] != plan['seasons']:
        raise ValueError('Unexpected season coverage')
    completed = sum(period['completed'] for period in periods)
    summary = {'completed': completed, 'coverage': len(pooled) / completed, **evaluate(pooled, weights, plan)}
    subgroups = {}
    for name, limited in [('limitedRecentHistory', True), ('otherFixtures', False)]:
        group = [row for row in pooled if row['limitedRecentHistory'] == limited]
        subgroups[name] = evaluate(group, weights, plan)
    models = summary['models']
    passed = (summary['pairedLogLossAgainst5']['ci95'][1] < 0
              and models['calibrated10']['brier'] < models['calibrated5']['brier']
              and models['calibrated10']['logLoss'] < models['base']['logLoss'])
    return {'protocol': plan, 'source': replay['source'], 'prospective': False, 'historicalGatePassed': passed,
            'periods': periods, 'pooled': summary, 'subgroups': subgroups}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + '\n', encoding='utf-8')
