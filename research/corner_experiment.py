"""Registered chronological corner-total experiment and portable candidate export."""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np

from research.corners import CornerModel, empiricalLeague, load_corners
from research.nightly import sha

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'docs/research/corner-plan-2026-11-06.json'
PROTOCOL = 'corner-research-1'
IMPLEMENTATION = ('research/corner_experiment.py', 'research/corners.py',
                  'research/models.py', 'research/data.py', 'requirements-research.txt')


def dump(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def target_rows(observations, seasons):
    return [{'date': row.match.day.isoformat(),
             'cutoffExclusive': (row.match.day - timedelta(days=1)).isoformat(),
             'season': row.match.season, 'home': row.match.home, 'away': row.match.away,
             'homeCorners': row.home_corners, 'awayCorners': row.away_corners,
             'actualTotal': row.total}
            for row in observations if row.match.season in seasons and row.total is not None]


def event_scores(prediction, actual, lines):
    if not isinstance(actual, int) or isinstance(actual, bool) or actual < 0:
        raise ValueError('A verified non-negative integer corner target is required')
    result = {}
    for line in lines:
        item = prediction['totals'][str(line)]
        p = item['over']; under = item['under']
        if (not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v) and 0 <= v <= 1 for v in (p, under))
            or abs(p + under - 1) > 1e-10):
            raise ValueError('Invalid corner event probabilities')
        outcome = int(actual > line)
        probability = p if outcome else under
        result[str(line)] = {'brier': (p - outcome) ** 2,
                             'logloss': -math.log(max(1e-12, probability)),
                             'correct': int((p >= .5) == bool(outcome))}
    return result


def summarize(rows, key, lines):
    if not rows:
        return {'n': 0, 'brier': None, 'logloss': None, 'accuracy': None,
                'meanAbsoluteError': None, 'byLine': {}}
    values = [event_scores(row[key], row['actualTotal'], lines) for row in rows]
    by_line = {str(line): {'n': len(rows), **{
        metric: float(np.mean([scores[str(line)][metric] for scores in values]))
        for metric in ('brier', 'logloss', 'correct')}} for line in lines}
    for scores in by_line.values():
        scores['accuracy'] = scores.pop('correct')
    return {'n': len(rows),
            **{metric: float(np.mean([scores[metric] for scores in by_line.values()]))
               for metric in ('brier', 'logloss', 'accuracy')},
            'meanAbsoluteError': float(np.mean([
                abs(row[key]['expectedCorners']['total'] - row['actualTotal']) for row in rows])),
            'predictiveIntervalCoverage': float(np.mean([
                row[key]['range80']['lower'] <= row['actualTotal'] <= row[key]['range80']['upper']
                for row in rows])),
            'meanPredictiveIntervalWidth': float(np.mean([
                row[key]['range80']['upper'] - row[key]['range80']['lower'] for row in rows])),
            'byLine': by_line}


def paired_bootstrap(rows, lines, samples=1500):
    groups = defaultdict(list)
    for row in rows:
        a = event_scores(row['challenger'], row['actualTotal'], lines)
        b = event_scores(row['baseline'], row['actualTotal'], lines)
        groups[row['date']].append({metric: float(np.mean([
            a[str(line)][metric] - b[str(line)][metric] for line in lines]))
            for metric in ('brier', 'logloss')})
    if not groups:
        raise ValueError('No paired corner observations')
    days = sorted(groups); rng = np.random.default_rng(20261106)
    indices = rng.integers(0, len(days), size=(samples, len(days)))
    counts = np.array([len(groups[day]) for day in days])
    result = {}
    for metric in ('brier', 'logloss'):
        sums = np.array([sum(value[metric] for value in groups[day]) for day in days])
        bootstrap = sums[indices].sum(axis=1) / counts[indices].sum(axis=1)
        result[metric] = {'difference': float(sums.sum() / counts.sum()),
            'ci95': np.quantile(bootstrap, [.025, .975]).tolist(),
            'method': 'paired resampling of calendar-day clusters; mean over three event lines',
            'samples': samples}
    return result


def predict(arguments):
    observations, rows, settings = arguments
    model = CornerModel(half_life=settings['halfLife'], ridge=settings['ridge'],
                        window_days=settings['windowDays'], family=settings['family'])
    result, previous = [], None
    for row in rows:
        cutoff = date.fromisoformat(row['cutoffExclusive'])
        if cutoff != previous:
            model.fit(observations, cutoff)
            previous = cutoff
        result.append({**row, 'challenger': model.predict(row['home'], row['away'],
                                                        date.fromisoformat(row['date']))})
    return result


def baseline_predictions(observations, rows, settings):
    result, prior, previous = [], None, None
    for row in rows:
        cutoff = date.fromisoformat(row['cutoffExclusive'])
        if cutoff != previous:
            prior = empiricalLeague(observations, cutoff, half_life=settings['halfLife'],
                                    window_days=settings['windowDays'])
            previous = cutoff
        result.append({**row, 'baseline': prior})
    return result


def markdown(report):
    lines = ['# Эксперимент: угловые', '',
        'Это отдельный прогноз количества угловых. Модель исхода матча не изменяется.',
        '', '| Отбор | Средний Log loss ↓ | Средний Brier ↓ | MAE количества ↓ |',
        '| --- | ---: | ---: | ---: |']
    for name, scores in [('Частоты лиги', report['selection']['baseline']),
                         *[(r['id'], r['metrics']) for r in report['selection']['validation']]]:
        lines.append(f"| {name} | {scores['logloss']:.6f} | {scores['brier']:.6f} | {scores['meanAbsoluteError']:.3f} |")
    lines += ['', 'Выбран: ' + report['selection']['chosen']['id'] + '.',
              'Средние ошибки считаются по вероятностям трёх событий: угловых больше 8,5 / 9,5 / 10,5.', '']
    for key, title in [('audit', 'Другой исторический сезон'), ('recentMonitoring', 'Текущий сезон')]:
        block = report.get(key)
        if block:
            lines += [f'## {title}', '', f"Матчей: {block['targets']}.", '',
                '| Модель | Log loss ↓ | Brier ↓ | MAE ↓ |', '| --- | ---: | ---: | ---: |']
            for name, scores in block['metrics'].items():
                if scores['n']:
                    lines.append(f"| {name} | {scores['logloss']:.6f} | {scores['brier']:.6f} | {scores['meanAbsoluteError']:.3f} |")
            lines.append('')
    lines += ['Все эти периоды — ретроспективная разработка, а не доказательство будущей точности.',
              'Погода, составы и непроверенная нагрузка не входят в вероятности угловых.', '']
    return '\n'.join(lines)


def execute(data_dir, output, as_of=None, plan_path=PLAN, phase='all', workers=2):
    started = time.monotonic()
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    as_of = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of or datetime.now(timezone.utc).date()
    cutoff = as_of - timedelta(days=1)
    plan = json.loads(Path(plan_path).read_text())
    if plan['protocol'] != PROTOCOL or not 1 <= len(plan['models']) <= 3:
        raise ValueError('Unregistered corner experiment protocol')
    lines = plan['lines']
    if lines != [8.5, 9.5, 10.5]:
        raise ValueError('Unregistered corner event lines')
    observations, sources = load_corners(data_dir, plan['seasons'])
    if any(source['rows'] != 380 for source in sources if source['season'] < max(plan['seasons'])):
        raise ValueError('A closed EPL season must contain 380 verified matches')
    observations = [row for row in observations if row.match.day < cutoff]
    years = set(plan['selectionSeasons'] + plan['auditSeasons'] + plan['recentMonitoringSeasons'])
    rows = target_rows(observations, years)
    validation = [row for row in rows if row['season'] in plan['selectionSeasons']]
    if len(validation) != plan['expectedValidationTargets']:
        raise ValueError('Corner validation cohort count changed or corner targets are missing')
    implementation = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                      for name in IMPLEMENTATION}
    frozen = {'planSha256': sha(plan), 'dataSha256': sha([
        [row.match.day.isoformat(), row.match.season, row.match.home, row.match.away,
         row.home_corners, row.away_corners] for row in observations]),
        'implementationSha256': sha(implementation)}
    selection_path = output / 'selection.json'
    if phase == 'audit':
        previous = json.loads(selection_path.read_text())
        if any(previous.get(key) != value for key, value in frozen.items()):
            raise ValueError('Plan, corner data or implementation changed after selection')
    dump(output / 'protocol.json', plan); dump(output / 'data-manifest.json', sources)
    dump(output / 'implementation.json', implementation)
    validation = baseline_predictions(observations, validation, plan['baseline'])
    baseline = summarize(validation, 'baseline', lines)
    if phase in ('all', 'selection'):
        tasks = [(observations, validation, settings) for settings in plan['models']]
        if workers == 2:
            with ProcessPoolExecutor(max_workers=2) as pool:
                predictions = list(pool.map(predict, tasks))
        else:
            predictions = [predict(task) for task in tasks]
        results = [{'id': settings['id'], 'settings': settings,
                    'metrics': summarize(predicted, 'challenger', lines)}
                   for settings, predicted in zip(plan['models'], predictions)]
        chosen = min(results, key=lambda item: item['metrics']['logloss'])
        selection = {'protocol': PROTOCOL, **frozen, 'baseline': baseline,
            'validation': results, 'chosen': chosen, 'auditDeferred': True,
            'retrospectiveOnly': True, 'promotedToDefault': False}
        dump(selection_path, selection)
        for result in results:
            print('VALIDATION', json.dumps(result), flush=True)
        print('SELECTION_FROZEN', chosen['id'], flush=True)
    if phase in ('all', 'audit'):
        selection = json.loads(selection_path.read_text())
        if any(selection.get(key) != value for key, value in frozen.items()):
            raise ValueError('Frozen corner experiment changed')
        report = {'schemaVersion': 1, 'protocol': PROTOCOL, 'status': 'success',
            'generatedAt': datetime.now(timezone.utc).isoformat(), 'asOf': as_of.isoformat(),
            'selection': selection, 'sourceHashes': sources, 'codeSha256': sha(implementation),
            'settingsSha256': sha(selection['chosen']['settings']), 'dataSha256': frozen['dataSha256'],
            'retrospectiveOnly': True, 'prospectiveValidationRequired': True,
            'promotedToDefault': False, 'market': 'TOTAL_CORNERS_REGULATION',
            'lines': lines, 'weatherUsed': False, 'lineupsUsed': False, 'workloadUsed': False}
        for label, years in [('audit', plan['auditSeasons']), ('recentMonitoring', plan['recentMonitoringSeasons'])]:
            targets = [row for row in rows if row['season'] in years]
            if label == 'audit' and len(targets) != plan['expectedAuditTargets']:
                raise ValueError('Corner audit cohort count changed or corner targets are missing')
            targets = baseline_predictions(observations, targets, plan['baseline'])
            result = predict((observations, targets, selection['chosen']['settings']))
            report[label] = {'targets': len(result), 'metrics': {
                key: summarize(result, key, lines) for key in ('challenger', 'baseline')}}
            if result:
                report[label]['challengerMinusBaseline'] = paired_bootstrap(result, lines)
            dump(output / (label + '-predictions.json'), result)
            print(label.upper(), json.dumps(report[label]), flush=True)
        a, b = (report['audit']['metrics'][key] for key in ('challenger', 'baseline'))
        report['passesRetrospectiveGate'] = a['brier'] < b['brier'] and a['logloss'] < b['logloss']
        settings = selection['chosen']['settings']
        final = CornerModel(half_life=settings['halfLife'], ridge=settings['ridge'],
                            window_days=settings['windowDays'], family=settings['family']).fit(observations, cutoff)
        model = final.export()
        model.update(protocol=PROTOCOL, status='experimental',
                     training_cutoff_exclusive=cutoff.isoformat(), recipe=settings,
                     promoted_to_default=False, prospective_validation_required=True)
        model['model_version'] = 'corners-candidate-' + as_of.isoformat() + '-' + sha(model)[:12]
        report.update(modelVersion=model['model_version'], modelSha256=sha(model),
            trainingCutoffExclusive=cutoff.isoformat(), latestTrainingMatch=final.fit_info['latest_training_date'],
            trainingMatches=final.fit_info['training_games'], durationSeconds=time.monotonic() - started)
        dump(output / 'model.json', model); dump(output / 'report.json', report)
        (output / 'report.md').write_text(markdown(report))
        records = [{'date': o.match.day.isoformat(), 'season': o.match.season,
                    'home': o.match.home, 'away': o.match.away,
                    'homeCorners': o.home_corners, 'awayCorners': o.away_corners} for o in observations]
        (output / 'training-inputs.json.gz').write_bytes(gzip.compress(
            json.dumps(records, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))
        return report
    return json.loads(selection_path.read_text())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--plan', default=PLAN, type=Path)
    parser.add_argument('--phase', choices=['all', 'selection', 'audit'], default='all')
    parser.add_argument('--workers', choices=[1, 2], default=2, type=int)
    args = parser.parse_args()
    execute(args.data_dir, args.output, args.as_of, args.plan, args.phase, args.workers)
