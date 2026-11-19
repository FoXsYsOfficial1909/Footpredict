"""Check whether a short gap between Premier League games improves BTTS forecasts."""
from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import subprocess

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logit

from research.goal_market_calibration import ROOT, chronological_split, inputs, paired_log_loss, predict, scores


PLAN = ROOT / 'docs/research/btts-rest-plan-2026-11-19.json'


def short_gap_teams(row, short_days):
    dates = row['lastLeagueMatch']
    target = date.fromisoformat(row['day'])
    gaps = [(target - date.fromisoformat(day)).days for day in (dates['home'], dates['away']) if day is not None]
    if any(gap < 2 for gap in gaps):
        raise ValueError('Last league match violates the pre-match cutoff')
    return sum(gap <= short_days for gap in gaps) if len(gaps) == 2 else None


def series(rows, plan, frozen_weights):
    poisson, base, actual = inputs(rows, plan['market'])
    calibrated = predict(poisson, base, frozen_weights)
    counts = [short_gap_teams(row, plan['shortGapDays']) for row in rows]
    known = np.array([count is not None for count in counts], dtype=bool)
    n_short = np.array([count or 0 for count in counts])
    return {'poisson': poisson, 'base': base, 'calibrated': calibrated}, actual, known, n_short


def fit_residual(calibrated, actual, n_short, ridge):
    if not n_short.any() or not 0 < actual.sum() < len(actual) or ridge <= 0:
        raise ValueError('Both outcomes, short-gap examples and positive regularization are required')
    offset = logit(calibrated)

    def objective(theta):
        logits = offset + n_short * theta[0]
        error = np.logaddexp(0, logits) - actual * logits
        gradient = np.mean(n_short * (expit(logits) - actual)) + ridge * theta[0]
        return error.mean() + ridge * theta[0] ** 2 / 2, np.array([gradient])

    result = minimize(objective, np.zeros(1), jac=True, method='L-BFGS-B',
                      options={'ftol': 1e-13, 'gtol': 1e-9})
    if not result.success or not np.all(np.isfinite(result.x)):
        raise RuntimeError('Short-gap fit did not converge')
    return {'coefficient': float(result.x[0]), 'iterations': result.nit,
            'initialObjective': float(objective([0.])[0]), 'fittedObjective': float(result.fun)}


def adjust(calibrated, n_short, coefficient):
    return np.where(n_short > 0, expit(logit(calibrated) + n_short * coefficient), calibrated)


def summarize(days, variants, actual, plan):
    if not len(actual):
        return {'checked': 0, 'models': None, 'pairedLogLoss': None}
    metrics, losses = {}, {}
    for name, probabilities in variants.items():
        metrics[name], losses[name] = scores(probabilities, actual)
    return {'checked': len(actual), 'models': metrics,
            'pairedLogLoss': paired_log_loss(days, losses, 'candidate', ('calibrated', 'base', 'poisson'), plan)}


def evaluate(rows, completed, plan, frozen, coefficient):
    variants, actual, known, n_short = series(rows, plan, frozen)
    variants['candidate'] = adjust(variants['calibrated'], n_short, coefficient)
    days = np.array([row['day'] for row in rows])
    short = n_short > 0
    return {'from': str(min(days)), 'to': str(max(days)), 'completed': completed,
            'coverage': len(rows) / completed, 'gapAvailable': int(known.sum()),
            'gapMissing': int((~known).sum()), **summarize(days, variants, actual, plan),
            'shortGap': summarize(days[short], {key: values[short] for key, values in variants.items()}, actual[short], plan)}


def experiment(replay, goal_plan, plan, reference):
    periods, train_rows = chronological_split(replay, goal_plan)
    frozen = reference['models'][plan['market']]
    if replay['source'] != reference['source'] or reference['candidateVersion'] != goal_plan['candidateVersion']:
        raise ValueError('Replay and frozen calibration use different sources or versions')
    variants, actual, known, n_short = series(train_rows, plan, frozen)
    model = fit_residual(variants['calibrated'], actual, n_short, plan['ridge'])
    years = goal_plan['validationSeasons'] + goal_plan['laterReplaySeasons']
    pooled_rows = [row for year in years for row in periods[year]['rows']]

    def compare(rows, completed):
        return evaluate(rows, completed, plan, frozen, model['coefficient'])

    return {'schemaVersion': 1, 'protocol': plan, 'source': replay['source'], 'prospective': False,
            'referenceVersion': reference['candidateVersion'], 'leadHours': replay['leadHours'],
            'resultSafetyHours': replay['resultSafetyHours'],
            'training': {'from': min(row['day'] for row in train_rows), 'to': max(row['day'] for row in train_rows),
                         'checked': len(train_rows), 'gapAvailable': int(known.sum()),
                         'shortGap': int((n_short > 0).sum()), **model},
            'periods': [{'season': year, **compare(periods[year]['rows'], periods[year]['completed'])} for year in years],
            'pooled': compare(pooled_rows, sum(periods[year]['completed'] for year in years))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    plan_bytes = PLAN.read_bytes()
    plan = json.loads(plan_bytes)
    goal_plan = json.loads((ROOT / plan['goalPlan']).read_bytes())
    reference_bytes = (ROOT / plan['referenceArtifact']).read_bytes()
    if sha256(reference_bytes).hexdigest() != plan['referenceSha256']:
        raise ValueError('Frozen calibration has changed')
    replay_bytes = subprocess.check_output(['node', 'scripts/replay_goal_markets.mjs', '--rest-rows'], cwd=ROOT)
    report = experiment(json.loads(replay_bytes), goal_plan, plan, json.loads(reference_bytes))
    report['replaySha256'] = sha256(replay_bytes).hexdigest()
    report['planSha256'] = sha256(plan_bytes).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
