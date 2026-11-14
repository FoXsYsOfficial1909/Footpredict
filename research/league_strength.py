"""Replay one fixed, league-specific attack/defence and Elo candidate offline."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time

import numpy as np

from research.data import Match
from research.evaluate import elo_predictions, paired_bootstrap, summarize
from research.hundred import DIRECTORY, REGISTERED_SHA, instant, verify_files
from research.models import LearnedPoisson
from research.nightly import sha

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'docs/research/league-strength-plan-2026-11-14.json'
PROTOCOL = 'league-strength-replay-1'


def cutoff_date(row):
    """Round the result-safety fence down, never forward into unknown results."""
    kickoff = instant(row['kickoff'])
    registration = instant(row['simulatedForecastAt'])
    if kickoff - registration != timedelta(hours=24):
        raise ValueError('Expected an exact 24-hour historical registration')
    return (registration - timedelta(hours=4)).date()


def matches_for_league(code, games):
    seen = set()
    matches = []
    for game in games:
        if game['id'] in seen:
            raise ValueError('Duplicate source fixture')
        seen.add(game['id'])
        score = game['score']['fullTime']
        if (game['status'] != 'FINISHED' or game['score']['duration'] != 'REGULAR'
                or game['homeTeam']['id'] == game['awayTeam']['id']
                or any(type(score.get(side)) is not int or not 0 <= score[side] <= 30
                       for side in ('home', 'away'))):
            raise ValueError('Invalid regulation-time training fixture')
        matches.append(Match(instant(game['utcDate']).date(), game['__year'],
            f"{code}:{game['homeTeam']['id']}", f"{code}:{game['awayTeam']['id']}",
            score['home'], score['away']))
    if len({match.key for match in matches}) != len(matches):
        raise ValueError('Ambiguous training fixture identity')
    return sorted(matches, key=lambda match: match.key)


def forecast_league(code, games, targets, settings, minimum=100):
    """Fit only this league, once per distinct cutoff; reject unknown clubs."""
    matches = matches_for_league(code, games)
    prepared = sorted([{**row, 'home': f"{code}:{row['homeId']}",
                        'away': f"{code}:{row['awayId']}",
                        'cutoff_date': cutoff_date(row).isoformat()} for row in targets],
                      key=lambda row: (row['cutoff_date'], row['kickoff'], row['fixtureId']))
    if any(row['league'] != code for row in prepared):
        raise ValueError('A foreign league cannot enter a model')
    elo = elo_predictions(matches, prepared, settings['elo'])
    model = LearnedPoisson(**settings['glm'])
    rows, excluded, fits = [], [], []
    previous_cutoff = None
    eligible = []
    for row, elo_probability in zip(prepared, elo):
        cutoff = date.fromisoformat(row['cutoff_date'])
        if cutoff != previous_cutoff:
            eligible = [match for match in matches
                        if 0 < (cutoff - match.day).days <= settings['glm']['window_days']]
            previous_cutoff = cutoff
            if len(eligible) >= minimum:
                model.fit(matches, cutoff)
                fits.append({'league': code, 'cutoffExclusive': cutoff.isoformat(),
                    **model.fit_info,
                    'trainingSha256': sha([(m.day.isoformat(), m.season, m.home, m.away,
                                           m.home_goals, m.away_goals) for m in eligible])})
        reason = None
        if len(eligible) < minimum:
            reason = 'fewer_than_100_past_matches'
        elif row['home'] not in model.teams or row['away'] not in model.teams:
            reason = 'unseen_team'
        if reason:
            excluded.append({'fixtureId': row['fixtureId'], 'league': code, 'reason': reason})
            continue
        prediction = model.predict(row['home'], row['away'], date.fromisoformat(row['date']))
        weight = settings['glm_weight']
        candidate = weight * prediction['probabilities'] + (1 - weight) * np.array(elo_probability)
        rows.append({**row, 'glm': prediction['probabilities'].tolist(), 'elo': elo_probability,
                     'candidate': candidate.tolist(),
                     'expectedGoalsGlm': [prediction['home_goals'], prediction['away_goals']],
                     'trainingMatches': model.fit_info['training_games'],
                     'latestTrainingDate': model.fit_info['latest_training_date'],
                     'trainingSha256': fits[-1]['trainingSha256']})
    return rows, excluded, fits


def validate_replay(replay, history, packet, plan):
    if (plan['protocol'] != PROTOCOL or plan['candidateCount'] != 1
            or plan['automaticPromotion'] is not False
            or plan['inputCohortSha256'] != REGISTERED_SHA
            or replay.get('protocol') != 'mixed-v1-replay-1'
            or replay['sourceCohortSha256'] != REGISTERED_SHA
            or replay.get('leadHours') != 24 or replay.get('resultSafetyHours') != 4
            or set(plan['leagues']) != set(packet['leagues'])):
        raise ValueError('Unexpected experiment identity or information fence')
    expected_manifest = {code: {'league': code, 'rows': len(history[code]),
                              'inputSha256': packet['inputSha256'][code]} for code in plan['leagues']}
    if (len(replay['sourceManifest']) != len(expected_manifest)
            or {item['league']: item for item in replay['sourceManifest']} != expected_manifest):
        raise ValueError('Replay does not describe the frozen input files')
    index = {(code, game['id']): game for code, games in history.items() for game in games}
    seen = set()
    for row in replay['rows']:
        key = row['league'], row['fixtureId']
        game = index.get(key)
        if key in seen or game is None:
            raise ValueError('Duplicate or unknown replay fixture')
        seen.add(key)
        score = game['score']['fullTime']
        actual = 0 if score['home'] > score['away'] else 1 if score['home'] == score['away'] else 2
        if (instant(row['kickoff']) != instant(game['utcDate'])
                or row['date'] != instant(game['utcDate']).date().isoformat()
                or row['homeId'] != game['homeTeam']['id'] or row['awayId'] != game['awayTeam']['id']
                or row['season'] != game['__year'] or row['actual'] != actual):
            raise ValueError('Replay identity or historical result changed')
        cutoff_date(row)
        for name in ('v1', 'base'):
            p = row[name]
            if len(p) != 3 or any(not np.isfinite(v) or v < 0 for v in p) or abs(sum(p) - 1) > 1e-8:
                raise ValueError('Invalid reference probability vector')


def evaluate(replay, history, packet, plan):
    validate_replay(replay, history, packet, plan)
    settings = plan['candidate']
    selection = json.loads((ROOT / plan['settingsSource']).read_text())
    if settings != {key: selection[key] for key in ('glm', 'elo', 'glm_weight')}:
        raise ValueError('The previously selected recipe changed')
    bounds = [plan['validationPeriod'], plan['auditPeriod']]
    targets = [row for row in replay['rows'] if any(a <= row['date'] < b for a, b in bounds)]
    rows, excluded, fits = [], [], []
    started = time.monotonic()
    for code in plan['leagues']:
        result = forecast_league(code, history[code], [row for row in targets if row['league'] == code],
                                 settings, plan['minimumTrainingMatches'])
        rows.extend(result[0]); excluded.extend(result[1]); fits.extend(result[2])
    fitting_seconds = time.monotonic() - started
    rows.sort(key=lambda row: (row['kickoff'], row['league'], row['fixtureId']))
    results = {}
    for name, period in [('validation', plan['validationPeriod']), ('audit', plan['auditPeriod'])]:
        selected = [row for row in rows if period[0] <= row['date'] < period[1]]
        if not selected:
            raise ValueError('Empty evaluation period')
        results[name] = {'period': period, 'matches': len(selected),
            'metrics': {key: summarize(selected, key) for key in ('v1', 'base', 'candidate')},
            'pairedIntervals': paired_bootstrap(selected, 'candidate', 'v1', samples=plan['bootstrapSamples']),
            'byLeague': {code: {'matches': sum(row['league'] == code for row in selected),
                'metrics': {key: summarize([row for row in selected if row['league'] == code], key)
                            for key in ('v1', 'base', 'candidate')}} for code in plan['leagues']}}
    aggregate_gate = all(results[period]['metrics']['candidate'][key] < results[period]['metrics']['v1'][key]
                         for period in results for key in plan['primaryMetrics'])
    league_gate = all(value['matches'] > 0 and all(
        value['metrics']['candidate'][key] <= value['metrics']['v1'][key] for key in plan['primaryMetrics'])
        for value in results['audit']['byLeague'].values())
    preliminary_gate = aggregate_gate and league_gate
    advantage_gate = preliminary_gate and all(
        results['audit']['pairedIntervals'][key]['ci95'][1] < 0 for key in plan['primaryMetrics'])
    report = {'schemaVersion': 1, 'protocol': PROTOCOL, 'planSha256': sha(plan),
        'sourceCohortSha256': REGISTERED_SHA, 'inputSha256': packet['inputSha256'],
        'settings': settings, 'sourceMatches': sum(len(games) for games in history.values()),
        'referenceReplayedMatches': len(replay['rows']), 'targetMatches': len(targets),
        'evaluatedMatches': len(rows), 'excluded': excluded,
        'fits': {'total': len(fits), 'byLeague': {code: sum(fit['league'] == code for fit in fits)
                                                for code in plan['leagues']},
                 'fittingAndPredictionSeconds': fitting_seconds},
        'results': results, 'aggregateGatePassed': aggregate_gate, 'allLeagueAuditGatePassed': league_gate,
        'preliminaryGatePassed': preliminary_gate, 'historicalAdvantageGatePassed': advantage_gate,
        'promotedToDefault': False, 'originalHundredChanged': False,
        'prospectiveValidationRequired': True, 'retrospective': True, 'limitations': plan['limitations']}
    return report, rows, fits


def markdown(report):
    lines = ['# Отдельные модели атаки и защиты для четырёх лиг', '',
        f"Исходных игр: {report['sourceMatches']}; прогнозов v1: {report['referenceReplayedMatches']}; "
        f"проверено новых прогнозов: {report['evaluatedMatches']} из {report['targetMatches']}. "
        f"Исключено: {len(report['excluded'])}.", '',
        'Один фиксированный рецепт: 75% обученного Пуассона + 25% Elo. Настройки взяты из прежнего '
        '`selection.json`; параметры команд и средние обучены отдельно для каждой лиги. '
        'Перебора настроек и выбора лучшей лиги по результатам нет.', '',
        'Обучение обновляется последовательно перед каждым прогнозом. Используются только более ранние результаты; '
        'из-за датового формата модели исключён также весь пограничный день четырёхчасового барьера. '
        'Модель v1 сохраняет исходный, менее консервативный барьер.', '',
        '| Период | Матчей | Модель | Brier ↓ | Log Loss ↓ | Совпал исход |',
        '| --- | ---: | --- | ---: | ---: | ---: |']
    for period, value in report['results'].items():
        for name, metric in value['metrics'].items():
            lines.append(f"| {period} | {metric['n']} | {name} | {metric['brier']:.6f} | "
                         f"{metric['logloss']:.6f} | {metric['accuracy']:.1%} |")
    lines += ['', 'Разбивка позднего периода по лигам:', '',
        '| Лига | Матчей | Brier v1 | Brier кандидат | Log Loss v1 | Log Loss кандидат |',
        '| --- | ---: | ---: | ---: | ---: | ---: |']
    for code, value in report['results']['audit']['byLeague'].items():
        v1, candidate = value['metrics']['v1'], value['metrics']['candidate']
        if value['matches']:
            lines.append(f"| {code} | {value['matches']} | {v1['brier']:.6f} | {candidate['brier']:.6f} | "
                         f"{v1['logloss']:.6f} | {candidate['logloss']:.6f} |")
    lines += ['', 'Парные отличия кандидата от v1 в позднем периоде (95% интервалы по UTC дням):', '']
    for key, value in report['results']['audit']['pairedIntervals'].items():
        lo, hi = value['ci95']
        lines.append(f"- {key}: {value['difference']:+.6f}, интервал [{lo:+.6f}; {hi:+.6f}].")
    lines += ['', f"Предварительный критерий: {report['preliminaryGatePassed']}. "
        f"Критерий исторического преимущества: {report['historicalAdvantageGatePassed']}.", '',
        f"Выполнено {report['fits']['total']} обучений для уникальных дат среза; "
        f"обучение и прогнозы заняли {report['fits']['fittingAndPredictionSeconds']:.2f} с на этой машине. "
        'Матчи с одной датой среза используют одну обученную модель.', '',
        'Это ретроспективная разработка на уже изучавшейся истории. Основная модель и исходные 100 прогнозов '
        'не изменены; будущая точность пока не доказана. Критерий требует улучшения обеих общих метрик '
        'в обоих периодах, отсутствия ухудшения по каждой лиге позднего периода и верхних границ '
        'обоих общих интервалов ниже нуля.', '', 'Ограничения:', '']
    lines.extend('- ' + limitation for limitation in report['limitations'])
    lines += ['', 'Воспроизведение без сетевых запросов:', '', '```bash',
        'node scripts/replay_historical_v1.mjs /tmp/footpredict-league-strength',
        'OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.league_strength \\',
        '  --replay /tmp/footpredict-league-strength/replay.json \\',
        '  --output /tmp/footpredict-league-strength/report', '```']
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replay', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(PLAN.read_text())
    packet = verify_files()
    history = {code: json.loads((DIRECTORY / 'inputs' / (code + '.json')).read_bytes())
               for code in plan['leagues']}
    raw = args.replay.read_bytes()
    report, rows, fits = evaluate(json.loads(raw), history, packet, plan)
    report['replaySha256'] = hashlib.sha256(raw).hexdigest()
    report['predictionsSha256'] = sha(rows)
    report['fitsSha256'] = sha(fits)
    report['completedAt'] = datetime.now(timezone.utc).isoformat()
    report['codeSha256'] = sha({name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ('research/league_strength.py', 'research/models.py', 'research/evaluate.py',
                     'research/data.py', 'research/hundred.py', 'requirements-research.txt',
                     'scripts/replay_historical_v1.mjs')})
    args.output.mkdir(parents=True, exist_ok=True)
    for name, value in [('report', report), ('predictions', rows), ('fits', fits)]:
        body = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        (args.output / (name + '.json')).write_text(body + '\n')
    (args.output / 'report.md').write_text(markdown(report))
    print(json.dumps({'evaluated': report['evaluatedMatches'], 'excluded': len(report['excluded']),
        'fits': report['fits'], 'preliminaryGatePassed': report['preliminaryGatePassed'],
        'historicalAdvantageGatePassed': report['historicalAdvantageGatePassed'],
        'results': {name: value['metrics'] for name, value in report['results'].items()}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
