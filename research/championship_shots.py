"""One fixed, past-only shots-on-target experiment on the pinned E1 archive."""
from __future__ import annotations

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

from research.championship import (ROOT, NIGHTLY_PROTOCOL as GOAL_NIGHTLY_PROTOCOL,
                                  blend, common_targets, dump, load_history, load_snapshot, training_rows)
from research.data import Match
from research.evaluate import elo_predictions, glm_predictions, paired_bootstrap, reference_rows, summarize
from research.export_model import export_elo
from research.nightly import sha
from research.performance import Performance, ShotPoisson, parse_performance

PLAN = ROOT / 'docs/research/championship-shots-plan-2026-11-14.json'
PROTOCOL = 'championship-shots-1'
NIGHTLY_PROTOCOL = 'championship-shots-nightly-1'
STUDY = ROOT / 'docs/research/championship-shots-2026-11-14/report.json'
FIELDS = ['date', 'season', 'home', 'away', 'homeGoals', 'awayGoals',
          'homeShots', 'awayShots', 'homeShotsOnTarget', 'awayShotsOnTarget', 'issues']


def normalized(observations):
    return [[o.match.day.isoformat(), o.match.season, o.match.home, o.match.away,
             o.match.home_goals, o.match.away_goals, o.home_shots, o.away_shots,
             o.home_sot, o.away_sot, list(o.issues)]
            for o in sorted(observations, key=lambda o: o.match.key)]


def validate_plan(plan):
    selection = json.loads((ROOT / plan['sourceStudy'] / 'selection.json').read_text())
    recipe = plan['candidate']
    if (plan['protocol'] != PROTOCOL or plan['league'] != 'ELC' or plan['division'] != 'E1'
            or plan['candidateCount'] != 1 or recipe['goal_weight'] != .5
            or plan['automaticPromotion'] is not False or plan['originalHundredMayChange'] is not False
            or sha(selection) != plan['goalSelectionSha256']
            or {key: recipe[key] for key in ('glm', 'elo', 'glm_weight')}
            != {key: selection[key] for key in ('glm', 'elo', 'glm_weight')}):
        raise ValueError('Unexpected shot experiment or changed fixed recipe')


def validate_observations(observations, plan):
    as_of = date.fromisoformat(plan['asOf'])
    goals, _ = load_snapshot(ROOT / plan['sourceStudy'] / 'training-data.json.gz', as_of)
    if sha([(m.day.isoformat(), m.season, m.home, m.away, m.home_goals, m.away_goals)
            for m in goals]) != plan['sourceGoalDataSha256']:
        raise ValueError('The registered goal archive changed')
    actual = {o.match.key: o.match for o in observations}
    if len(actual) != len(observations) or actual != {m.key: m for m in goals}:
        raise ValueError('Shot statistics do not match all registered goal fixtures')


def load_csv(directory, plan):
    study = ROOT / plan['sourceStudy']
    source = json.loads((study / 'input-manifest.json').read_text())
    observations = []
    cutoff = date.fromisoformat(plan['asOf']) - timedelta(days=1)
    for item in source['dataSources']:
        year = item['season']
        raw = (Path(directory) / f'{year}-E1.csv').read_bytes()
        if item.get('division') != 'E1' or hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('The pinned E1 CSV bytes changed')
        observations.extend(o for o in parse_performance(raw, year, division='E1') if o.match.day < cutoff)
    observations.sort(key=lambda o: o.match.key)
    validate_observations(observations, plan)
    return observations, source['dataSources']


def coverage(observations):
    complete = sum(o.home_sot is not None and o.away_sot is not None for o in observations)
    return {'matches': len(observations), 'completeShotMatches': complete,
            'goalFallbackMatches': len(observations) - complete,
            'invalidStatisticsMatches': sum(bool(o.issues) for o in observations)}


def save_inputs(output, observations, sources, plan, *, purpose='historical', as_of=None):
    raw = json.dumps(normalized(observations), ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    compressed = gzip.compress(raw, mtime=0)
    (output / 'shots-data.json.gz').write_bytes(compressed)
    manifest = {'schemaVersion': 1, 'protocol': PROTOCOL, 'league': 'ELC', 'division': 'E1',
        'planSha256': sha(plan), 'purpose': purpose, 'asOf': as_of or plan['asOf'],
        'fields': FIELDS, 'coverage': coverage(observations),
        'dataSha256': sha(normalized(observations)),
        'compressedSha256': hashlib.sha256(compressed).hexdigest(),
        'uncompressedSha256': hashlib.sha256(raw).hexdigest(), 'dataSources': sources}
    dump(output / 'shots-manifest.json', manifest)
    return manifest


def load_inputs(path, plan):
    path = Path(path)
    manifest = json.loads(path.with_name('shots-manifest.json').read_text())
    compressed = path.read_bytes()
    if (len(compressed) > 2_000_000 or manifest.get('protocol') != PROTOCOL
            or manifest.get('purpose', 'historical') != 'historical'
            or manifest.get('planSha256') != sha(plan) or manifest.get('fields') != FIELDS
            or hashlib.sha256(compressed).hexdigest() != manifest.get('compressedSha256')):
        raise ValueError('Shot archive identity or compressed checksum mismatch')
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        raw = stream.read(6_000_001)
    if len(raw) > 6_000_000 or hashlib.sha256(raw).hexdigest() != manifest.get('uncompressedSha256'):
        raise ValueError('Shot archive normalized checksum mismatch')
    records = json.loads(raw)
    observations = []
    for row in records:
        if (not isinstance(row, list) or len(row) != len(FIELDS) or type(row[1]) is not int
                or any(not isinstance(name, str) or not name.strip() for name in row[2:4])
                or row[2] == row[3] or any(type(n) is not int or not 0 <= n <= 30 for n in row[4:6])
                or any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 100)
                       for v in row[6:10])
                or not isinstance(row[10], list) or any(not isinstance(v, str) for v in row[10])):
            raise ValueError('Invalid shot archive fixture or statistic')
        if any(row[a] is not None and row[b] is not None and row[b] > row[a] for a, b in ((6, 8), (7, 9))):
            raise ValueError('Shot archive contains inconsistent statistics')
        observations.append(Performance(Match(date.fromisoformat(row[0]), *row[1:6]),
                                        *row[6:10], issues=tuple(row[10])))
    validate_observations(observations, plan)
    if sha(normalized(observations)) != manifest.get('dataSha256') or coverage(observations) != manifest.get('coverage'):
        raise ValueError('Shot archive data checksum or coverage mismatch')
    return observations, manifest['dataSources']


def shot_predictions(observations, rows, recipe):
    model = ShotPoisson(goal_weight=recipe['goal_weight'], **recipe['glm'])
    probabilities, diagnostics, expected_goals = [], [], []
    previous = None
    for row in rows:
        cutoff = date.fromisoformat(row['cutoff_date'])
        if cutoff != date.fromisoformat(row['date']) - timedelta(days=1) or (previous is not None and cutoff < previous):
            raise ValueError('Shot forecasts require chronological conservative cutoffs')
        if cutoff != previous:
            model.fit(observations, cutoff)
            consumed = [o for o in observations if 0 < (cutoff - o.match.day).days <= recipe['glm']['window_days']]
            diagnostics.append({'cutoffExclusive': cutoff.isoformat(), **model.fit_info,
                'goalFallbackMatches': len(consumed) - model.fit_info['shot_games'],
                'goalsPerShotOnTarget': model.shot_scale, 'trainingSha256': sha(normalized(consumed))})
            previous = cutoff
        if row['home'] not in model.teams or row['away'] not in model.teams:
            raise ValueError('No neutral fallback for an unseen evaluation team')
        prediction = model.predict(row['home'], row['away'], date.fromisoformat(row['date']))
        probabilities.append(prediction['probabilities'].tolist())
        expected_goals.append([prediction['home_goals'], prediction['away_goals']])
    return probabilities, expected_goals, diagnostics


def evaluate(observations, plan):
    matches = [o.match for o in observations]
    recipe = plan['candidate']
    results, predictions, fits = {}, {}, {}
    for name, years in [('validation', plan['validationSeasons']), ('audit', plan['auditSeasons']),
                        ('monitoring', plan['monitoringSeasons'])]:
        rows, counts = common_targets(matches, years, plan['expectedCommonMatches'][name])
        if counts['commonMatches'] != plan['expectedCommonMatches'][name]:
            raise ValueError('The registered comparison cohort changed')
        ratings = elo_predictions(matches, rows, recipe['elo'])
        goals = glm_predictions((matches, rows, recipe['glm']))['predictions']
        rows = blend(rows, goals, ratings, recipe['glm_weight'], 'goals')
        shots, expected, diagnostics = shot_predictions(observations, rows, recipe)
        rows = blend(rows, shots, ratings, recipe['glm_weight'], 'shots')
        rows = [{**row, 'shotExpectedGoals': value} for row, value in zip(rows, expected)]
        results[name] = {'seasons': years, **counts,
            'metrics': {key: summarize(rows, key) for key in ('shots', 'goals', 'v1', 'base')},
            'pairedIntervals': {reference: paired_bootstrap(rows, 'shots', reference, samples=plan['bootstrapSamples'])
                                for reference in ('goals', 'v1')}}
        predictions[name], fits[name] = rows, diagnostics
    point_gate = all(results[period]['metrics']['shots'][metric] < results[period]['metrics'][reference][metric]
                     for period in results for reference in ('goals', 'v1') for metric in plan['primaryMetrics'])
    historical_gate = point_gate and all(results['audit']['pairedIntervals'][reference][metric]['ci95'][1] < 0
                                        for reference in ('goals', 'v1') for metric in plan['primaryMetrics'])
    return results, predictions, fits, point_gate, historical_gate


def final_model(observations, plan, historical_gate, *, as_of=None):
    as_of = as_of or date.fromisoformat(plan['asOf'])
    recipe = plan['candidate']; cutoff = as_of - timedelta(days=1)
    fitted = ShotPoisson(goal_weight=recipe['goal_weight'], **recipe['glm']).fit(observations, cutoff)
    matches = [o.match for o in observations]
    model = {'league': 'ELC', 'division': 'E1', 'status': 'experimental', 'protocol': PROTOCOL,
        'as_of': as_of.isoformat(), 'training_cutoff_exclusive': cutoff.isoformat(), 'method': 'shots_on_target',
        'glm_weight': recipe['glm_weight'], 'glm': fitted.export(), 'elo': export_elo(matches, cutoff, recipe['elo']),
        'team_identity': 'Football-Data.co.uk canonical names within E1 only',
        'plan_sha256': sha(plan), 'goal_selection_sha256': plan['goalSelectionSha256'],
        'features_used': ['past_results', 'past_shots_on_target', 'opponent_strength', 'home_advantage'],
        'is_xg': False, 'historical_gate_passed': historical_gate,
        'prospective_validation_required': True, 'promoted_to_default': False}
    model['model_version'] = 'ELC-shots-experimental-' + as_of.isoformat() + '-' + sha(model)[:12]
    return model


def code_sha():
    files = ('research/championship_shots.py', 'research/championship.py', 'research/performance.py',
             'research/models.py', 'research/evaluate.py', 'research/data.py', 'research/nightly.py',
             'research/export_model.py', 'requirements-research.txt')
    return sha({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files})


def markdown(report):
    c = report['coverage']
    lines = ['# Чемпионшип: прошлые удары в створ в обучении', '',
        f"Исходных матчей: {c['matches']}; полная статистика ударов в створ: {c['completeShotMatches']}; "
        f"возврат к реальному счёту из-за неполных данных: {c['goalFallbackMatches']}.", '',
        'Проверен один фиксированный вариант: половина фактических голов и половина прошлых ударов в створ, '
        'переведённых в голевой масштаб по более ранним играм этой лиги. '
        'Параметры затухания, регуляризации и смесь с Elo сохранены от предыдущего исследования. '
        'Это не измеренный xG. Статистика прогнозируемого матча не используется.', '',
        '| Период | Общих матчей | Модель | Brier ↓ | Log Loss ↓ | Совпал исход |',
        '| --- | ---: | --- | ---: | ---: | ---: |']
    for period, block in report['results'].items():
        for name, metrics in block['metrics'].items():
            lines.append(f"| {period} | {metrics['n']} | {name} | {metrics['brier']:.6f} | "
                         f"{metrics['logloss']:.6f} | {metrics['accuracy']:.1%} |")
    lines += ['', '`shots` — новый вариант; `goals` — тот же рецепт без ударов; `v1` — датовый '
        'пересчёт базовой модели; `base` — прошлые частоты исходов лиги.', '',
        f"Критерий средних ошибок: {report['pointGatePassed']}. "
        f"Критерий исторического преимущества с интервалами: {report['historicalGatePassed']}.", '',
        'Все периоды уже изучались. Это ретроспективная разработка; будущая точность не установлена. '
        'Основная модель, её ежедневный кандидат без ударов и исходная сотня прогнозов не заменяются.', '',
        f"Время расчёта после загрузки данных: {report['durationSeconds']:.2f} с.", '', 'Ограничения:', '']
    lines.extend('- ' + value for value in report['limitations'])
    return '\n'.join(lines) + '\n'


def execute(output, *, directory=None, snapshot=None):
    plan = json.loads(PLAN.read_text()); validate_plan(plan)
    output = Path(output)
    if output.resolve() == (ROOT / plan['sourceStudy']).resolve():
        raise ValueError('The shot experiment cannot overwrite the original goal study')
    observations, sources = load_inputs(snapshot, plan) if snapshot else load_csv(directory, plan)
    output.mkdir(parents=True, exist_ok=True)
    manifest = save_inputs(output, observations, sources, plan)
    dump(output / 'recipe.json', {'protocol': PROTOCOL, 'planSha256': sha(plan),
        'goalSelectionSha256': plan['goalSelectionSha256'], 'candidate': plan['candidate'], 'candidateCount': 1})
    started = time.monotonic()
    results, predictions, fits, point_gate, historical_gate = evaluate(observations, plan)
    model = final_model(observations, plan, historical_gate)
    report = {'schemaVersion': 1, 'protocol': PROTOCOL, 'league': 'ELC', 'division': 'E1',
        'asOf': plan['asOf'], 'completedAt': datetime.now(timezone.utc).isoformat(),
        'planSha256': sha(plan), 'codeSha256': code_sha(), 'dataSha256': manifest['dataSha256'],
        'goalSelectionSha256': plan['goalSelectionSha256'], 'candidateCount': 1, 'settings': plan['candidate'],
        'coverage': coverage(observations), 'results': results,
        'fitCounts': {name: len(values) for name, values in fits.items()}, 'modelSha256': sha(model),
        'modelVersion': model['model_version'], 'durationSeconds': time.monotonic() - started,
        'pointGatePassed': point_gate, 'historicalGatePassed': historical_gate,
        'predictionSha256': {name: sha(rows) for name, rows in predictions.items()}, 'fitsSha256': sha(fits),
        'retrospective': True, 'prospectiveValidationRequired': True, 'promotedToDefault': False,
        'originalHundredChanged': False, 'limitations': plan['limitations']}
    dump(output / 'report.json', report); dump(output / 'model.json', model); dump(output / 'fits.json', fits)
    (output / 'report.md').write_text(markdown(report))
    for name, rows in predictions.items():
        (output / (name + '-predictions.json')).write_text(json.dumps(rows, ensure_ascii=False, allow_nan=False) + '\n')
    print(json.dumps({'coverage': report['coverage'], 'modelVersion': report['modelVersion'],
        'pointGatePassed': point_gate, 'historicalGatePassed': historical_gate, 'seconds': report['durationSeconds'],
        'results': {name: block['metrics'] for name, block in results.items()}}, ensure_ascii=False), flush=True)
    return report


def load_current(directory, goal_report, as_of, plan):
    """Reuse the goal job's exact cached CSVs; never download another copy."""
    if (goal_report.get('status') != 'success' or goal_report.get('protocol') != GOAL_NIGHTLY_PROTOCOL
            or goal_report.get('method', 'goals') != 'goals' or goal_report.get('league') != 'ELC'
            or goal_report.get('division') != 'E1' or goal_report.get('asOf') != as_of.isoformat()
            or goal_report.get('selectionSha256') != plan['goalSelectionSha256']):
        raise ValueError('The current goal report does not verify the E1 source or recipe')
    matches, sources = load_history(directory, as_of, offline=True)
    if sha(training_rows(matches)) != goal_report['dataSha256'] or sources != goal_report['dataSources']:
        raise ValueError('Cached E1 results or statistics changed after the goal job')
    observations = []
    for source in sources:
        year = source['season']; raw = (Path(directory) / f'{year}-E1.csv').read_bytes()
        if hashlib.sha256(raw).hexdigest() != source['sha256']:
            raise ValueError('Cached shot source changed while reading')
        observations.extend(o for o in parse_performance(raw, year, division='E1')
                            if o.match.day < as_of - timedelta(days=1))
    observations.sort(key=lambda o: o.match.key)
    if len({o.match.key for o in observations}) != len(observations) or [o.match for o in observations] != matches:
        raise ValueError('Current shots do not match all verified goal results')
    return observations, sources


def refresh(directory, goal_report_path, output, as_of, *, study_path=STUDY):
    """Refresh this fixed research method; retain its actual historical qualification."""
    output = Path(output)
    if output.resolve() in (Path(study_path).resolve().parent, Path(goal_report_path).resolve().parent):
        raise ValueError('Shot refresh cannot overwrite its source study or goal output')
    output.mkdir(parents=True, exist_ok=True)
    for name in ('model.json', 'report.json', 'report.md', 'evaluation.json', 'fits.json',
                 'shots-data.json.gz', 'shots-manifest.json', 'attempt.json'):
        (output / name).unlink(missing_ok=True)
    generated = datetime.now(timezone.utc).isoformat(); started = time.monotonic()
    try:
        plan = json.loads(PLAN.read_text()); validate_plan(plan)
        study = json.loads(Path(study_path).read_text())
        if (study.get('protocol') != PROTOCOL or study.get('planSha256') != sha(plan)
                or study.get('settings') != plan['candidate'] or type(study.get('historicalGatePassed')) is not bool):
            raise ValueError('The original study does not verify this fixed shot recipe')
        goal_report = json.loads(Path(goal_report_path).read_text())
        observations, sources = load_current(directory, goal_report, as_of, plan)
        loaded = time.monotonic(); matches = [o.match for o in observations]
        period = goal_report['evaluation']; seasons = {m.season for m in matches
                                                      if period['from'] <= m.day.isoformat() <= period['to']}
        rows = [r for r in reference_rows(matches, seasons)
                if period['from'] <= r['date'] <= period['to'] and r['v1'] is not None]
        if len(rows) != period['commonMatches'] or len(rows) < 60:
            raise ValueError('Daily shots and goals must share at least 60 identical evaluation matches')
        recipe = plan['candidate']; ratings = elo_predictions(matches, rows, recipe['elo'])
        goals = glm_predictions((matches, rows, recipe['glm']))['predictions']
        rows = blend(rows, goals, ratings, recipe['glm_weight'], 'goals')
        shots, expected, fits = shot_predictions(observations, rows, recipe)
        rows = blend(rows, shots, ratings, recipe['glm_weight'], 'shots')
        rows = [{**row, 'shotExpectedGoals': value} for row, value in zip(rows, expected)]
        validated = time.monotonic()
        model = final_model(observations, plan, study['historicalGatePassed'], as_of=as_of)
        model.pop('model_version'); model['protocol'] = NIGHTLY_PROTOCOL
        model['model_version'] = 'ELC-shots-candidate-' + as_of.isoformat() + '-' + sha(model)[:12]
        fitted = time.monotonic()
        manifest = save_inputs(output, observations, sources, plan, purpose='nightly', as_of=as_of.isoformat())
        report = {'schemaVersion': 1, 'protocol': NIGHTLY_PROTOCOL, 'status': 'success',
            'league': 'ELC', 'division': 'E1', 'method': 'shots_on_target', 'asOf': as_of.isoformat(),
            'generatedAt': generated, 'trainingCutoffExclusive': model['training_cutoff_exclusive'],
            'latestTrainingMatch': max(m.day for m in matches).isoformat(),
            'trainingMatches': len(matches), 'poissonTrainingMatches': model['glm']['fit']['training_games'],
            'modelVersion': model['model_version'], 'modelSha256': sha(model),
            'dataSha256': manifest['dataSha256'], 'goalDataSha256': goal_report['dataSha256'],
            'settingsSha256': sha(recipe), 'planSha256': sha(plan), 'codeSha256': code_sha(),
            'evaluation': {'kind': 'rolling_historical_procedure', 'commonMatches': len(rows),
                'from': rows[0]['date'], 'to': rows[-1]['date'],
                'metrics': {key: summarize(rows, key) for key in ('shots', 'goals', 'v1', 'base')}},
            'coverage': coverage(observations), 'predictionSha256': sha(rows),
            'historicalGatePassed': study['historicalGatePassed'], 'durationSeconds': time.monotonic() - started,
            'timings': {'dataLoadSeconds': loaded - started, 'historicalValidationSeconds': validated - loaded,
                        'finalFitSeconds': fitted - validated},
            'additionalProviderRequests': 0, 'prospectiveValidationRequired': True, 'promotedToDefault': False}
        dump(output / 'model.json', model); dump(output / 'report.json', report); dump(output / 'fits.json', fits)
        (output / 'evaluation.json').write_text(json.dumps(rows, ensure_ascii=False, allow_nan=False) + '\n')
        lines = ['# Чемпионшип: ночной кандидат с прошлыми ударами', '',
            f"Срез: {as_of.isoformat()}; исторических общих матчей: {len(rows)}.", '',
            '| Модель | Brier ↓ | Log Loss ↓ | Совпал исход |', '| --- | ---: | ---: | ---: |']
        for name, metrics in report['evaluation']['metrics'].items():
            lines.append(f"| {name} | {metrics['brier']:.6f} | {metrics['logloss']:.6f} | {metrics['accuracy']:.1%} |")
        lines += ['', f"Критерий исходного исследования: {study['historicalGatePassed']}. "
            'Это исторический мониторинг; будущая точность новых весов не доказана. Основная модель не меняется.', '',
            'Использованы те же проверенные E1 CSV, что и у кандидата без ударов. Дополнительных запросов к поставщику: 0.']
        (output / 'report.md').write_text('\n'.join(lines) + '\n')
        attempt = report
        print(json.dumps({'modelVersion': model['model_version'], 'trainingMatches': len(matches),
            'commonMatches': len(rows), 'additionalProviderRequests': 0, 'seconds': report['durationSeconds']}), flush=True)
    except Exception as error:
        attempt = {'schemaVersion': 1, 'protocol': NIGHTLY_PROTOCOL, 'status': 'failed',
            'league': 'ELC', 'division': 'E1', 'method': 'shots_on_target', 'asOf': as_of.isoformat(),
            'generatedAt': generated, 'errorType': type(error).__name__,
            'message': 'The shot candidate was not completed.', 'promotedToDefault': False}
        raise
    finally:
        dump(output / 'attempt.json', attempt)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--goal-report', type=Path)
    parser.add_argument('--as-of', type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument('--data-dir', type=Path)
    inputs.add_argument('--snapshot', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.refresh:
        if not args.data_dir or not args.goal_report or args.snapshot:
            parser.error('--refresh requires --data-dir and --goal-report, and does not accept --snapshot')
        refresh(args.data_dir, args.goal_report, args.output, args.as_of)
    else:
        if not (args.data_dir or args.snapshot) or args.goal_report:
            parser.error('The fixed experiment requires --data-dir or --snapshot without --goal-report')
        execute(args.output, directory=args.data_dir, snapshot=args.snapshot)
