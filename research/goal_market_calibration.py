"""Fit the prespecified binary corrections using the exact Worker replay."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
import subprocess

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logit


ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'docs/research/goal-market-calibration-plan-2026-11-19.json'
EPSILON = 1e-12


def inputs(rows, market):
    poisson = np.array([row['poisson'][market] for row in rows])
    base = np.array([row['base'][market] for row in rows])
    if any(np.any(~np.isfinite(p) | (p < 0) | (p > 1)) for p in (poisson, base)):
        raise ValueError('Invalid replay probability')
    actual = np.array([row['events'][market] for row in rows], dtype=float)
    return poisson, base, actual


def design(poisson, base):
    offset = logit(np.clip(base, EPSILON, 1 - EPSILON))
    contrast = logit(np.clip(poisson, EPSILON, 1 - EPSILON)) - offset
    return offset, np.column_stack([np.ones_like(offset), contrast])


def fit(rows, market, ridge):
    poisson, base, actual = inputs(rows, market)
    if not 0 < actual.sum() < len(actual) or ridge <= 0:
        raise ValueError('Both outcomes and positive regularization are required')
    offset, features = design(poisson, base)

    def objective(theta):
        logits = offset + features @ theta
        error = np.logaddexp(0, logits) - actual * logits
        gradient = features.T @ (expit(logits) - actual) / len(actual)
        return error.mean() + ridge * (theta @ theta) / 2, gradient + ridge * theta

    start = np.zeros(2)
    result = minimize(objective, start, method='L-BFGS-B', jac=True,
                      options={'ftol': 1e-13, 'gtol': 1e-9})
    if not result.success or not np.all(np.isfinite(result.x)):
        raise RuntimeError(f'{market}: calibration fit failed')
    intercept, slope = result.x.tolist()
    return {'intercept': intercept, 'slope': slope, 'ridge': ridge,
            'iterations': result.nit, 'initialObjective': float(objective(start)[0]),
            'fittedObjective': float(result.fun)}


def predict(poisson, base, weights):
    offset, features = design(poisson, base)
    return expit(offset + features @ [weights['intercept'], weights['slope']])


def scores(probabilities, actual):
    log_losses = -np.log(np.maximum(EPSILON, np.where(actual, probabilities, 1 - probabilities)))
    bins = []
    indexes = np.minimum(4, np.floor(probabilities * 5).astype(int))
    for idx in range(5):
        mask = indexes == idx
        bins.append({'n': int(mask.sum()), 'predicted': float(probabilities[mask].mean()) if mask.any() else None,
                     'observed': float(actual[mask].mean()) if mask.any() else None})
    return {'logLoss': float(log_losses.mean()), 'brier': float(np.mean(2 * (probabilities - actual)**2)),
            'accuracy': float(np.mean((probabilities >= .5) == actual)),
            'meanProbability': float(probabilities.mean()), 'calibration': bins}, log_losses


def paired_log_loss(days, losses, candidate, references, plan):
    days, by_day = np.unique(days, return_inverse=True)
    counts = np.bincount(by_day)
    rng = np.random.default_rng(plan['bootstrapSeed'])
    samples = rng.integers(len(days), size=(plan['bootstrapSamples'], len(days)))
    differences = {}
    for reference in references:
        delta = losses[candidate] - losses[reference]
        sums = np.bincount(by_day, weights=delta)
        draws = sums[samples].sum(axis=1) / counts[samples].sum(axis=1)
        differences[reference] = {'difference': float(delta.mean()),
                                  'ci95': np.quantile(draws, [.025, .975]).tolist()}
    return differences


def evaluate(period, models, plan):
    rows = period['rows']
    days = [row['day'] for row in rows]
    markets = {}
    for market, weights in models.items():
        poisson, base, actual = inputs(rows, market)
        variants = {'poisson': poisson, 'base': base, 'candidate': predict(poisson, base, weights)}
        metrics, losses = {}, {}
        for name, probabilities in variants.items():
            metrics[name], losses[name] = scores(probabilities, actual)
        differences = paired_log_loss(days, losses, 'candidate', ('poisson', 'base'), plan)
        markets[market] = {'frequency': float(actual.mean()), **metrics, 'pairedLogLoss': differences}
    fields = ('season', 'from', 'to', 'completed', 'coverage')
    return {**{key: period[key] for key in fields}, 'checked': len(rows),
            'skipped': len(period['skipped']), 'markets': markets}


def chronological_split(replay, plan):
    train = plan['trainSeasons']
    checks = plan['validationSeasons'] + plan['laterReplaySeasons']
    periods = {period['season']: period for period in replay['periods']}
    if train + checks != sorted(periods) or replay['league'] != plan['league']:
        raise ValueError('Unexpected replay seasons or league')
    rows = [row for year in train for row in periods[year]['rows']]
    latest = max(row['day'] for row in rows)
    earliest_check = min(date.fromisoformat(periods[year]['from']) for year in checks)
    if date.fromisoformat(latest) >= earliest_check - timedelta(days=1):
        raise ValueError('Training labels overlap the validation information cutoff')
    return periods, rows


def experiment(replay, plan):
    train = plan['trainSeasons']
    periods, rows = chronological_split(replay, plan)
    latest = max(row['day'] for row in rows)
    models = {market: fit(rows, market, plan['ridge']) for market in plan['markets']}
    return {'schemaVersion': 1, 'candidateVersion': plan['candidateVersion'], 'league': plan['league'],
            'source': replay['source'], 'settings': replay['settings'], 'leadHours': replay['leadHours'],
            'resultSafetyHours': replay['resultSafetyHours'], 'primaryMetric': plan['primaryMetric'],
            'brierScale': plan['brierScale'], 'prospective': False,
            'bootstrap': {'method': 'paired resampling by UTC calendar day', 'samples': plan['bootstrapSamples'],
                          'seed': plan['bootstrapSeed']},
            'training': {'from': min(row['day'] for row in rows), 'to': latest, 'checked': len(rows),
                         'completed': sum(periods[year]['completed'] for year in train)},
            'models': models, 'validation': [evaluate(periods[year], models, plan) for year in plan['validationSeasons']],
            'laterReplay': [evaluate(periods[year], models, plan) for year in plan['laterReplaySeasons']],
            'promotionPolicy': plan['promotionPolicy']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    replay_bytes = subprocess.check_output(['node', 'scripts/replay_goal_markets.mjs', '--training-rows'], cwd=ROOT)
    plan_bytes = PLAN.read_bytes()
    report = experiment(json.loads(replay_bytes), json.loads(plan_bytes))
    report['replaySha256'] = sha256(replay_bytes).hexdigest()
    report['planSha256'] = sha256(plan_bytes).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
