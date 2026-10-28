"""Freeze a chronological EPL cohort, archive pre-match evidence, derive unlabelled features.

Each invocation collects at most one missing fixture. The supervisor supplies the
interval; this command never loops on an exhausted upstream quota.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import urllib.error

from research.features import digest, feature_row, instant
from research.monitor import API, fetch
from scripts.overnight import atomic_json, locked_state


def immutable_json(path, value):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if Path(path).read_bytes() != body:
            raise RuntimeError('An immutable evidence file differs: ' + Path(path).name)
        return
    with os.fdopen(fd, 'wb') as target:
        target.write(body)


def select_cohort(games, now, count=10):
    eligible = [game for game in games if game.get('status') in ('TIMED', 'SCHEDULED') and
                isinstance(game.get('id'), int) and now + timedelta(hours=24) < instant(game['utcDate']) <= now + timedelta(days=7)]
    eligible.sort(key=lambda game: (instant(game['utcDate']), game['id']))
    selected = eligible[:count]
    if not selected:
        raise ValueError('No future Premier League fixtures at least 24 hours away')
    return [{'id': game['id'], 'kickoff': game['utcDate'], 'home': game['homeTeam']['name'], 'away': game['awayTeam']['name']} for game in selected]


def canonical_packet(packet):
    return {key: value for key, value in packet.items() if key not in
            ('cached', 'storedOnly', 'stale', 'refreshStatus', 'refreshReason')}


def run(root):
    root = Path(root)
    directory = root / 'data' / 'prospective'
    directory.mkdir(parents=True, exist_ok=True)
    for name in ('snapshots', 'features'):
        (directory / name).mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    plan_path = directory / 'PLAN.json'
    if not plan_path.exists():
        year = now.year if now.month >= 7 else now.year - 1
        body, source = fetch(API + f'/fd/competitions/PL/matches?season={year}', 4000000)
        games = json.loads(body)['matches']
        plan = {'created_at': now.isoformat(), 'source': source, 'selection': 'First ten upcoming EPL fixtures by kickoff, then fixture id; kickoff 24h–7d from registration.',
                'fixtures': select_cohort(games, now),
                'primary_prediction': 'First valid archived candidate snapshot, at least 24 hours before kickoff; never replaced based on outcome.',
                'secondary_snapshots': 'Later evidence may be archived, but does not replace the primary prediction.',
                'model_changes': 'No tuning or candidate promotion on this cohort. News remains outside model probabilities.',
                'metrics_after_settlement': ['multiclass Brier (sum over three outcomes)', 'natural-log loss', 'top-outcome accuracy'],
                'minimum_evidence_for_news_model': 'No news model is trained on these ten fixtures. Collect longitudinal data and validate by time on substantially more fixtures.'}
        plan['sha256'] = digest(plan)
        immutable_json(plan_path, plan)
    plan = json.loads(plan_path.read_text())
    index_path = directory / 'index.json'
    index = json.loads(index_path.read_text()) if index_path.exists() else {'plan_sha256': plan['sha256'], 'primary': {}, 'snapshots': []}
    if index['plan_sha256'] != plan['sha256']:
        raise RuntimeError('Cohort identity changed')
    pending = [game for game in plan['fixtures'] if str(game['id']) not in index['primary']]
    result = {'checked_at': now.isoformat(), 'cohort_size': len(plan['fixtures']), 'primary_predictions': len(index['primary'])}
    if pending:
        game = pending[0]
        if instant(game['kickoff']) - now < timedelta(hours=24):
            result.update(status='missed_primary_cutoff', fixture_id=game['id'])
        else:
            try:
                try:
                    body, _ = fetch(API + f"/v1/research/{game['id']}/stored", 500000)
                    packet = json.loads(body)
                    if packet.get('candidate', {}).get('status') != 'experimental':
                        raise ValueError('Stored snapshot has no frozen candidate')
                except (urllib.error.HTTPError, ValueError) as error:
                    if isinstance(error, urllib.error.HTTPError) and error.code != 404:
                        raise
                    body, _ = fetch(API + f"/v1/research/{game['id']}", 500000)
                    packet = json.loads(body)
                packet = canonical_packet(packet)
                row = feature_row(packet)
                if row['fixture_id'] != game['id'] or row['home'] != game['home'] or row['away'] != game['away']:
                    raise ValueError('Fixture identity differs from the registered cohort')
                if row['features']['horizon_hours'] < 24:
                    raise ValueError('Primary snapshot is less than 24 hours before kickoff')
                sha = row['snapshot_sha256']
                immutable_json(directory / 'snapshots' / (sha + '.json'), packet)
                immutable_json(directory / 'features' / (sha + '.json'), row)
                index['primary'][str(game['id'])] = {'snapshot_sha256': sha, 'collected_at': row['collected_at'],
                                                      'model_version': row['forecast']['model_version'], 'target': None}
                if sha not in index['snapshots']:
                    index['snapshots'].append(sha)
                atomic_json(index_path, index)
                result.update(status='archived', fixture_id=game['id'], primary_predictions=len(index['primary']),
                              ai_status=packet.get('ai', {}).get('status'), news_articles=len(packet.get('articles', [])))
            except (urllib.error.URLError, ValueError, KeyError) as error:
                result.update(status='deferred', fixture_id=game['id'], error_type=type(error).__name__, http=getattr(error, 'code', None))
    else:
        result['status'] = 'complete'
    atomic_json(directory / 'latest.json', result)
    with locked_state(root) as state:
        state['prospective_summary'] = result
        for artifact in (str(plan_path), str(index_path)):
            if artifact not in state.setdefault('artifacts', []):
                state['artifacts'].append(artifact)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result['status'] in ('deferred', 'missed_primary_cutoff'):
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    run(args.run_dir)
