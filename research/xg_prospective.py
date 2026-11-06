"""Freeze and settle an independent paired xG experiment on future EPL games."""
from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path

import numpy as np

from research.data import ALIASES, canonical
from research.evaluate import summarize
from research.export_model import export_elo
from research.models import LearnedPoisson, loss, score_probabilities
from research.nightly import sha
from research.xg_model import ExpectedGoalsPoisson

PROTOCOL = 'xg-paired-1'
MODEL_KEYS = ('xg', 'reference')


def instant(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('An explicit UTC-aware timestamp is required')
    return value.astimezone(timezone.utc)


def stamp(value):
    return instant(value).isoformat().replace('+00:00', 'Z')


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('A positive integer fixture/team ID is required')
    return value


def fixture_rows(provider):
    rows = provider.get('matches', []) if isinstance(provider, dict) else provider
    if not isinstance(rows, list):
        raise ValueError('Expected a provider fixture list')
    seen = set()
    for row in rows:
        key = identifier(row.get('id'))
        if key in seen:
            raise ValueError('Duplicate provider fixture ID')
        seen.add(key)
    return rows


def fixture_identity(row, provider):
    context = provider if isinstance(provider, dict) else {}
    competition = row.get('competition') or context.get('competition') or {}
    if competition.get('code') != 'PL' and competition.get('id') != 2021:
        raise ValueError('The fixture is not a verified EPL fixture')
    season = row.get('season') or {}
    year = int(season['startDate'][:4]) if season.get('startDate') else (context.get('filters') or {}).get('season')
    if year != 2026:
        raise ValueError('The fixture is outside the registered EPL season')
    home, away = row['homeTeam'], row['awayTeam']
    home_id, away_id = identifier(home.get('id')), identifier(away.get('id'))
    names = [canonical(team.get('name', '')) for team in (home, away)]
    if home_id == away_id or not all(names) or names[0] == names[1]:
        raise ValueError('Invalid fixture teams')
    return {'fixtureId': identifier(row.get('id')), 'utcDate': stamp(row['utcDate']),
            'homeId': home_id, 'awayId': away_id, 'home': names[0], 'away': names[1],
            'competition': 'PL', 'season': 2026}


def predict_export(model, home, away):
    glm, elo = model['glm'], model['elo']
    hs, aws = glm['teams'].get(home, {}), glm['teams'].get(away, {})
    h = math.exp(glm['intercept'] + glm['home_advantage'] + hs.get('attack', 0) - aws.get('defence', 0))
    a = math.exp(glm['intercept'] + aws.get('attack', 0) - hs.get('defence', 0))
    poisson = score_probabilities(max(.05, min(5, h)), max(.05, min(5, a)))
    delta = elo['ratings'].get(home, 1500.) - elo['ratings'].get(away, 1500.) + elo['parameters']['home_advantage']
    strength = 10 ** (delta / 800)
    values = np.array([strength, elo['parameters']['draw_weight'], 1 / strength])
    return (model['glm_weight'] * poisson + (1 - model['glm_weight']) * values / values.sum()).tolist()


def seal_packet(packet):
    sealed = copy.deepcopy(packet)
    sealed.pop('packetSha256', None)
    sealed['packetSha256'] = sha(sealed)
    return sealed


def verify_packet(packet):
    if packet.get('packetSha256') != seal_packet(packet)['packetSha256']:
        raise ValueError('Frozen packet digest mismatch')
    if packet.get('schemaVersion') != 1 or packet.get('protocol') != PROTOCOL or packet.get('promotedToDefault') is not False:
        raise ValueError('Invalid prospective protocol')
    frozen = instant(packet['frozenAt'])
    cutoff = frozen.date() - timedelta(days=1)
    if packet['trainingCutoffExclusive'] != cutoff.isoformat() or packet.get('minimumLeadHours') != 24:
        raise ValueError('Invalid frozen training or lead-time fence')
    if date.fromisoformat(packet['training']['latestTrainingMatch']) >= cutoff:
        raise ValueError('Training includes excluded dates')
    if packet['settingsSha256'] != sha(packet['settings']):
        raise ValueError('Frozen settings digest mismatch')
    for key in MODEL_KEYS:
        model = packet['models'][key]
        if (packet['modelSha256'][key] != sha(model) or model['glm']['as_of'] != cutoff.isoformat()
                or model['elo']['as_of'] != cutoff.isoformat() or model['as_of'] != frozen.date().isoformat()
                or model['training_cutoff_exclusive'] != cutoff.isoformat()):
            raise ValueError('Frozen model digest or cutoff mismatch')
    fixtures = packet['fixtures']
    if not 1 <= len(fixtures) <= 10:
        raise ValueError('Invalid frozen fixture count')
    seen = set()
    for fixture in fixtures:
        key = identifier(fixture['fixtureId'])
        if key in seen:
            raise ValueError('Duplicate frozen fixture ID')
        seen.add(key)
        lead = instant(fixture['utcDate']) - frozen
        if not timedelta(hours=24) <= lead <= timedelta(days=7):
            raise ValueError('Frozen fixture violates registration lead time')
        if fixture['predictionsSha256'] != sha(fixture['predictions']):
            raise ValueError('Frozen probabilities digest mismatch')
        for name in MODEL_KEYS:
            p = fixture['predictions'][name]
            loss(p, 0)  # Validate the complete distribution, including normalization.
            expected = predict_export(packet['models'][name], fixture['home'], fixture['away'])
            if not np.allclose(p, expected, rtol=0, atol=1e-12):
                raise ValueError('Frozen probabilities disagree with the frozen model')
    return True


def build_packet(observations, selected_settings, reference_settings,
                 fixtures_provider_json, now, source_hashes=None):
    now = instant(now)
    cutoff = now.date() - timedelta(days=1)
    available = sorted((row for row in observations if row.match.day < cutoff), key=lambda r: r.match.key)
    if len({row.match.key for row in available}) != len(available):
        raise ValueError('Duplicate training fixture')
    eligible = []
    for raw in fixture_rows(fixtures_provider_json):
        if raw.get('status') not in ('SCHEDULED', 'TIMED'):
            continue
        kickoff = instant(raw['utcDate'])
        if timedelta(hours=24) <= kickoff - now <= timedelta(days=7):
            eligible.append({**fixture_identity(raw, fixtures_provider_json), 'status': raw['status']})
    eligible.sort(key=lambda row: (row['utcDate'], row['fixtureId']))
    eligible = eligible[:10]
    if not eligible:
        raise ValueError('No eligible future EPL fixture with at least 24 hours lead')
    matches = [row.match for row in available]
    xg = ExpectedGoalsPoisson(goal_weight=selected_settings['goalWeight'],
        half_life=selected_settings['halfLife'], ridge=selected_settings['ridge'],
        window_days=selected_settings['windowDays']).fit(available, cutoff)
    reference = LearnedPoisson(**reference_settings['glm']).fit(matches, cutoff)
    elo = export_elo(matches, cutoff, reference_settings['elo'])
    models = {
        'xg': {'glm': xg.export(), 'elo': elo, 'glm_weight': selected_settings['poissonWeight']},
        'reference': {'glm': reference.export(), 'elo': elo, 'glm_weight': reference_settings['glm_weight']},
    }
    for name, model in models.items():
        model.update(as_of=now.date().isoformat(), training_cutoff_exclusive=cutoff.isoformat(),
                     aliases=ALIASES, status='experimental', promoted_to_default=False,
                     news_used_in_probabilities=False)
        model['features_used'] = ['past_match_results', 'opponent_strength', 'home_advantage']
        if name == 'xg':
            model['features_used'].append('past_expected_goals')
        model['model_version'] = 'ensemble-' + name + '-frozen-' + now.date().isoformat() + '-' + sha(model)[:12]
    for fixture in eligible:
        fixture['predictions'] = {key: predict_export(model, fixture['home'], fixture['away']) for key, model in models.items()}
        fixture['predictionsSha256'] = sha(fixture['predictions'])
        fixture['unseenTeams'] = {key: [name for name in (fixture['home'], fixture['away']) if name not in model['glm']['teams']]
                                  for key, model in models.items()}
    settings = {'xg': selected_settings, 'reference': reference_settings}
    packet = {'schemaVersion': 1, 'protocol': PROTOCOL, 'frozenAt': stamp(now),
        'minimumLeadHours': 24, 'maximumLeadDays': 7, 'maximumFixtures': 10,
        'trainingCutoffExclusive': cutoff.isoformat(), 'promotedToDefault': False,
        'settings': settings, 'settingsSha256': sha(settings), 'models': models,
        'modelSha256': {key: sha(model) for key, model in models.items()},
        'dataSha256': sha([(r.match.day.isoformat(), r.match.season, r.match.home, r.match.away,
                           r.match.home_goals, r.match.away_goals, r.home_xg, r.away_xg) for r in available]),
        'providerResponseSha256': sha(fixtures_provider_json), 'sourceHashes': source_hashes or {},
        'training': {'matches': len(available), 'latestTrainingMatch': matches[-1].day.isoformat(),
                     'xg': xg.xg_info, 'referencePoissonMatches': reference.fit_info['training_games']},
        'fixtures': eligible,
        'limitations': ['Small paired prospective experiment; no default-model promotion.',
            'All weights are frozen at registration and are not refitted for these fixtures.',
            'Historical xG coverage may be stale; missing xG uses verified actual goals.',
            'A changed kickoff or team identity excludes the complete pair.']}
    packet = seal_packet(packet)
    verify_packet(packet)
    return packet


def settle_packet(packet, current_fixtures, now):
    verify_packet(packet)
    now = instant(now)
    if now < instant(packet['frozenAt']):
        raise ValueError('Settlement cannot precede registration')
    current = {row['id']: row for row in fixture_rows(current_fixtures)}
    statuses, settled = [], []
    for fixture in packet['fixtures']:
        result = {'fixtureId': fixture['fixtureId'], 'home': fixture['home'], 'away': fixture['away'],
                  'utcDate': fixture['utcDate'], 'status': 'pending', 'reason': 'result_unavailable'}
        raw = current.get(fixture['fixtureId'])
        if raw is not None:
            try:
                identity = fixture_identity(raw, current_fixtures)
                changed = any(identity[key] != fixture[key] for key in identity)
            except (KeyError, TypeError, ValueError):
                changed = True
            if changed:
                result.update(status='excluded', reason='fixture_identity_changed')
            elif raw.get('status') in ('CANCELLED', 'CANCELED', 'POSTPONED', 'SUSPENDED', 'AWARDED'):
                result.update(status='excluded', reason='fixture_cancelled_or_postponed')
            elif raw.get('status') == 'FINISHED' and now >= instant(fixture['utcDate']):
                score = (raw.get('score') or {}).get('fullTime') or {}
                h, a = score.get('home'), score.get('away')
                if all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 30 for v in (h, a)):
                    actual = 0 if h > a else 1 if h == a else 2
                    result.update(status='settled', reason='verified_full_time', score=[h, a], actual=actual)
                    settled.append({'actual': actual, **fixture['predictions']})
                else:
                    result['reason'] = 'invalid_full_time_score'
        statuses.append(result)
    counts = {status: sum(row['status'] == status for row in statuses) for status in ('pending', 'settled', 'excluded')}
    return {'schemaVersion': 1, 'protocol': PROTOCOL, 'packetSha256': packet['packetSha256'],
            'frozenAt': packet['frozenAt'], 'evaluatedAt': stamp(now), 'promotedToDefault': False,
            'providerResponseSha256': sha(current_fixtures), 'registered': len(statuses), **counts,
            'commonMatches': len(settled), 'metrics': {key: summarize(settled, key) for key in MODEL_KEYS},
            'fixtures': statuses}


def report_markdown(report):
    lines = ['# Prospective xG paired experiment', '',
        f"Registered: {report['registered']}; settled common matches: {report['commonMatches']}; pending: {report['pending']}; excluded pairs: {report['excluded']}.",
        '', '| Model | Brier ↓ | Log loss ↓ | Correct outcome |', '| --- | ---: | ---: | ---: |']
    for name in MODEL_KEYS:
        m = report['metrics'][name]
        values = f"{m['brier']:.6f} | {m['logloss']:.6f} | {m['accuracy']:.2%}" if m['n'] else '— | — | —'
        lines.append(f'| {name} | {values} |')
    lines += ['', 'Both models use the identical settled cohort. Pending/excluded games count for neither model.',
              'These fixed weights remain experimental. No default-model promotion.',
              '', f"Frozen packet SHA-256: `{report['packetSha256']}`.", '']
    return '\n'.join(lines)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['settle'])
    parser.add_argument('--packet', required=True, type=Path)
    parser.add_argument('--fixtures', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = settle_packet(json.loads(args.packet.read_text()), json.loads(args.fixtures.read_text()), datetime.now(timezone.utc))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    (args.output / 'report.md').write_text(report_markdown(report))
