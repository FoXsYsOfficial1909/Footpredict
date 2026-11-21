"""Score existing past-only opponent-adjusted Poisson forecasts on the frozen BTTS cohort."""
import argparse
from datetime import date, timedelta
import gzip
from hashlib import sha256
import json
from pathlib import Path
import subprocess

import numpy as np

from research.goal_market_calibration import ROOT, inputs, paired_log_loss, predict, scores
from research.goal_market_dixon import both_score
from research.nightly import sha


PLAN = ROOT / 'docs/research/btts-opponents-plan-2026-11-21.json'


def checked_bytes(raw, expected):
    if sha256(raw).hexdigest() != expected:
        raise ValueError('Frozen input checksum mismatch')
    return raw


def join_rate(row, season, fixture, indexed):
    _, day, year, home, away, hg, ag = fixture[:7]
    rate = indexed[(day, home, away)]
    cutoff = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    if not (year == season == rate['season'] and day == row['day']
            and rate['rate_cutoff'] == cutoff and rate['latestTrainingDate'] < cutoff
            and (hg, ag) == (rate['home_goals'], rate['away_goals'])
            and row['score'] == {'home': hg, 'away': ag}):
        raise ValueError('Fixture, score or historical cutoff mismatch')
    return both_score(rate['home_rate'], rate['away_rate'], 0., 20)


def evaluate(rows, completed, weights, plan):
    poisson, base, actual = inputs(rows, plan['market'])
    opponent = np.array([row['opponent'] for row in rows])
    variants = {'poisson': poisson, 'base': base, 'calibrated': predict(poisson, base, weights),
                'opponent': opponent, 'opponentCalibrated': predict(opponent, base, weights)}
    metrics, losses = {}, {}
    for name, probabilities in variants.items():
        metrics[name], losses[name] = scores(probabilities, actual)
    days = [row['day'] for row in rows]
    paired = {name: paired_log_loss(days, losses, name, refs, plan) for name, refs in (
        ('opponent', ('poisson', 'calibrated', 'base')), ('opponentCalibrated', ('calibrated', 'base')))}
    return {'from': min(days), 'to': max(days), 'completed': completed, 'checked': len(rows),
            'coverage': len(rows) / completed, 'frequency': float(actual.mean()), 'models': metrics, 'pairedLogLoss': paired}


def run():
    plan = json.loads(PLAN.read_bytes())
    raw = subprocess.check_output(['node', 'scripts/replay_goal_markets.mjs', '--score-rows'], cwd=ROOT)
    replay = json.loads(checked_bytes(raw, plan['replaySha256']))
    source = replay['source']
    calendar = json.loads(gzip.decompress(checked_bytes((ROOT / source['file']).read_bytes(), source['sha256'])))
    fixtures = [game for game in calendar if game[0] == 'E0']
    artifact = ROOT / 'docs/research/goal-markets-2026-11-19/calibration.json'
    weights = json.loads(checked_bytes(artifact.read_bytes(), plan['calibrationSha256']))['models'][plan['market']]
    indexed = {}
    for label, digest in plan['rateHashes'].items():
        archive = json.loads((ROOT / plan['rateArchive'] / (label + '-predictions.json')).read_bytes())
        if sha(archive) != digest:
            raise ValueError('Archived rolling rate pairs changed')
        for rate in archive:
            key = (rate['date'], rate['home'], rate['away'])
            if key in indexed:
                raise ValueError('Duplicate archived match')
            indexed[key] = rate

    pooled, periods = [], []
    for period in replay['periods']:
        year = period['season']
        if year not in plan['seasons']:
            continue
        rows = period['rows']
        for row in rows:
            row['opponent'] = join_rate(row, year, fixtures[row['fixture'] - 1], indexed)
        periods.append({'season': year, **evaluate(rows, period['completed'], weights, plan)})
        pooled.extend(rows)
    completed = sum(p['completed'] for p in periods)
    if len(indexed) != completed or [p['season'] for p in periods] != plan['seasons']:
        raise ValueError('Unexpected league or season coverage')
    summary = evaluate(pooled, completed, weights, plan)
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
