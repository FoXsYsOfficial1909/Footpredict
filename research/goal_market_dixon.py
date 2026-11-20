"""Run a fixed Dixon--Coles correction on the Worker goal-market replay."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from hashlib import sha256
import json
import math
from pathlib import Path
import subprocess

import numpy as np

from research.dixon_coles import fit_rho, score_matrix
from research.goal_market_calibration import ROOT, chronological_split, inputs, paired_log_loss, predict, scores


PLAN = ROOT / 'docs/research/btts-dixon-plan-2026-11-20.json'


def score_rate_pair(row):
    day = date.fromisoformat(row['day'])
    home, away = row['expectedGoals']
    return {'date': row['day'], 'rate_cutoff': (day - timedelta(days=1)).isoformat(),
            'home_goals': row['score']['home'], 'away_goals': row['score']['away'],
            'home_rate': home, 'away_rate': away}


def both_score(home, away, rho, limit):
    matrix = score_matrix(home, away, rho, limit)
    return math.fsum(probability for row in matrix[1:] for probability in row[1:])


def evaluate(rows, completed, plan, frozen_weights, rho):
    poisson, base, actual = inputs(rows, plan['market'])
    rates = [row['expectedGoals'] for row in rows]
    limit = plan['scoreLimit']
    zero = np.array([both_score(home, away, 0., limit) for home, away in rates])
    if not np.allclose(zero, poisson, rtol=0, atol=1e-12):
        raise ValueError('Independent score matrices do not reproduce the Worker reference')
    dixon = np.array([both_score(home, away, rho, limit) for home, away in rates])
    variants = {'poisson': poisson, 'base': base, 'calibrated': predict(poisson, base, frozen_weights),
                'dixon': dixon, 'calibratedDixon': predict(dixon, base, frozen_weights)}
    metrics, losses = {}, {}
    for name, probabilities in variants.items():
        metrics[name], losses[name] = scores(probabilities, actual)
    days = [row['day'] for row in rows]
    paired = {candidate: paired_log_loss(days, losses, candidate, references, plan) for candidate, references in (
        ('dixon', ('poisson', 'base')), ('calibratedDixon', ('calibrated', 'base')))}
    return {'from': min(days), 'to': max(days), 'completed': completed, 'checked': len(rows),
            'coverage': len(rows) / completed, 'frequency': float(actual.mean()), 'models': metrics, 'pairedLogLoss': paired}


def experiment(replay, goal_plan, plan, reference):
    periods, train_rows = chronological_split(replay, goal_plan)
    if (replay['source'] != reference['source'] or reference['candidateVersion'] != goal_plan['candidateVersion']
            or replay['leadHours'] != 24):
        raise ValueError('The goal replay and frozen calibration do not describe the same protocol')
    frozen = reference['models'][plan['market']]
    model = fit_rho([score_rate_pair(row) for row in train_rows], plan['rhoFitCutoffExclusive'],
                    half_life=plan['rhoHalfLifeDays'], window_days=plan['rhoWindowDays'],
                    bounds=plan['rhoBounds'], minimum_matches=plan['minimumRhoMatches'])
    years = goal_plan['validationSeasons'] + goal_plan['laterReplaySeasons']

    def compare(rows, completed):
        return evaluate(rows, completed, plan, frozen, model['rho'])

    pooled = compare([row for year in years for row in periods[year]['rows']],
                     sum(periods[year]['completed'] for year in years))
    results = pooled['models']
    gate = (pooled['pairedLogLoss']['calibratedDixon']['calibrated']['ci95'][1] < 0
            and results['calibratedDixon']['brier'] < results['calibrated']['brier']
            and results['calibratedDixon']['logLoss'] < results['base']['logLoss'])
    return {'schemaVersion': 1, 'protocol': plan, 'source': replay['source'], 'prospective': False,
            'referenceVersion': reference['candidateVersion'], 'leadHours': replay['leadHours'],
            'resultSafetyHours': replay['resultSafetyHours'], 'model': model, 'historicalGatePassed': bool(gate),
            'periods': [{'season': year, **compare(periods[year]['rows'], periods[year]['completed'])} for year in years],
            'pooled': pooled}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    plan_bytes = PLAN.read_bytes()
    plan = json.loads(plan_bytes)
    goal_plan_bytes = (ROOT / plan['goalPlan']).read_bytes()
    reference_bytes = (ROOT / plan['referenceArtifact']).read_bytes()
    if sha256(reference_bytes).hexdigest() != plan['referenceSha256']:
        raise ValueError('Frozen calibration has changed')
    reference = json.loads(reference_bytes)
    if sha256(goal_plan_bytes).hexdigest() != reference['planSha256']:
        raise ValueError('Goal-market plan has changed')
    replay_bytes = subprocess.check_output(['node', 'scripts/replay_goal_markets.mjs', '--score-rows'], cwd=ROOT)
    report = experiment(json.loads(replay_bytes), json.loads(goal_plan_bytes), plan, reference)
    report['replaySha256'] = sha256(replay_bytes).hexdigest()
    report['planSha256'] = sha256(plan_bytes).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
