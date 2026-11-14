"""Select Championship settings on earlier seasons and refresh a separate candidate."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import time

import numpy as np

from research.data import DIVISIONS, Match, download_season
from research.evaluate import elo_predictions, glm_predictions, paired_bootstrap, reference_rows, summarize
from research.models import loss
from research.nightly import build_candidate, sha

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'docs/research/championship-plan-2026-11-14.json'
SELECTION = ROOT / 'docs/research/championship-2026-11-14/selection.json'
PROTOCOL = 'championship-selection-1'
NIGHTLY_PROTOCOL = 'championship-nightly-1'


def dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def training_rows(matches):
    return [(m.day.isoformat(), m.season, m.home, m.away, m.home_goals, m.away_goals)
            for m in sorted(matches, key=lambda m: m.key)]


def code_sha():
    files = ('research/championship.py', 'research/data.py', 'research/models.py',
             'research/evaluate.py', 'research/nightly.py', 'research/export_model.py',
             'requirements-research.txt')
    return sha({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in files})


def validate_plan(plan):
    count = len(plan['glmGrid']) * len(plan['glmWeights'])
    if (plan['protocol'] != PROTOCOL or plan['league'] != 'ELC' or plan['division'] != 'E1'
            or plan['closedSeasonMatches'] != DIVISIONS['E1']['closed_season_matches']
            or count != plan['maximumCandidates'] or not 1 <= count <= 18
            or plan['automaticPromotion'] is not False or plan['originalHundredMayChange'] is not False
            or not max(plan['selectionSeasons']) < min(plan['auditSeasons'])
            or not max(plan['auditSeasons']) < min(plan['monitoringSeasons'])):
        raise ValueError('Unexpected Championship protocol or unbounded search')
    if any(weight <= 0 or weight > 1 for weight in plan['glmWeights']):
        raise ValueError('Invalid ensemble weight')


def load_history(directory, as_of, *, offline=False):
    season = as_of.year if as_of.month >= 7 else as_of.year - 1
    matches, sources = [], []
    for year in range(2015, season + 1):
        if offline and not (Path(directory) / f'{year}-E1.csv').exists():
            raise ValueError('Offline Championship history is incomplete')
        rows, source = download_season(year, directory, refresh=year == season and not offline, division='E1')
        if year < season and len(rows) != DIVISIONS['E1']['closed_season_matches']:
            raise ValueError('A closed Championship season must contain 552 regular-season matches')
        matches.extend(rows)
        sources.append(source)
    cutoff = as_of - timedelta(days=1)
    available = sorted((m for m in matches if m.day < cutoff), key=lambda m: m.key)
    if len({m.key for m in available}) != len(available):
        raise ValueError('Duplicate Championship fixture across seasons')
    return available, sources


def load_snapshot(path, as_of):
    """Reproduce a study from its pinned normalized archive without HTTP."""
    path = Path(path)
    manifest = json.loads(path.with_name('input-manifest.json').read_text())
    body = path.read_bytes()
    if (len(body) > 2_000_000 or manifest.get('league') != 'ELC' or manifest.get('division') != 'E1'
            or hashlib.sha256(body).hexdigest() != manifest.get('compressedSha256')):
        raise ValueError('Championship snapshot checksum or league mismatch')
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
        raw = stream.read(6_000_001)
    if len(raw) > 6_000_000 or hashlib.sha256(raw).hexdigest() != manifest.get('uncompressedSha256'):
        raise ValueError('Championship normalized snapshot checksum mismatch')
    records = json.loads(raw)
    if not isinstance(records, list) or len(records) != manifest.get('sourceMatches'):
        raise ValueError('Championship snapshot row count mismatch')
    matches = []
    for row in records:
        if (not isinstance(row, list) or len(row) != 6 or type(row[1]) is not int
                or any(not isinstance(team, str) or not team.strip() for team in row[2:4])
                or row[2] == row[3] or any(type(value) is not int or not 0 <= value <= 30 for value in row[4:])):
            raise ValueError('Invalid Championship snapshot fixture')
        day = date.fromisoformat(row[0])
        if not date(row[1], 7, 1) <= day <= date(row[1] + 1, 7, 31) or day >= as_of - timedelta(days=1):
            raise ValueError('Championship snapshot crossed its information fence')
        matches.append(Match(day, *row[1:]))
    if len({m.key for m in matches}) != len(matches) or sha(training_rows(matches)) != manifest.get('dataSha256'):
        raise ValueError('Championship snapshot identity or data checksum mismatch')
    return sorted(matches, key=lambda m: m.key), manifest['dataSources']


def common_targets(matches, seasons, minimum):
    targets = reference_rows(matches, set(seasons))
    rows = [row for row in targets if row['v1'] is not None]
    if len(rows) < minimum:
        raise ValueError('Insufficient common Championship evaluation fixtures')
    return rows, {'targets': len(targets), 'commonMatches': len(rows), 'excludedFromAll': len(targets) - len(rows)}


def blend(rows, poisson, ratings, weight, name='candidate'):
    if not len(rows) == len(poisson) == len(ratings):
        raise ValueError('Mismatched Championship prediction counts')
    return [{**row, name: (weight * np.array(p) + (1 - weight) * np.array(e)).tolist()}
            for row, p, e in zip(rows, poisson, ratings)]


def select_settings(matches, plan):
    validate_plan(plan)
    rows, coverage = common_targets(matches, plan['selectionSeasons'], plan['minimumCommonMatches']['selection'])
    ratings = elo_predictions(matches, rows, plan['elo'])
    trials = []
    for parameters in plan['glmGrid']:
        poisson = glm_predictions((matches, rows, parameters))['predictions']
        for weight in plan['glmWeights']:
            candidate = blend(rows, poisson, ratings, weight)
            trials.append({'id': f"glm-{parameters['half_life']}-r{parameters['ridge']}-w{weight}",
                           'glm': parameters, 'glm_weight': weight, 'metrics': summarize(candidate, 'candidate')})
    best = min(enumerate(trials), key=lambda item: (item[1]['metrics']['logloss'], item[1]['metrics']['brier'], item[0]))[1]
    # Bind only data actually available for fitting and labels used for selection.
    # Audit/monitoring outcomes cannot even alter the selection fingerprint.
    latest_cutoff = max(date.fromisoformat(row['cutoff_date']) for row in rows)
    target_keys = {(row['date'], row['home'], row['away']) for row in rows}
    used = [m for m in matches if m.day < latest_cutoff or m.key in target_keys]
    selection = {'league': 'ELC', 'division': 'E1', 'protocol': PROTOCOL, 'planSha256': sha(plan),
        'candidateId': best['id'], 'glm': best['glm'], 'elo': plan['elo'], 'glm_weight': best['glm_weight'],
        'validation': best['metrics'], 'selectionSeasons': plan['selectionSeasons'], 'coverage': coverage,
        'selectionDataSha256': sha(training_rows(used)), 'chosenBeforeAudit': True,
        'promotedToDefault': False}
    return selection, trials


def diagnose(rows, key):
    labels = ('home', 'draw', 'away')
    return {'byActualOutcome': {label: summarize([r for r in rows if r['actual'] == index], key)
                               for index, label in enumerate(labels)},
        'meanProbabilities': np.mean([row[key] for row in rows], axis=0).tolist(),
        'observedOutcomeShares': [sum(row['actual'] == index for row in rows) / len(rows) for index in range(3)],
        'largestLogLoss': [{'date': row['date'], 'home': row['home'], 'away': row['away'],
                           'actual': row['actual'], 'probabilities': row[key],
                           'logloss': loss(row[key], row['actual'])['logloss']}
                          for row in sorted(rows, key=lambda r: loss(r[key], r['actual'])['logloss'], reverse=True)[:10]]}


def evaluate_period(matches, selection, plan, name, seasons):
    rows, coverage = common_targets(matches, seasons, plan['minimumCommonMatches'][name])
    ratings = elo_predictions(matches, rows, plan['elo'])
    predictions = {}
    for recipe, key in [(selection, 'candidate'), (plan['referenceRecipe'], 'transferred')]:
        identity = sha(recipe['glm'])
        if identity not in predictions:
            predictions[identity] = glm_predictions((matches, rows, recipe['glm']))['predictions']
        rows = blend(rows, predictions[identity], ratings, recipe['glm_weight'], key)
    metrics = {key: summarize(rows, key) for key in ('candidate', 'transferred', 'v1', 'base')}
    intervals = {reference: paired_bootstrap(rows, 'candidate', reference, samples=plan['bootstrapSamples'])
                 for reference in ('v1', 'transferred', 'base')}
    block = {'seasons': seasons, **coverage, 'metrics': metrics, 'pairedIntervals': intervals,
             'errors': {key: diagnose(rows, key) for key in ('candidate', 'transferred', 'v1')}}
    return block, rows


def scoped_candidate(matches, selection, as_of, *, manifests=(), qualification=None, **options):
    if selection.get('league') != 'ELC' or selection.get('division') != 'E1':
        raise ValueError('Championship weights require an ELC selection')
    model, report, rows = build_candidate(matches, selection, as_of, manifests=manifests, **options)
    model.pop('model_version')
    model.update(league='ELC', division='E1', protocol=NIGHTLY_PROTOCOL, selection_sha256=sha(selection),
                 team_identity='Football-Data.co.uk canonical names within E1 only')
    if qualification is not None:
        model['research_qualification'] = qualification
    model['model_version'] = 'ELC-candidate-' + as_of.isoformat() + '-' + sha(model)[:12]
    report.update(league='ELC', division='E1', protocol=NIGHTLY_PROTOCOL,
                  modelVersion=model['model_version'], modelSha256=sha(model), codeSha256=code_sha(),
                  selectionSha256=sha(selection))
    if qualification is not None:
        report['historicalQualification'] = qualification
    return model, report, rows


def markdown(report):
    lines = ['# Обучение Чемпионшипа на расширенной истории', '',
        f"Проверено исходных игр: {report['sourceMatches']}. Выбрано сочетаний настроек: "
        f"{report['candidateCount']}; выбран рецепт `{report['selection']['candidateId']}`.", '',
        'Настройки выбраны на сезонах 2023/24 и 2024/25 и сохранены до расчёта более поздних метрик. '
        'Поздние сезоны уже частично изучались раньше; это ретроспективная разработка.', '',
        '| Период | Общих матчей | Модель | Brier ↓ | Log Loss ↓ | Совпал исход |',
        '| --- | ---: | --- | ---: | ---: | ---: |']
    for period, block in report['results'].items():
        for key, value in block['metrics'].items():
            lines.append(f"| {period} | {value['n']} | {key} | {value['brier']:.6f} | "
                         f"{value['logloss']:.6f} | {value['accuracy']:.1%} |")
    lines += ['', '`transferred` — прежний рецепт АПЛ, переобученный на этой же расширенной истории Чемпионшипа. '
        '`v1` — формула базовой модели с тем же консервативным барьером дат, а не исходная серия из 100 прогнозов.', '',
        f"Критерий исторического преимущества: {report['historicalGatePassed']}. "
        'Основная модель автоматически не меняется.', '',
        'Отличия кандидата от v1 в сезоне 2025/26, 95% интервалы по UTC дням:', '']
    for metric, value in report['results']['audit']['pairedIntervals']['v1'].items():
        lines.append(f"- {metric}: {value['difference']:+.6f}; "
                     f"[{value['ci95'][0]:+.6f}; {value['ci95'][1]:+.6f}].")
    lines += ['', f"Время исследования после загрузки данных: {report['durationSeconds']:.2f} с. "
        'Обновление финальных весов выполняется отдельно, с уже выбранными настройками.', '', 'Ограничения:', '']
    lines.extend('- ' + value for value in report['limitations'])
    return '\n'.join(lines) + '\n'


def experiment(directory, output, as_of, *, offline=False, snapshot=None):
    plan = json.loads(PLAN.read_text()); validate_plan(plan)
    matches, sources = load_snapshot(snapshot, as_of) if snapshot else load_history(directory, as_of, offline=offline)
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    selection, trials = select_settings(matches, plan)
    dump(output / 'selection.json', selection)  # Choice precedes any later-period calculation.
    dump(output / 'validation-grid.json', trials)
    blocks, predictions = {}, {}
    for name, seasons in [('audit', plan['auditSeasons']), ('monitoring', plan['monitoringSeasons'])]:
        blocks[name], predictions[name] = evaluate_period(matches, selection, plan, name, seasons)
    point_gate = all(blocks[period]['metrics']['candidate'][metric] < blocks[period]['metrics'][reference][metric]
                     for period in blocks for reference in ('v1', 'base', 'transferred') for metric in ('brier', 'logloss'))
    historical_gate = point_gate and all(blocks['audit']['pairedIntervals']['v1'][metric]['ci95'][1] < 0
                                        for metric in ('brier', 'logloss'))
    report = {'schemaVersion': 1, 'protocol': PROTOCOL, 'league': 'ELC', 'division': 'E1',
        'asOf': as_of.isoformat(), 'completedAt': datetime.now(timezone.utc).isoformat(),
        'planSha256': sha(plan), 'codeSha256': code_sha(), 'sourceMatches': len(matches), 'dataSources': sources,
        'dataSha256': sha(training_rows(matches)), 'candidateCount': len(trials), 'selection': selection,
        'selectionSha256': sha(selection), 'results': blocks, 'pointGatePassed': point_gate,
        'historicalGatePassed': historical_gate, 'durationSeconds': time.monotonic() - started,
        'predictionsSha256': {name: sha(rows) for name, rows in predictions.items()},
        'promotedToDefault': False, 'originalHundredChanged': False, 'prospectiveValidationRequired': True,
        'retrospective': True, 'limitations': plan['limitations']}
    dump(output / 'report.json', report)
    (output / 'report.md').write_text(markdown(report))
    for name, rows in predictions.items():
        (output / (name + '-predictions.json')).write_text(json.dumps(rows, ensure_ascii=False, allow_nan=False) + '\n')
    body = json.dumps(training_rows(matches), ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    compressed = gzip.compress(body, mtime=0)
    (output / 'training-data.json.gz').write_bytes(compressed)
    dump(output / 'input-manifest.json', {'schemaVersion': 1, 'league': 'ELC', 'division': 'E1',
        'dataSha256': sha(training_rows(matches)), 'compressedSha256': hashlib.sha256(compressed).hexdigest(),
        'uncompressedSha256': hashlib.sha256(body).hexdigest(), 'sourceMatches': len(matches), 'dataSources': sources})
    print(json.dumps({'sourceMatches': len(matches), 'selected': selection['candidateId'],
        'selectionMetrics': selection['validation'], 'historicalGatePassed': historical_gate,
        'seconds': report['durationSeconds'], 'results': {name: block['metrics'] for name, block in blocks.items()}}, ensure_ascii=False), flush=True)
    return report


def refresh(directory, output, as_of, *, offline=False, selection_path=SELECTION):
    if Path(output).resolve() == Path(selection_path).resolve().parent:
        raise ValueError('Nightly output must not overwrite the original selection study')
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    for name in ('model.json', 'report.json', 'report.md', 'evaluation.json', 'training-data.json.gz', 'attempt.json'):
        (output / name).unlink(missing_ok=True)
    generated = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    try:
        plan = json.loads(PLAN.read_text()); validate_plan(plan)
        selection = json.loads(Path(selection_path).read_text())
        if selection.get('planSha256') != sha(plan) or selection.get('chosenBeforeAudit') is not True:
            raise ValueError('The frozen Championship selection does not match its plan')
        study = json.loads(Path(selection_path).with_name('report.json').read_text())
        if (study.get('protocol') != PROTOCOL or study.get('selectionSha256') != sha(selection)
                or type(study.get('historicalGatePassed')) is not bool):
            raise ValueError('The Championship study does not verify the selected settings')
        qualification = {'protocol': PROTOCOL, 'selectionSha256': sha(selection),
                         'historicalGatePassed': study['historicalGatePassed'],
                         'retrospective': True, 'prospectiveValidationRequired': True}
        matches, sources = load_history(directory, as_of, offline=offline)
        loaded = time.monotonic()
        model, report, rows = scoped_candidate(matches, selection, as_of, manifests=sources, qualification=qualification)
        report['timings']['dataLoadSeconds'] = loaded - started
        report['generatedAt'] = generated
        report['computeSeconds'] = report['durationSeconds']
        report['durationSeconds'] = time.monotonic() - started
        dump(output / 'model.json', model); dump(output / 'report.json', report)
        (output / 'evaluation.json').write_text(json.dumps(rows, ensure_ascii=False, allow_nan=False) + '\n')
        (output / 'training-data.json.gz').write_bytes(gzip.compress(
            json.dumps(training_rows(matches), ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))
        from research.nightly import markdown as nightly_markdown
        text = nightly_markdown(report).replace('# Ночное обучение кандидата', '# Чемпионшип: отдельный ночной кандидат', 1)
        text += '\nКритерий исходного исторического исследования: ' + str(study['historicalGatePassed']) + '. '
        text += 'Ежедневное обновление весов не доказывает рост точности и не меняет основную модель.\n'
        (output / 'report.md').write_text(text)
        attempt = report
        print(json.dumps({'modelVersion': model['model_version'], 'league': 'ELC',
                          'trainingMatches': report['trainingMatches'], 'seconds': report['durationSeconds']}), flush=True)
    except Exception as error:
        attempt = {'schemaVersion': 1, 'protocol': NIGHTLY_PROTOCOL, 'league': 'ELC', 'division': 'E1',
            'status': 'failed', 'asOf': as_of.isoformat(), 'generatedAt': generated,
            'errorType': type(error).__name__, 'message': 'The Championship candidate was not completed.',
            'promotedToDefault': False}
        raise
    finally:
        dump(output / 'attempt.json', attempt)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('experiment', 'refresh'))
    parser.add_argument('--data-dir', type=Path)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--as-of', type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    if args.mode == 'refresh' and (not args.data_dir or args.snapshot):
        parser.error('refresh requires --data-dir and does not accept --snapshot')
    if args.mode == 'experiment' and not (args.data_dir or args.snapshot):
        parser.error('experiment requires --data-dir or --snapshot')
    if args.mode == 'experiment':
        experiment(args.data_dir, args.output, args.as_of, offline=args.offline, snapshot=args.snapshot)
    else:
        refresh(args.data_dir, args.output, args.as_of, offline=args.offline)
