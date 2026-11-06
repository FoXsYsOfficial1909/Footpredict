"""Bounded experiments triggered by model-relevant data changes, never promotion."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import platform
import time

import numpy as np
import scipy

from research.evaluate import reference_rows, elo_predictions, summarize, paired_bootstrap
from research.models import loss
from research.nightly import sha
from research.performance import load_performance
from research.prediction_cache import PredictionCache
from research.xg_data import load_xg_matches, merge_xg
from research.xg_model import ExpectedGoalsPoisson

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'docs/research/automatic-experiment-plan-2026-11-06.json'
PROTOCOL = 'automatic-experiments-1'
IMPLEMENTATION = ('research/auto_experiment.py', 'research/prediction_cache.py',
    'research/evaluate.py', 'research/models.py', 'research/xg_model.py',
    'research/performance.py', 'research/xg_data.py', 'research/data.py')


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def row_value(observation, *, xg=True):
    m = observation.match
    value = [m.day.isoformat(), m.season, m.home, m.away, m.home_goals, m.away_goals]
    return value + [observation.home_xg, observation.away_xg] if xg else value


def code_revision():
    return sha({'files': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                         for name in IMPLEMENTATION},
                'python': platform.python_version(), 'numpy': np.__version__, 'scipy': scipy.__version__})


def cached_predictions(arguments):
    observations, rows, settings, elo_settings, directory, implementation = arguments
    matches = [o.match for o in observations]
    ratings = elo_predictions(matches, rows, elo_settings)
    cache = PredictionCache(directory)
    result = []; current = None; model = None; poisson_hash = elo_hash = None
    for row, rating in zip(rows, ratings):
        cutoff = date.fromisoformat(row['cutoff_date'])
        if cutoff != current:
            prior = [o for o in observations if o.match.day < cutoff]
            window = [o for o in prior if (cutoff - o.match.day).days <= settings['windowDays']]
            poisson_hash = sha([row_value(o) for o in window])
            elo_hash = sha([row_value(o, xg=False) for o in prior])
            model = None; current = cutoff
        inputs = {'recipe': settings, 'elo': elo_settings, 'code': implementation,
            'cutoffExclusive': cutoff.isoformat(), 'poissonInputsSha256': poisson_hash,
            'eloInputsSha256': elo_hash, 'home': row['home'], 'away': row['away']}
        def compute():
            nonlocal model
            if model is None:
                # Cold fits make reuse independent of which earlier days hit cache.
                model = ExpectedGoalsPoisson(goal_weight=settings['goalWeight'],
                    half_life=settings['halfLife'], ridge=settings['ridge'], window_days=settings['windowDays'])
                model.fit(observations, cutoff)
                cache.stats['fits'] += 1
            p = model.predict(row['home'], row['away'], date.fromisoformat(row['date']))['probabilities']
            return (settings['poissonWeight'] * p + (1 - settings['poissonWeight']) * np.array(rating)).tolist()
        result.append(cache.get(inputs, compute))
    return {'predictions': result, 'cacheStats': cache.stats}


def metrics(rows, key):
    common = [row for row in rows if row['v1'] is not None]
    return summarize(common, key)


def source_freshness(observations, today):
    season = today.year if today.month >= 7 else today.year - 1
    current = [o for o in observations if o.match.season == season]
    covered = [o for o in current if o.home_xg is not None and o.away_xg is not None]
    latest = max((o.match.day.isoformat() for o in covered), default=None)
    return {'season': season, 'completedMatches': len(current), 'matchedXg': len(covered),
            'missingXg': len(current) - len(covered), 'latestXgDate': latest,
            'latestResultDate': max((o.match.day.isoformat() for o in current), default=None),
            'dataAgeDays': (today - date.fromisoformat(latest)).days if latest else None,
            'incomplete': len(covered) < len(current)}


def error_analysis(rows):
    values = [r for r in rows if r['v1'] is not None]
    wrong = [r for r in values if int(np.argmax(r['baseline'])) != r['actual']]
    matrix = [[0, 0, 0] for _ in range(3)]
    for row in values:
        matrix[int(np.argmax(row['baseline']))][row['actual']] += 1
    return {'commonMatches': len(values), 'incorrectTopOutcomes': len(wrong),
        'confusionMatrix': {'labels': ['home', 'draw', 'away'],
                            'rows': 'predicted', 'columns': 'actual', 'counts': matrix},
        'incorrectWithConfidenceAtLeast60Percent': sum(max(r['baseline']) >= .6 for r in wrong),
        'largestProbabilityErrors': [{'date': r['date'], 'home': r['home'], 'away': r['away'],
            'actual': r['actual'], 'probabilities': r['baseline'],
            'logloss': loss(r['baseline'], r['actual'])['logloss']}
            for r in sorted(wrong, key=lambda r: loss(r['baseline'], r['actual'])['logloss'], reverse=True)[:5]]}


def markdown(report):
    lines = ['# Автоматическая проверка кандидатов', '',
        'Статус: **' + report['status'] + '**. Проверено: ' + report['generatedAt'] + '.',
        'Последний расчёт: ' + report.get('evaluatedAt', 'не выполнен') + '.', '']
    if report['status'] == 'failed':
        return '\n'.join(lines + ['Расчёт не завершён; предыдущий успешный отчёт сохранён.', ''])
    if report['status'] == 'unchanged':
        lines += ['Данные, влияющие на модель, и рецепт не изменились. Повторное обучение пропущено.', '']
    freshness = report['sourceFreshness']
    lines += [f"xG: {freshness['matchedXg']}/{freshness['completedMatches']}; последние данные: {freshness['latestXgDate'] or 'неизвестно'}.", '',
        '| Отбор | Совпал исход | Log loss ↓ | Brier ↓ |', '| --- | ---: | ---: | ---: |']
    for item in [{'id': 'Текущий xG-кандидат', 'metrics': report['selection']['baseline']}, *report['selection']['validation']]:
        m = item['metrics']; lines.append(f"| {item['id']} | {m['accuracy']:.2%} ({m['n']}) | {m['logloss']:.6f} | {m['brier']:.6f} |")
    chosen = report['selection']['chosen']
    lines += ['', 'Выбран для дальнейшей проверки: ' + (chosen['id'] if chosen else 'текущий кандидат сохраняется') + '.',
        'Это повторяемое ретроспективное исследование. Устойчивый рост точности требует будущих матчей; основная модель автоматически не меняется.',
        '', f"Кэш: {report['cacheStats']['hits']} совпадений, {report['cacheStats']['misses']} новых прогнозов, {report['cacheStats']['fits']} подгонок модели.", '']
    for label, title in [('retrospectiveAudit', 'Другой исторический сезон'), ('recentMonitoring', 'Текущий сезон')]:
        block = report.get(label)
        if block:
            lines += [f"## {title}", '', f"Общих матчей: {block['commonMatches']}.", '', '| Модель | Точность | Log loss ↓ | Brier ↓ |', '| --- | ---: | ---: | ---: |']
            for name, m in block['metrics'].items():
                if m['n']:
                    lines.append(f"| {name} | {m['accuracy']:.2%} | {m['logloss']:.6f} | {m['brier']:.6f} |")
            lines += ['']
    return '\n'.join(lines)


def write_report(output, report):
    dump(Path(output) / 'report.json', report)
    dump(Path(output) / 'attempt.json', report)
    (Path(output) / 'report.md').write_text(markdown(report))


def execute(data_dir, xg_matches, xg_manifest, output, cache_dir, state_dir,
            plan_path=PLAN, as_of=None, workers=2):
    output, state_dir = Path(output), Path(state_dir)
    output.mkdir(parents=True, exist_ok=True); state_dir.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    try:
        today = date.fromisoformat(as_of) if isinstance(as_of, str) else as_of or datetime.now(timezone.utc).date()
        plan = json.loads(Path(plan_path).read_text())
        if plan['protocol'] != PROTOCOL or len(plan['models']) > 3:
            raise ValueError('Unregistered experiment protocol or unbounded candidate list')
        observations, csv_sources = load_performance(data_dir, plan['seasons'])
        if any(s['rows'] != 380 for s in csv_sources if s['season'] < max(plan['seasons'])):
            raise ValueError('A closed EPL season must contain 380 verified results')
        xg_source = json.loads(Path(xg_manifest).read_text())
        xg_digest = hashlib.sha256(Path(xg_matches).read_bytes()).hexdigest()
        if xg_source.get('normalizedMatchesSha256') != xg_digest:
            raise ValueError('xG manifest does not bind normalized data')
        observations, joined = merge_xg(observations, load_xg_matches(xg_matches))
        cutoff = today - timedelta(days=1)
        actual_freshness = source_freshness([o for o in observations if o.match.day <= today], today)
        observations = [o for o in observations if o.match.day < cutoff]
        data_sha = sha([row_value(o) for o in observations])
        plan_sha, code_sha = sha(plan), code_revision()
        fingerprint = sha({'data': data_sha, 'plan': plan_sha, 'implementation': code_sha})
        provenance = {'csv': csv_sources, 'xgNormalizedSha256': xg_digest, 'join': joined,
                      'xgSource': xg_source}
        collector = xg_source.get('sourceFreshness') or {}
        freshness = {**actual_freshness,
            'sourceStatus': collector.get('sourceStatus', 'unknown'),
            'sourceCheckedAt': collector.get('sourceCheckedAt'),
            'sourceDataStatus': collector.get('status', 'unknown'),
            'cohortCutoffExclusive': cutoff.isoformat(),
            'eligibleForEvaluation': source_freshness(observations, today)}
        previous_path = state_dir / 'last-success.json'
        previous = None
        if previous_path.exists():
            previous = json.loads(previous_path.read_text())
            if (previous.get('schemaVersion') != 1 or previous.get('reportSha256') != sha(previous.get('report'))
                or previous['report'].get('status') != 'success'
                or previous['inputFingerprint'] != previous['report'].get('inputFingerprint')):
                raise ValueError('Last successful experiment state checksum mismatch')
        if previous and previous['inputFingerprint'] == fingerprint:
            report = {**previous['report'], 'status': 'unchanged', 'generatedAt': generated,
                'sourceFreshness': freshness, 'checkedSources': provenance,
                'cacheStats': {'hits': 0, 'misses': 0, 'corrupt': 0, 'fits': 0},
                'durationSeconds': time.monotonic() - started}
            write_report(output, report)
            return report
        common_years = set(plan['selectionSeasons'] + plan['auditSeasons'] + plan['recentMonitoringSeasons'])
        rows = reference_rows([o.match for o in observations], common_years)
        validation = [r for r in rows if r['season'] in plan['selectionSeasons']]
        if sum(r['v1'] is not None for r in validation) != plan['expectedValidationCommon']:
            raise ValueError('Validation cohort count changed')
        total_cache = {'hits': 0, 'misses': 0, 'corrupt': 0, 'fits': 0}
        def add_stats(result):
            for key in total_cache:
                total_cache[key] += result['cacheStats'][key]
        baseline_result = cached_predictions((observations, rows, plan['baseline'], plan['elo'], cache_dir, code_sha))
        add_stats(baseline_result)
        rows = [{**r, 'baseline': p} for r, p in zip(rows, baseline_result['predictions'])]
        validation = [r for r in rows if r['season'] in plan['selectionSeasons']]
        baseline = metrics(validation, 'baseline')
        tasks = [(observations, validation, recipe, plan['elo'], cache_dir, code_sha) for recipe in plan['models']]
        if workers == 2 and len(tasks) > 1:
            with ProcessPoolExecutor(max_workers=2) as pool:
                predicted = list(pool.map(cached_predictions, tasks))
        else:
            predicted = [cached_predictions(task) for task in tasks]
        candidates = []
        for recipe, result in zip(plan['models'], predicted):
            add_stats(result)
            evaluated = [{**r, 'challenger': p} for r, p in zip(validation, result['predictions'])]
            candidates.append({'id': recipe['id'], 'settings': recipe, 'metrics': metrics(evaluated, 'challenger')})
        eligible = [c for c in candidates if c['metrics']['accuracy'] >= baseline['accuracy']
                    and c['metrics']['logloss'] < baseline['logloss'] - 1e-10]
        chosen = min(eligible, key=lambda c: c['metrics']['logloss']) if eligible else None
        selection = {'baseline': baseline, 'validation': candidates, 'chosen': chosen,
            'inputFingerprint': fingerprint, 'planSha256': plan_sha, 'codeSha256': code_sha,
            'auditComputedAfterSelection': True}
        dump(output / 'selection.json', selection)  # Freeze before challenger audit.
        report = {'schemaVersion': 1, 'protocol': PROTOCOL, 'status': 'success',
            'generatedAt': generated, 'evaluatedAt': generated, 'inputFingerprint': fingerprint,
            'dataSha256': data_sha, 'planSha256': plan_sha, 'codeSha256': code_sha,
            'evaluationCutoffExclusive': cutoff.isoformat(), 'selection': selection,
            'sourceFreshness': freshness, 'checkedSources': provenance,
            'promotedToDefault': False, 'prospectiveValidationRequired': True,
            'retrospectiveOnly': True, 'passesRetrospectiveGate': False}
        for label, years in [('retrospectiveAudit', plan['auditSeasons']), ('recentMonitoring', plan['recentMonitoringSeasons'])]:
            targets = [r for r in rows if r['season'] in years]
            common = [r for r in targets if r['v1'] is not None]
            if label == 'retrospectiveAudit' and len(common) != plan['expectedAuditCommon']:
                raise ValueError('Audit cohort count changed')
            if chosen and targets:
                result = cached_predictions((observations, targets, chosen['settings'], plan['elo'], cache_dir, code_sha))
                add_stats(result)
                targets = [{**r, 'challenger': p} for r, p in zip(targets, result['predictions'])]
                common = [r for r in targets if r['v1'] is not None]
            model_names = ['baseline', 'challenger'] if chosen else ['baseline']
            report[label] = {'commonMatches': len(common), 'targets': len(targets),
                'metrics': {name: summarize(common, name) for name in model_names},
                'errorAnalysis': error_analysis(common)}
            if chosen and common:
                report[label]['challengerMinusBaseline'] = paired_bootstrap(common, 'challenger', 'baseline')
            dump(output / (label + '-predictions.json'), targets)
        if chosen:
            a, b = (report['retrospectiveAudit']['metrics'][key] for key in ('challenger', 'baseline'))
            report['passesRetrospectiveGate'] = (a['accuracy'] >= b['accuracy'] and
                a['logloss'] < b['logloss'] - 1e-10 and a['brier'] < b['brier'] - 1e-10)
        report['cacheStats'] = total_cache
        report['durationSeconds'] = time.monotonic() - started
        write_report(output, report)
        dump(previous_path, {'schemaVersion': 1, 'inputFingerprint': fingerprint,
                             'reportSha256': sha(report), 'report': report})
        return report
    except Exception as error:
        report = {'schemaVersion': 1, 'protocol': PROTOCOL, 'status': 'failed',
            'generatedAt': generated, 'inputFingerprint': None, 'errorType': type(error).__name__,
            'message': 'The experiment did not complete; the last successful report is retained.',
            'promotedToDefault': False, 'prospectiveValidationRequired': True,
            'durationSeconds': time.monotonic() - started}
        write_report(output, report)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('data-dir', 'xg-matches', 'xg-manifest', 'output', 'cache-dir', 'state-dir'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--plan', default=PLAN, type=Path)
    parser.add_argument('--as-of', type=date.fromisoformat)
    parser.add_argument('--workers', type=int, choices=[1, 2], default=2)
    args = parser.parse_args()
    result = execute(args.data_dir, args.xg_matches, args.xg_manifest, args.output,
        args.cache_dir, args.state_dir, args.plan, args.as_of, args.workers)
    print(json.dumps({key: result[key] for key in ('status', 'inputFingerprint', 'cacheStats', 'durationSeconds')}))
