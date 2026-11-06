"""Freeze and settle independent pre-match total-corner probabilities."""
from __future__ import annotations

import argparse
import copy
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path

from research.corners import LINES, corner_count, distribution_prediction, load_corners
from research.nightly import sha
from research.xg_prospective import instant, stamp, identifier, fixture_rows, fixture_identity

PROTOCOL = 'corner-paired-1'
MODELS = ('candidate', 'baseline')


def predict_export(model, home, away):
    if model.get('target') != 'corners_total_regulation' or model.get('schemaVersion') != 1:
        raise ValueError('Expected a portable corner-count model')
    home, away = (model.get('aliases', {}).get(name, name) for name in (home, away))
    if home == away or not all((home, away)):
        raise ValueError('Invalid corner fixture teams')
    teams = model['teams']
    if home not in teams or away not in teams:
        raise ValueError('No fitted corner history for an eligible club')
    h, a = teams[home], teams[away]
    home_mean = math.exp(model['intercept'] + model['home_advantage'] + h['attack'] - a['defence'])
    away_mean = math.exp(model['intercept'] + a['attack'] - h['defence'])
    if model.get('side_mean_bounds') != [.1, 20.]:
        raise ValueError('Unregistered corner mean bounds')
    if not all(math.isfinite(v) for v in (home_mean, away_mean)):
        raise ValueError('Invalid corner means')
    return distribution_prediction(max(.1, min(20., home_mean)), max(.1, min(20., away_mean)),
                                   model['family'], model['alpha'])


def probabilities(prediction):
    values = {}
    for line in LINES:
        item = prediction['totals'][str(line)]
        p, q = item['over'], item['under']
        if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
               or not 0 <= v <= 1 for v in (p, q)) or abs(p + q - 1) > 1e-12:
            raise ValueError('Invalid paired corner probabilities')
        values[str(line)] = {'over': float(p), 'under': float(q)}
    if any(values[str(a)]['over'] < values[str(b)]['over'] for a, b in zip(LINES, LINES[1:])):
        raise ValueError('Corner threshold probabilities are not monotone')
    return values


def seal_packet(packet):
    result = copy.deepcopy(packet)
    result.pop('packetSha256', None)
    result['packetSha256'] = sha(result)
    return result


def verify_packet(packet):
    if packet.get('packetSha256') != seal_packet(packet)['packetSha256']:
        raise ValueError('Frozen corner packet digest mismatch')
    if packet.get('schemaVersion') != 1 or packet.get('protocol') != PROTOCOL:
        raise ValueError('Invalid corner prospective protocol')
    frozen = instant(packet['frozenAt'])
    cutoff = frozen.date() - timedelta(days=1)
    if packet.get('minimumLeadHours') != 24 or packet['trainingCutoffExclusive'] != cutoff.isoformat():
        raise ValueError('Invalid corner training or registration fence')
    model, baseline = packet['model'], packet['baseline']
    if packet['modelSha256'] != sha(model) or packet['baselineSha256'] != sha(baseline):
        raise ValueError('Frozen corner model or reference digest mismatch')
    for item in (model, baseline):
        if item['as_of'] != cutoff.isoformat() or date.fromisoformat(item['fit']['latest_training_date']) >= cutoff:
            raise ValueError('Corner inputs include excluded training dates')
    baseline_p = probabilities(baseline)
    seen, identities = set(), set()
    if not 1 <= len(packet['fixtures']) <= 10:
        raise ValueError('Invalid corner prospective fixture count')
    for fixture in packet['fixtures']:
        key = identifier(fixture['fixtureId'])
        if key in seen:
            raise ValueError('Duplicate frozen corner fixture')
        seen.add(key)
        identity = (fixture['utcDate'][:10], fixture['home'], fixture['away'])
        if identity in identities:
            raise ValueError('Duplicate frozen corner fixture identity')
        identities.add(identity)
        lead = instant(fixture['utcDate']) - frozen
        if not timedelta(hours=24) <= lead <= timedelta(days=7):
            raise ValueError('Insufficient corner registration lead')
        if fixture['predictionsSha256'] != sha(fixture['predictions']):
            raise ValueError('Frozen corner prediction digest mismatch')
        predicted = probabilities(predict_export(model, fixture['home'], fixture['away']))
        for name, expected in (('candidate', predicted), ('baseline', baseline_p)):
            actual = probabilities({'totals': fixture['predictions'][name]})
            if any(abs(actual[str(line)][side] - expected[str(line)][side]) > 1e-12
                   for line in LINES for side in ('over', 'under')):
                raise ValueError('Corner predictions disagree with frozen inputs')
    return True


def build_packet(model, baseline, provider_fixtures, now, source_hashes=None):
    now = instant(now)
    eligible = []
    for raw in fixture_rows(provider_fixtures):
        if raw.get('status') not in ('SCHEDULED', 'TIMED'):
            continue
        if timedelta(hours=24) <= instant(raw['utcDate']) - now <= timedelta(days=7):
            eligible.append(fixture_identity(raw, provider_fixtures))
    eligible.sort(key=lambda row: (row['utcDate'], row['fixtureId']))
    eligible = eligible[:10]
    if not eligible:
        raise ValueError('No eligible future corner fixture with 24 hours lead')
    for fixture in eligible:
        fixture['predictions'] = {'candidate': probabilities(predict_export(model, fixture['home'], fixture['away'])),
                                  'baseline': probabilities(baseline)}
        fixture['predictionsSha256'] = sha(fixture['predictions'])
    packet = {'schemaVersion': 1, 'protocol': PROTOCOL, 'frozenAt': stamp(now),
        'trainingCutoffExclusive': (now.date() - timedelta(days=1)).isoformat(),
        'minimumLeadHours': 24, 'maximumLeadDays': 7, 'maximumFixtures': 10,
        'lines': list(LINES), 'model': copy.deepcopy(model), 'baseline': copy.deepcopy(baseline),
        'modelSha256': sha(model), 'baselineSha256': sha(baseline),
        'providerResponseSha256': sha(provider_fixtures), 'sourceHashes': source_hashes or {},
        'fixtures': eligible, 'default1X2Changed': False,
        'limitations': ['Independent experimental corner target; not 1X2 accuracy.',
            'Both probabilities fixed before games; no refitting for this cohort.',
            'Missing corner results remain pending; changed fixtures exclude the pair.']}
    packet = seal_packet(packet)
    verify_packet(packet)
    return packet


def summarize(settled, name):
    result = {'n': len(settled), 'byLine': {}}
    for line in LINES:
        key = str(line)
        if not settled:
            result['byLine'][key] = {'n': 0, 'brier': None, 'logloss': None, 'accuracy': None}
            continue
        p = [r['predictions'][name][key]['over'] for r in settled]
        y = [int(r['totalCorners'] > line) for r in settled]
        result['byLine'][key] = {'n': len(y),
            'brier': sum((v - a) ** 2 for v, a in zip(p, y)) / len(y),
            'logloss': -sum(math.log(max(1e-12, min(1 - 1e-12, v if a else 1 - v))) for v, a in zip(p, y)) / len(y),
            'accuracy': sum((v >= .5) == bool(a) for v, a in zip(p, y)) / len(y)}
    for metric in ('brier', 'logloss', 'accuracy'):
        result[metric] = sum(value[metric] for value in result['byLine'].values()) / len(LINES) if settled else None
    return result


def settle_packet(packet, provider_fixtures, observations, now):
    verify_packet(packet)
    now = instant(now)
    if now < instant(packet['frozenAt']):
        raise ValueError('Corner settlement precedes registration')
    provider = {row['id']: row for row in fixture_rows(provider_fixtures)}
    corners = {}
    for row in observations:
        if row.match.key in corners:
            raise ValueError('Duplicate corner result fixture')
        corners[row.match.key] = row
    results, settled = [], []
    for fixture in packet['fixtures']:
        item = {key: fixture[key] for key in ('fixtureId', 'home', 'away', 'utcDate')}
        item.update(status='pending', reason='result_unavailable')
        raw = provider.get(fixture['fixtureId'])
        if raw is not None:
            try:
                identity = fixture_identity(raw, provider_fixtures)
                changed = any(identity[key] != fixture[key] for key in identity)
            except (KeyError, TypeError, ValueError, AttributeError):
                changed = True
            if changed:
                item.update(status='excluded', reason='fixture_identity_changed')
            elif raw.get('status') in ('POSTPONED', 'CANCELLED', 'SUSPENDED', 'AWARDED'):
                item.update(status='excluded', reason='fixture_cancelled_or_postponed')
            elif raw.get('status') == 'FINISHED' and now >= instant(fixture['utcDate']):
                row = corners.get((fixture['utcDate'][:10], fixture['home'], fixture['away']))
                score = raw.get('score')
                full = score.get('fullTime') if isinstance(score, dict) else None
                values = (full.get('home'), full.get('away')) if isinstance(full, dict) else (None, None)
                if not all(type(value) is int and 0 <= value <= 30 for value in values):
                    item['reason'] = 'invalid_provider_final_score'
                elif row is None:
                    item['reason'] = 'corner_results_unavailable'
                elif (row.match.home_goals, row.match.away_goals) != values:
                    item.update(status='excluded', reason='corner_source_score_disagreement')
                else:
                    try:
                        h, a = corner_count(row.home_corners), corner_count(row.away_corners)
                    except ValueError:
                        h = a = None
                    if h is None or a is None:
                        item['reason'] = 'corner_results_unavailable'
                    else:
                        item.update(status='settled', reason='verified_final_corner_counts',
                                    homeCorners=h, awayCorners=a, totalCorners=h + a,
                                    score={'home': values[0], 'away': values[1]})
                        settled.append({'totalCorners': h + a, 'predictions': fixture['predictions']})
        results.append(item)
    counts = {key: sum(r['status'] == key for r in results) for key in ('pending', 'settled', 'excluded')}
    return {'schemaVersion': 1, 'protocol': PROTOCOL, 'packetSha256': packet['packetSha256'],
        'frozenAt': packet['frozenAt'], 'evaluatedAt': stamp(now), 'registered': len(results),
        **counts, 'commonMatches': len(settled), 'providerResponseSha256': sha(provider_fixtures),
        'metrics': {name: summarize(settled, name) for name in MODELS},
        'fixtures': results, 'default1X2Changed': False}


def report_markdown(report):
    lines = ['# Проверка заранее сохранённых прогнозов угловых', '',
        f"Зафиксировано: {report['registered']}; завершено: {report['settled']}; ожидают угловых: {report['pending']}; исключено: {report['excluded']}.",
        '', '| Модель | Общих матчей | Brier ↓ | Log loss ↓ |', '| --- | ---: | ---: | ---: |']
    for name in MODELS:
        m = report['metrics'][name]
        values = f"{m['brier']:.6f} | {m['logloss']:.6f}" if m['n'] else '— | —'
        lines.append(f"| {name} | {m['n']} | {values} |")
    return '\n'.join(lines + ['', 'Средние показатели по трём тоталам; обе модели оцениваются на одинаковых матчах.',
        'Отсутствующие угловые не считаются нулевыми. История прогнозов 1X2 не изменяется.',
        '', f"SHA-256 исходного пакета: `{report['packetSha256']}`.", ''])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['settle'])
    for name in ('packet', 'fixtures', 'data-dir', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    observations, sources = load_corners(args.data_dir, [2026])
    result = settle_packet(json.loads(args.packet.read_text()), json.loads(args.fixtures.read_text()), observations,
                           datetime.now(timezone.utc))
    result['cornerResultSources'] = sources
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    (args.output / 'report.md').write_text(report_markdown(result))
