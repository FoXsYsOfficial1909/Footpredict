"""Refresh corner-count weights from a frozen recipe without reselecting it."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import time

from research.corner_experiment import baseline_predictions, predict, summarize, target_rows
from research.corners import CornerModel, LINES, empiricalLeague, load_corners
from research.data import ALIASES, download_season
from research.nightly import sha

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = 'corner-candidate-1'


def recipe(selection):
    value = selection.get('chosen', selection)
    if value is None:
        raise ValueError('A persisted corner recipe is required')
    value = value.get('settings', value)
    if (value.get('family') not in ('poisson', 'negative_binomial')
        or value.get('halfLife') not in (180, 365) or value.get('ridge') != .03
        or value.get('windowDays') != 1095 or not isinstance(value.get('id'), str)):
        raise ValueError('Unregistered fixed corner recipe')
    return value


def build_candidate(observations, selection, as_of, *, manifests=(), evaluation_limit=120, min_common=60):
    started = time.monotonic()
    selected = recipe(selection)
    serving_policy = selection.get('servingPolicy', 'selected_model')
    if serving_policy not in ('selected_model', 'empirical_league'):
        raise ValueError('Unknown corner serving policy')
    cutoff = as_of - timedelta(days=1)
    available = sorted((row for row in observations if row.match.day < cutoff), key=lambda r: r.match.key)
    if len({row.match.key for row in available}) != len(available):
        raise ValueError('Duplicate corner training fixture')
    paired = [row for row in available if row.total is not None]
    if len(paired) < 200:
        raise ValueError('At least 200 verified past corner matches are required')
    # Keep the complete first evaluation date so team ordering cannot choose a cohort.
    first = paired[max(0, len(paired) - evaluation_limit)].match.day
    seasons = {row.match.season for row in paired if row.match.day >= first}
    targets = [row for row in target_rows(available, seasons) if row['date'] >= first.isoformat()]
    if len(targets) < min_common:
        raise ValueError('Too few common corner monitoring fixtures')
    rows = baseline_predictions(available, targets, {'halfLife': 365, 'windowDays': 1095, 'smoothing': .5})
    if serving_policy == 'empirical_league':
        rows = [{**row, 'challenger': row['baseline']} for row in rows]
    else:
        rows = predict((available, rows, selected))
    metrics = {key: summarize(rows, key, list(LINES)) for key in ('challenger', 'baseline')}
    if serving_policy == 'empirical_league':
        estimate = empiricalLeague(available, cutoff, half_life=365, window_days=1095)
        consumed = [row for row in paired if 0 < (cutoff - row.match.day).days <= 1095]
        clubs = sorted({name for row in consumed for name in (row.match.home, row.match.away)})
        exported = {'schemaVersion': 1, 'target': 'corners_total_regulation', 'status': 'experimental',
            'family': 'empirical', 'as_of': cutoff.isoformat(), 'lines': list(LINES), 'aliases': ALIASES,
            'teams': {name: {'attack': 0., 'defence': 0.} for name in clubs},
            'fit': dict(estimate['fit']), 'league_estimate': estimate,
            'parameters': {'half_life': 365, 'window_days': 1095, 'smoothing': .5},
            'scope': 'league_baseline', 'reason': 'team_model_not_validated'}
        model_training_matches = len(consumed)
        latest = estimate['fit']['latest_training_date']
        served_settings = {'id': 'empirical-league', 'family': 'empirical', 'halfLife': 365,
                           'windowDays': 1095, 'smoothing': .5}
    else:
        model = CornerModel(half_life=selected['halfLife'], ridge=selected['ridge'],
                            window_days=selected['windowDays'], family=selected['family']).fit(available, cutoff)
        exported = model.export()
        model_training_matches = model.fit_info['paired_corners_games']
        latest = model.fit_info['latest_training_date']
        served_settings = selected
    exported['protocol'] = PROTOCOL
    exported['training_cutoff_exclusive'] = cutoff.isoformat()
    exported['promoted_to_default'] = False
    prefix = 'corners-league-' if serving_policy == 'empirical_league' else 'corners-candidate-'
    exported['model_version'] = prefix + as_of.isoformat() + '-' + sha(exported)[:12]
    report = {'schemaVersion': 1, 'protocol': PROTOCOL, 'status': 'success',
        'asOf': as_of.isoformat(), 'trainingCutoffExclusive': cutoff.isoformat(),
        'latestTrainingMatch': latest, 'trainingMatches': len(available),
        'pairedCornerMatches': len(paired), 'modelTrainingMatches': model_training_matches,
        'modelVersion': exported['model_version'], 'modelSha256': sha(exported),
        'settings': served_settings, 'settingsSha256': sha(served_settings),
        'servingPolicy': serving_policy, 'researchSelection': selection,
        'dataSha256': sha([(r.match.day.isoformat(), r.match.season, r.match.home, r.match.away,
                           r.home_corners, r.away_corners) for r in available]),
        'codeSha256': sha({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ['research/corner_nightly.py', 'research/corner_experiment.py',
                         'research/corners.py', 'research/models.py', 'research/data.py', 'requirements-research.txt']}),
        'sourceCoverage': {'resultMatches': len(available), 'pairedCornerMatches': len(paired),
            'missingCornerMatches': len(available) - len(paired),
            'latestResultDate': available[-1].match.day.isoformat(), 'latestCornerDate': latest},
        'evaluation': {'kind': 'rolling_historical_corner_procedure', 'targets': len(rows),
            'commonMatches': len(rows), 'from': rows[0]['date'], 'to': rows[-1]['date'],
            'lines': list(LINES), 'metrics': metrics},
        'dataSources': list(manifests), 'durationSeconds': time.monotonic() - started,
        'promotedToDefault': False, 'prospectiveValidationRequired': True,
        'limitations': ['The persisted corner recipe is fixed; no new search or selection occurs.',
            'Monitoring evaluates rolling historical fits, not the final weights on future matches.',
            'Corner event accuracy is not comparable to three-way match-outcome accuracy.',
            'Target and immediately preceding calendar-day observations are excluded.',
            'Weather, injuries, actual lineups and unverified workload are not numeric corner predictors.']}
    return exported, report, rows


def markdown(report):
    lines = ['# Ежедневное обновление модели угловых', '',
        f"Срез: {report['asOf']}; последние использованные угловые: {report['latestTrainingMatch']}.",
        f"Версия: `{report['modelVersion']}`. Полных наблюдений: {report['pairedCornerMatches']}.", '',
        '| Мониторинг | Матчей | Log loss ↓ | Brier ↓ | MAE ↓ |', '| --- | ---: | ---: | ---: | ---: |']
    for name, m in report['evaluation']['metrics'].items():
        label = ('Ориентир лиги' if report.get('servingPolicy') == 'empirical_league' else 'Кандидат') if name == 'challenger' else 'Прошлые частоты лиги'
        lines.append(f"| {label} | {m['n']} | {m['logloss']:.6f} | {m['brier']:.6f} | {m['meanAbsoluteError']:.3f} |")
    lines += ['', 'Вероятности событий: угловых больше 8,5 / 9,5 / 10,5. Рецепт зафиксирован, повторного выбора нет.',
              'Это исторический мониторинг. Будущая точность ещё требует независимой проверки.',
              'Модель исхода матча и ранее сохранённые прогнозы не меняются.',
              '', f"Время расчёта: {report['durationSeconds']:.2f} с.", '']
    if report.get('servingPolicy') == 'empirical_league':
        lines += ['Показан общий ориентир по прошлым матчам лиги. Командная модель не прошла проверку и не используется для пользовательских вероятностей.',
                  'Равенство мониторинговых метрик здесь ожидаемо: сравниваются одинаковые частоты лиги, а не доказанный прирост качества.', '']
    return '\n'.join(lines)


def run(data_dir, output, as_of, *, selection_path, offline=False):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    for name in ('model.json', 'report.json', 'report.md', 'attempt.json', 'evaluation.json', 'training-data.json.gz'):
        path = output / name
        if path.exists():
            path.unlink()
    generated = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    try:
        selected = json.loads(Path(selection_path).read_text())
        recipe(selected)  # Fail before provider calls if a recipe has not been persisted.
        season = as_of.year if as_of.month >= 7 else as_of.year - 1
        for year in range(2015, season + 1):
            path = Path(data_dir) / (str(year) + '-E0.csv')
            if offline and not path.exists():
                raise ValueError('Offline corner data are incomplete')
            if not offline:
                download_season(year, data_dir, refresh=year == season)
        observations, manifests = load_corners(data_dir, range(2015, season + 1))
        if any(source['rows'] != 380 for source in manifests if source['season'] < season):
            raise ValueError('A completed EPL season must contain 380 verified results')
        model, report, predictions = build_candidate(observations, selected, as_of, manifests=manifests)
        report['computeSeconds'] = report['durationSeconds']
        report['durationSeconds'] = time.monotonic() - started
        report['generatedAt'] = generated
        for name, value in [('model.json', model), ('report.json', report), ('evaluation.json', predictions)]:
            (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        cutoff = as_of - timedelta(days=1)
        training = [{'date': row.match.day.isoformat(), 'season': row.match.season,
            'home': row.match.home, 'away': row.match.away,
            'homeCorners': row.home_corners, 'awayCorners': row.away_corners}
            for row in observations if row.match.day < cutoff]
        body = json.dumps(training, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        (output / 'training-data.json.gz').write_bytes(gzip.compress(body, mtime=0))
        (output / 'report.md').write_text(markdown(report))
        attempt = report
        print(json.dumps({'modelVersion': report['modelVersion'], 'pairedCornerMatches': report['pairedCornerMatches'],
                          'seconds': report['durationSeconds']}), flush=True)
    except Exception as error:
        attempt = {'schemaVersion': 1, 'protocol': PROTOCOL, 'status': 'failed',
            'asOf': as_of.isoformat(), 'generatedAt': generated, 'errorType': type(error).__name__,
            'message': 'Corner training did not complete. Inspect the failed workflow step; the last good model is retained.',
            'promotedToDefault': False, 'prospectiveValidationRequired': True}
        raise
    finally:
        (output / 'attempt.json').write_text(json.dumps(attempt, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--selection', required=True, type=Path)
    parser.add_argument('--as-of', type=date.fromisoformat, default=datetime.now(timezone.utc).date())
    parser.add_argument('--offline', action='store_true')
    args = parser.parse_args()
    run(args.data_dir, args.output, args.as_of, selection_path=args.selection, offline=args.offline)
