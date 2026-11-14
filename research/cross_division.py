"""One fixed cross-division history experiment on the existing ELC cohort."""
import argparse
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import time

import numpy as np

from research.championship import ROOT, dump
from research.championship_shots import PLAN as ELC_PLAN, load_inputs as load_elc
from research.data import Match
from research.division_model import DivisionGame, DivisionPoisson
from research.evaluate import elo_predictions, summarize, paired_bootstrap
from research.export_model import export_elo
from research.nightly import sha
from research.performance import Performance, parse_performance

PLAN = ROOT / 'docs/research/cross-division-plan-2026-11-14.json'
PROTOCOL = 'cross-division-research-1'


def normalized(games):
    return [[g.division, o.match.day.isoformat(), o.match.season, o.match.home, o.match.away,
             o.match.home_goals, o.match.away_goals, o.home_shots, o.away_shots, o.home_sot, o.away_sot, list(o.issues)]
            for g in sorted(games, key=lambda g: (*g.observation.match.key, g.division)) for o in [g.observation]]


def validate(games, plan):
    if (plan['protocol'] != PROTOCOL or plan['candidateCount'] != 1 or plan['automaticPromotion'] is not False
            or plan['existingFrozenPredictionsMayChange'] is not False or plan['divisions'] != ['E0', 'E1']):
        raise ValueError('Unexpected or unbounded division experiment')
    if {d: sum(g.division == d for g in games) for d in plan['divisions']} != plan['sourceMatches']:
        raise ValueError('The registered two-division corpus count changed')
    seen, memberships = set(), {}
    cutoff = date.fromisoformat(plan['asOf']) - timedelta(days=1)
    for g in games:
        o = g.observation; m = o.match
        if (g.division not in plan['divisions'] or m.key in seen or m.home == m.away or m.day >= cutoff
                or not date(m.season, 7, 1) <= m.day <= date(m.season+1, 7, 31)
                or any(type(v) is not int or not 0 <= v <= 30 for v in (m.home_goals, m.away_goals))):
            raise ValueError('Duplicate, invalid or future fixture in the division corpus')
        values = (o.home_shots, o.away_shots, o.home_sot, o.away_sot)
        if (any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 100) for v in values)
                or any(a is not None and b is not None and b > a for a, b in ((o.home_shots, o.home_sot), (o.away_shots, o.away_sot)))):
            raise ValueError('Invalid normalized shot statistics')
        seen.add(m.key)
        for team in (m.home, m.away):
            key = m.season, team
            if key in memberships and memberships[key] != g.division:
                raise ValueError('A club has conflicting divisions in the same season')
            memberships[key] = g.division
    elc = [g.observation for g in games if g.division == 'E1']
    from research.championship_shots import normalized as elc_normalized
    if sha(elc_normalized(elc)) != plan['sourceELCDataSha256']:
        raise ValueError('The original ELC shot data changed')


def load_sources(e0_directory, plan):
    source = ROOT / plan['sourceELCStudy']
    elc, elc_sources = load_elc(source/'shots-data.json.gz', json.loads(ELC_PLAN.read_text()))
    games = [DivisionGame('E1', o) for o in elc]
    sources = list(elc_sources)
    cutoff = date.fromisoformat(plan['asOf']) - timedelta(days=1)
    for year in range(2015, 2027):
        raw = (Path(e0_directory)/f'{year}-E0.csv').read_bytes()
        observations = parse_performance(raw, year, division='E0')
        if year < 2026 and len(observations) != 380:raise ValueError('Incomplete closed E0 season')
        games.extend(DivisionGame('E0', o) for o in observations if o.match.day < cutoff)
        sources.append({'division': 'E0', 'season': year, 'rows': len(observations),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'url': f'https://www.football-data.co.uk/mmz4281/{year%100:02d}{(year+1)%100:02d}/E0.csv'})
    games.sort(key=lambda g: (*g.observation.match.key, g.division))
    validate(games, plan)
    return games, sources


def load_snapshot(path, plan):
    path = Path(path); manifest = json.loads(path.with_name('input-manifest.json').read_text())
    compressed = path.read_bytes()
    if (len(compressed) > 3_000_000 or manifest.get('protocol') != PROTOCOL or manifest['planSha256'] != sha(plan)
            or hashlib.sha256(compressed).hexdigest() != manifest['compressedSha256']):
        raise ValueError('Joint snapshot identity or checksum mismatch')
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:raw = stream.read(8_000_001)
    if len(raw) > 8_000_000 or hashlib.sha256(raw).hexdigest() != manifest['uncompressedSha256']:
        raise ValueError('Joint normalized snapshot checksum mismatch')
    games = []
    for row in json.loads(raw):
        if not isinstance(row, list) or len(row) != 12:raise ValueError('Malformed joint snapshot row')
        o = Performance(Match(date.fromisoformat(row[1]), *row[2:7]), *row[7:11], issues=tuple(row[11]))
        games.append(DivisionGame(row[0], o))
    validate(games, plan)
    if sha(normalized(games)) != manifest['dataSha256']:raise ValueError('Joint data checksum mismatch')
    return games, manifest['sources']


def settings(plan):
    return {key: plan['candidate'][key] for key in ('half_life', 'ridge', 'window_days', 'goal_weight')}


def past_counts(games, cutoff, days):
    result = {}
    for g in games:
        m = g.observation.match
        if 0 < (cutoff-m.day).days <= days:
            for team in (m.home, m.away):
                by_division = result.setdefault(team, {'E0': 0, 'E1': 0})
                by_division[g.division] += 1
    return result


def forecast(games, rows, plan):
    model = DivisionPoisson(**settings(plan)); prior_cutoff = None
    e1 = [g.observation.match for g in games if g.division == 'E1']
    ratings = elo_predictions(e1, rows, plan['candidate']['elo'])
    result, fits, counts = [], [], {}
    for row, rating in zip(rows, ratings):
        cutoff = date.fromisoformat(row['cutoff_date'])
        if cutoff != date.fromisoformat(row['date'])-timedelta(days=1) or (prior_cutoff and cutoff < prior_cutoff):
            raise ValueError('Joint forecasts must follow chronological conservative cutoffs')
        if cutoff != prior_cutoff:
            model.fit(games, cutoff)
            if len(model.divisions) != 2 or not model.fit_info['bridgeTeams']:
                raise ValueError('No verified bridge connects both earlier division histories')
            consumed = [g for g in games if 0 < (cutoff-g.observation.match.day).days <= model.window_days]
            counts = past_counts(consumed, cutoff, model.window_days)
            fits.append({'cutoffExclusive': cutoff.isoformat(), **model.fit_info,
                         'divisionParameters': model.export()['divisions'], 'trainingSha256': sha(normalized(consumed))})
            prior_cutoff = cutoff
        predicted = model.predict('E1', row['home'], row['away'], date.fromisoformat(row['date']))
        weight = plan['candidate']['poisson_weight']
        probability = weight*predicted['probabilities']+(1-weight)*np.array(rating)
        histories = {team: counts[team] for team in (row['home'], row['away'])}
        low_history = any(v['E1'] < 20 and v['E0'] >= 20 for v in histories.values())
        result.append({**row, 'joint': probability.tolist(), 'pastClubGames': histories,
                       'lowELCHistoryWithPL': low_history,
                       'jointExpectedGoals': [predicted['home_goals'], predicted['away_goals']]})
    return result, fits


def code_sha():
    files = ('research/cross_division.py', 'research/division_model.py', 'research/models.py',
             'research/performance.py', 'research/data.py', 'research/evaluate.py', 'requirements-research.txt')
    return sha({name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files})


def execute(output, *, e0_directory=None, snapshot=None):
    plan = json.loads(PLAN.read_text()); source = ROOT/plan['sourceELCStudy']
    protected = (source.resolve(), (ROOT/json.loads(ELC_PLAN.read_text())['sourceStudy']).resolve())
    if Path(output).resolve() in protected:
        raise ValueError('Joint research cannot overwrite a registered source study')
    games, sources = load_snapshot(snapshot, plan) if snapshot else load_sources(e0_directory, plan)
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(normalized(games), ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    compressed = gzip.compress(raw, mtime=0); (output/'training-data.json.gz').write_bytes(compressed)
    manifest = {'schemaVersion': 1, 'protocol': PROTOCOL, 'planSha256': sha(plan),
        'dataSha256': sha(normalized(games)), 'compressedSha256': hashlib.sha256(compressed).hexdigest(),
        'uncompressedSha256': hashlib.sha256(raw).hexdigest(), 'sources': sources}
    dump(output/'input-manifest.json', manifest)
    results, predictions, fits = {}, {}, {}; started = time.monotonic()
    labels = {g.observation.match.key: g.observation.match.outcome for g in games if g.division == 'E1'}
    for name, checksum in plan['sourcePredictionSha256'].items():
        rows = json.loads((source/(name+'-predictions.json')).read_text())
        if sha(rows) != checksum or len(rows) != plan['expectedCommonMatches'][name]:
            raise ValueError('The registered E1 comparison cohort changed')
        if any(r['actual'] != labels[r['date'], r['home'], r['away']] for r in rows):
            raise ValueError('The original E1 outcome identity changed')
        predicted, diagnostics = forecast(games, rows, plan)
        subgroup = [r for r in predicted if r['lowELCHistoryWithPL']]
        keys = ('joint', 'shots', 'v1', 'base')
        results[name] = {'matches': len(predicted), 'metrics': {key: summarize(predicted, key) for key in keys},
            'pairedIntervals': {ref: paired_bootstrap(predicted, 'joint', ref, samples=plan['bootstrapSamples'])
                                for ref in ('shots', 'v1')},
            'lowELCHistoryWithPL': {'matches': len(subgroup), 'metrics': {key: summarize(subgroup, key) for key in keys}}}
        predictions[name], fits[name] = predicted, diagnostics
    point_gate = all(results[p]['metrics']['joint'][metric] < results[p]['metrics'][ref][metric]
                     for p in results for ref in ('shots', 'v1') for metric in plan['primaryMetrics'])
    historical_gate = point_gate and all(results['audit']['pairedIntervals'][ref][metric]['ci95'][1] < 0
                                        for ref in ('shots', 'v1') for metric in plan['primaryMetrics'])
    cutoff = date.fromisoformat(plan['asOf'])-timedelta(days=1)
    final = DivisionPoisson(**settings(plan)).fit(games, cutoff)
    e1 = [g.observation.match for g in games if g.division == 'E1']
    model = {'protocol': PROTOCOL, 'status': 'experimental', 'as_of': plan['asOf'],
        'target_division': 'E1', 'training_cutoff_exclusive': cutoff.isoformat(), 'joint': final.export(),
        'elo': export_elo(e1, cutoff, plan['candidate']['elo']), 'poisson_weight': plan['candidate']['poisson_weight'],
        'data_sha256': manifest['dataSha256'], 'plan_sha256': sha(plan),
        'historical_gate_passed': historical_gate, 'promoted_to_default': False}
    model['model_version'] = 'cross-division-experimental-'+plan['asOf']+'-'+sha(model)[:12]
    counts = past_counts(games, cutoff, final.window_days)
    current_clubs = sorted({t for m in e1 if m.season == 2026 for t in (m.home, m.away)})
    report = {'schemaVersion': 1, 'protocol': PROTOCOL, 'asOf': plan['asOf'],
        'completedAt': datetime.now(timezone.utc).isoformat(), 'planSha256': sha(plan), 'codeSha256': code_sha(),
        'dataSha256': manifest['dataSha256'], 'sourceMatches': plan['sourceMatches'], 'candidateCount': 1,
        'results': results, 'pointGatePassed': point_gate, 'historicalGatePassed': historical_gate,
        'currentELCClubHistory': {club: counts[club] for club in current_clubs},
        'fitCounts': {name: len(items) for name, items in fits.items()},
        'modelSha256': sha(model), 'modelVersion': model['model_version'], 'durationSeconds': time.monotonic()-started,
        'predictionSha256': {name: sha(rows) for name, rows in predictions.items()}, 'fitsSha256': sha(fits),
        'retrospective': True, 'promotedToDefault': False, 'prospectiveValidationRequired': True,
        'existingFrozenPredictionsChanged': False, 'limitations': plan['limitations']}
    dump(output/'report.json', report); dump(output/'model.json', model)
    for name, value in [('fits', fits), *[(name+'-predictions', rows) for name, rows in predictions.items()]]:
        (output/(name+'.json')).write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)+'\n')
    lines = ['# История клубов между АПЛ и Чемпионшипом', '',
        'Один фиксированный вариант с общими параметрами клубов и отдельными средними результативности, '
        'домашним преимуществом и голевым масштабом ударов для каждой лиги. Отдельный Elo E1 сохранён.', '',
        '| Период | Общих матчей | Модель | Brier ↓ | Log Loss ↓ | Совпал исход |',
        '| --- | ---: | --- | ---: | ---: | ---: |']
    for period, block in results.items():
        for name, metrics in block['metrics'].items():
            lines.append(f"| {period} | {metrics['n']} | {name} | {metrics['brier']:.6f} | {metrics['logloss']:.6f} | {metrics['accuracy']:.1%} |")
    lines += ['', f'Критерий средних ошибок: {point_gate}. Критерий исторического преимущества: {historical_gate}.', '',
        'joint — совместное обучение; shots — сохранённый кандидат только по истории E1; v1 и base — прежние контрольные формулы.', '',
        f"История источников: {sum(plan['sourceMatches'].values())} игр. Обучение и проверка после загрузки: {report['durationSeconds']:.2f} с.", '',
        'Это дальнейшая разработка на уже изученных исторических периодах. Исходные 100 и 36 будущих прогнозов '
        'не перезаписываются. Основная модель не меняется автоматически.', '', 'Ограничения:', '']
    lines.extend('- '+value for value in plan['limitations'])
    (output/'report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'sourceMatches': plan['sourceMatches'], 'pointGatePassed': point_gate,
        'historicalGatePassed': historical_gate, 'seconds': report['durationSeconds'],
        'bridgeTeams': final.fit_info['bridgeTeams'], 'results': {k: v['metrics'] for k, v in results.items()}}, ensure_ascii=False), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--e0-data-dir', type=Path); inputs.add_argument('--snapshot', type=Path)
    parser.add_argument('--output', type=Path, required=True); args = parser.parse_args()
    execute(args.output, e0_directory=args.e0_data_dir, snapshot=args.snapshot)
