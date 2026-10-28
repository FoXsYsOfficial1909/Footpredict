"""Prespecified chronological validation, one held-out-period evaluation, and export."""
from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta
import json
import math
from pathlib import Path
import time

import numpy as np

from research.data import ALIASES, load_seasons
from research.models import LearnedPoisson, loss, v1_statistics

GRID = [{'half_life': half_life, 'ridge': ridge, 'window_days': 1095}
        for half_life in (180, 365, 730) for ridge in (.03, .15)]
ELO_GRID = [{'k': 20, 'home_advantage': 65, 'draw_weight': .9},
            {'k': 30, 'home_advantage': 80, 'draw_weight': .9}]
ENSEMBLE_WEIGHTS = [0., .25, .5, .75, 1.]


def summarize(rows, key):
    values = [loss(row[key], row['actual']) for row in rows if row.get(key) is not None]
    if not values:
        return {'n': 0, 'brier': None, 'logloss': None, 'accuracy': None}
    return {'n': len(values), 'brier': float(np.mean([x['brier'] for x in values])),
            'logloss': float(np.mean([x['logloss'] for x in values])),
            'accuracy': float(np.mean([x['correct'] for x in values]))}


def base_distribution(matches, cutoff, season):
    counts = np.ones(3)
    for match in matches:
        if match.day >= cutoff or match.season not in (season, season-1):
            continue
        weight = math.exp(-math.log(2) * (cutoff-match.day).days / 120) * (1 if match.season == season else .35)
        counts[match.outcome] += weight
    return (counts/counts.sum()).tolist()


def reference_rows(matches, seasons):
    result, base_cache = [], {}
    for match in matches:
        if match.season not in seasons:
            continue
        # CSV dates do not establish a reliable final-whistle publication timestamp.
        # Deliberately exclude the immediately preceding calendar day's games too.
        cutoff = match.day-timedelta(days=1)
        key = (cutoff, match.season)
        if key not in base_cache:
            base_cache[key] = base_distribution(matches, cutoff, match.season)
        old = v1_statistics(matches, cutoff, match.season, match.home, match.away)
        result.append({'date': match.day.isoformat(), 'cutoff_date': cutoff.isoformat(), 'season': match.season,
                       'home': match.home, 'away': match.away, 'actual': match.outcome,
                       'base': base_cache[key], 'v1': old['probabilities'].tolist() if old else None})
    return result


def glm_predictions(arguments):
    matches, rows, settings = arguments
    model, last_cutoff, predictions = LearnedPoisson(**settings), None, []
    started = time.monotonic()
    for row in rows:
        cutoff = date.fromisoformat(row['cutoff_date'])
        if cutoff != last_cutoff:
            model.fit(matches, cutoff)
            last_cutoff = cutoff
        prediction = model.predict(row['home'], row['away'], date.fromisoformat(row['date']))
        predictions.append(prediction['probabilities'].tolist())
    return {'settings': settings, 'predictions': predictions, 'seconds': time.monotonic()-started}


def elo_predictions(matches, rows, settings):
    by_day, targets = defaultdict(list), defaultdict(list)
    for match in matches:
        by_day[match.day].append(match)
    for index, row in enumerate(rows):
        targets[date.fromisoformat(row['cutoff_date'])].append((index, row))
    ratings, predictions, season = {}, [None]*len(rows), None
    all_days = sorted(set(by_day) | set(targets))

    def probabilities(home, away):
        difference = ratings.get(home, 1500.) - ratings.get(away, 1500.) + settings['home_advantage']
        strength = 10 ** (difference/800)
        values = np.array([strength, settings['draw_weight'], 1/strength])
        return (values/values.sum()).tolist()

    for day in all_days:
        # Prediction precedes all updates on cutoff day, preserving the same information fence as the other models.
        for index, row in targets.get(day, []):
            predictions[index] = probabilities(row['home'], row['away'])
        games = by_day.get(day, [])
        if games and games[0].season != season:
            if season is not None:
                ratings = {name: 1500+(value-1500)*.85 for name, value in ratings.items()}
            season = games[0].season
        changes = defaultdict(float)
        for match in games:
            difference = ratings.get(match.home, 1500.) - ratings.get(match.away, 1500.) + settings['home_advantage']
            expectation = 1/(1+10**(-difference/400))
            actual = 1. if match.outcome == 0 else .5 if match.outcome == 1 else 0.
            change = settings['k']*(actual-expectation)
            changes[match.home] += change
            changes[match.away] -= change
        for name, change in changes.items():
            ratings[name] = ratings.get(name, 1500.) + change
    if any(value is None for value in predictions):
        raise RuntimeError('Missing Elo predictions')
    return predictions


def with_predictions(rows, predictions, key):
    if len(rows) != len(predictions):
        raise ValueError('Prediction count does not match target count')
    return [{**row, key: prediction} for row, prediction in zip(rows, predictions)]


def paired_bootstrap(rows, candidate, reference='base', samples=1500):
    groups = defaultdict(list)
    for row in rows:
        groups[row['date']].append(row)
    days = sorted(groups)
    rng = np.random.default_rng(20261028)
    result = {}
    for metric in ('brier', 'logloss'):
        sums, counts = [], []
        for day in days:
            differences = [loss(row[candidate], row['actual'])[metric]-loss(row[reference], row['actual'])[metric] for row in groups[day]]
            sums.append(sum(differences)); counts.append(len(differences))
        sums, counts = np.asarray(sums), np.asarray(counts)
        indices = rng.integers(0, len(days), size=(samples, len(days)))
        distribution = sums[indices].sum(axis=1)/counts[indices].sum(axis=1)
        result[metric] = {'difference': float(sums.sum()/counts.sum()),
                          'ci95': np.quantile(distribution, [.025, .975]).tolist(),
                          'method': 'paired resampling of calendar-day clusters', 'samples': samples}
    return result


def execute(data_dir, output, workers=2):
    output = Path(output);output.mkdir(parents=True, exist_ok=True)
    manifest = {'development_seasons': list(range(2015, 2023)), 'validation_seasons': [2023, 2024],
                'held_out_seasons': [2025], 'current_season_not_used_for_selection': 2026,
                'glm_grid': GRID, 'elo_grid': ELO_GRID, 'ensemble_glm_weights': ENSEMBLE_WEIGHTS,
                'selection_metric': 'validation logloss on common eligible fixtures',
                'data_fence': 'Only matches strictly earlier than the day preceding the target match; no same-day or immediately preceding-day results.',
                'caveat': 'Held out within this experiment. These seasons may have been inspected in previous versions; future prospective validation is still required.'}
    (output/'protocol.json').write_text(json.dumps(manifest, indent=2))
    matches, sources = load_seasons(range(2015, 2027), data_dir)
    (output/'data-manifest.json').write_text(json.dumps(sources, indent=2))
    validation = reference_rows(matches, {2023, 2024})
    test = reference_rows(matches, {2025})
    common_val = [i for i, row in enumerate(validation) if row['v1'] is not None]
    print('Validation targets:',len(validation),'common coverage:',len(common_val),'held-out targets:',len(test),flush=True)
    tasks = [(matches, validation, settings) for settings in GRID]
    candidates = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for item in pool.map(glm_predictions, tasks):
            rows = with_predictions(validation, item['predictions'], 'glm')
            item['validation'] = summarize([rows[i] for i in common_val], 'glm')
            candidates.append(item)
            print('GLM validation:',json.dumps({'settings':item['settings'],'metrics':item['validation'],'seconds':round(item['seconds'],2)}),flush=True)
    chosen_glm = min(candidates, key=lambda item: item['validation']['logloss'])
    elo_candidates=[]
    for settings in ELO_GRID:
        predictions=elo_predictions(matches, validation, settings)
        rows=with_predictions(validation,predictions,'elo')
        elo_candidates.append({'settings':settings,'predictions':predictions,'validation':summarize([rows[i] for i in common_val],'elo')})
    chosen_elo=min(elo_candidates,key=lambda item:item['validation']['logloss'])
    combined=with_predictions(with_predictions(validation,chosen_glm['predictions'],'glm'),chosen_elo['predictions'],'elo')
    ensembles=[]
    for weight in ENSEMBLE_WEIGHTS:
        rows=[{**row,'candidate':(weight*np.array(row['glm'])+(1-weight)*np.array(row['elo'])).tolist()} for row in combined]
        ensembles.append({'glm_weight':weight,'validation':summarize([rows[i] for i in common_val],'candidate')})
    chosen=min(ensembles,key=lambda item:item['validation']['logloss'])
    selection={'glm':chosen_glm['settings'],'elo':chosen_elo['settings'],'glm_weight':chosen['glm_weight'],
               'validation':chosen['validation'],'chosen_before_test':True}
    (output/'selection.json').write_text(json.dumps(selection,indent=2))
    # The held-out predictions are generated only after selection has been persisted.
    test_glm=glm_predictions((matches,test,chosen_glm['settings']))['predictions']
    test_elo=elo_predictions(matches,test,chosen_elo['settings'])
    test_rows=with_predictions(with_predictions(test,test_glm,'glm'),test_elo,'elo')
    for row in test_rows:
        row['candidate']=(chosen['glm_weight']*np.array(row['glm'])+(1-chosen['glm_weight'])*np.array(row['elo'])).tolist()
    common_test=[row for row in test_rows if row['v1'] is not None]
    if len(common_test)<200:
        raise RuntimeError('Insufficient common held-out coverage')
    metrics={key:summarize(common_test,key) for key in ('base','v1','glm','elo','candidate')}
    comparison=paired_bootstrap(common_test,'candidate')
    strong_advantage=all(comparison[metric]['ci95'][1]<0 for metric in ('brier','logloss'))
    summary={'protocol':manifest,'selection':selection,'validation_targets':len(validation),
             'validation_common_targets':len(common_val),'held_out_targets':len(test),
             'held_out_common_targets':len(common_test),'metrics_common':metrics,
             'metrics_full_coverage':{key:summarize(test_rows,key) for key in ('base','glm','elo','candidate')},
             'candidate_minus_base_bootstrap':comparison,'passes_historical_advantage_gate':strong_advantage,
             'promoted_to_default':False,'prospective_validation_required':True,
             'validation_grid':[{'settings':item['settings'],'metrics':item['validation']} for item in candidates]}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    (output/'held-out-predictions.json').write_text(json.dumps(test_rows,ensure_ascii=False))
    (output/'validation-predictions.json').write_text(json.dumps(combined,ensure_ascii=False))
    as_of=date(2026,10,27)
    export=LearnedPoisson(**chosen_glm['settings']).fit(matches,as_of).export()
    export['aliases']=ALIASES
    export['benchmark']={'period':'2025/26','common_fixtures':len(common_test),'metrics':metrics['glm'],
                         'base_metrics':metrics['base'],'candidate_selected_glm_weight':chosen['glm_weight'],
                         'note':'This export is the learned Poisson component; ensemble results are reported separately.'}
    (output/'poisson-v2-experimental.json').write_text(json.dumps(export,ensure_ascii=False,indent=2))
    lines=['# Исторический эксперимент Footpredict', '',
           'Настройки выбраны на сезонах 2023/24 и 2024/25. Итоговый период этого запуска — 2025/26.',
           'Эти сезоны могли обсуждаться ранее; строгая следующая проверка — на новых будущих матчах.', '',
           f'Общие матчи итоговой проверки: {len(common_test)} из {len(test)}. Пропуски — недостаток прошлых домашних/выездных игр для v1, одинаково исключённый из основного сравнения всех моделей.', '',
           '| Модель | Матчи | Brier ↓ | Log Loss ↓ | Совпал исход |','|---|---:|---:|---:|---:|']
    for key,value in metrics.items():
        lines.append(f"| {key} | {value['n']} | {value['brier']:.4f} | {value['logloss']:.4f} | {value['accuracy']:.1%} |")
    lines+=['', '## Выбор до итоговой проверки', '', '```json',json.dumps(selection,indent=2),'```','',
            '## Парное сравнение с базой', '', 'Отрицательное отличие означает меньшую ошибку кандидата. Интервалы получены повторной выборкой календарных дней, seed зафиксирован.', '',
            '```json',json.dumps(comparison,indent=2),'```','',
            f'Исторический критерий преимущества выполнен: {strong_advantage}. Модель автоматически основной не становится.', '',
            '## Ограничения', '',
            '- Использованы только дата, команды и результаты более ранних матчей. Коэффициенты, удары, карточки и статистика прогнозируемой игры не используются.',
            '- Из-за неполной информации о времени публикации результатов исключены также матчи непосредственно предыдущего календарного дня. Такой срез консервативнее онлайн-модели.',
            '- Травмы, составы, новости и xG в этом эксперименте не использовались: исторических снимков этих признаков нет.',
            '- Точность прошлых сезонов не гарантирует будущие результаты; нужна отдельная перспективная серия с зафиксированными параметрами.',
            '- Источник: https://www.football-data.co.uk/englandm.php . CSV скачаны с TLS-проверкой; URL, SHA-256, объём и использованные столбцы сохранены в data-manifest.json.', '']
    (output/'report.md').write_text('\n'.join(lines))
    print('COMPLETE:',json.dumps({'output':str(output),'metrics':metrics,'historical_gate':strong_advantage}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--workers',type=int,choices=(1,2),default=2)
    args=parser.parse_args()
    execute(args.data_dir,args.output,args.workers)
