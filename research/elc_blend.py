"""Choose one v1/shot mixture on earlier predictions, then inspect later periods."""
import argparse
import hashlib
import json
from pathlib import Path

from research.evaluate import summarize, paired_bootstrap
from research.nightly import sha

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'docs/research/elc-blend-plan-2026-11-14.json'
PROTOCOL = 'elc-blend-selection-1'


def blend(rows, weight):
    return [{**row, 'blend': [(1-weight)*a+weight*b for a, b in zip(row['v1'], row['shots'])]} for row in rows]


def choose(rows, plan):
    weights = plan['shotWeights']
    if (plan['protocol'] != PROTOCOL or len(weights) != plan['maximumCandidates']
            or weights != [0., .25, .5, .75, 1.] or plan['automaticPromotion'] is not False):
        raise ValueError('Unexpected mixture protocol or search grid')
    trials = [{'shotWeight': w, 'metrics': summarize(blend(rows, w), 'blend')} for w in weights]
    best = min(trials, key=lambda trial: (trial['metrics']['logloss'], trial['shotWeight']))
    return {'schemaVersion': 1, 'protocol': PROTOCOL, 'planSha256': sha(plan),
        'shotWeight': best['shotWeight'], 'validation': best['metrics'], 'trials': trials,
        'selectionDataSha256': sha(rows), 'selectedBeforeLaterMetrics': True, 'promotedToDefault': False}


def later_metrics(periods, selection, plan):
    results, predictions = {}, {}
    for name, rows in periods.items():
        chosen = blend(rows, selection['shotWeight'])
        predictions[name] = chosen
        results[name] = {'matches': len(chosen),
            'metrics': {key: summarize(chosen, key) for key in ('blend', 'v1', 'shots', 'base')},
            'pairedIntervals': paired_bootstrap(chosen, 'blend', 'v1', samples=plan['bootstrapSamples'])}
    point_gate = all(results[p]['metrics']['blend'][key] < results[p]['metrics']['v1'][key]
                     for p in results for key in ('brier', 'logloss'))
    historical_gate = point_gate and all(results['audit']['pairedIntervals'][key]['ci95'][1] < 0
                                        for key in ('brier', 'logloss'))
    return results, predictions, point_gate, historical_gate


def execute(output):
    plan = json.loads(PLAN.read_text()); study = ROOT / plan['sourceStudy']
    if hashlib.sha256((study/'report.json').read_bytes()).hexdigest() != plan['sourceReportSha256']:
        raise ValueError('The pinned shot study changed')
    periods = {}
    for name, checksum in plan['sourcePredictionSha256'].items():
        rows = json.loads((study/(name+'-predictions.json')).read_text())
        if (sha(rows) != checksum or len(rows) != plan['expectedMatches'][name]
                or len({(r['date'], r['home'], r['away']) for r in rows}) != len(rows)):
            raise ValueError('The pinned forecast cohort changed')
        periods[name] = rows
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    selection = choose(periods['validation'], plan)
    (output/'selection.json').write_text(json.dumps(selection, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    results, predictions, point_gate, historical_gate = later_metrics(periods, selection, plan)
    report = {'schemaVersion': 1, 'protocol': PROTOCOL, 'planSha256': sha(plan),
        'selectionSha256': sha(selection), 'selection': selection, 'results': results,
        'sourcePredictionSha256': plan['sourcePredictionSha256'],
        'pointGatePassed': point_gate, 'historicalGatePassed': historical_gate,
        'retrospective': True, 'prospectiveValidationRequired': True, 'promotedToDefault': False,
        'limitations': plan['limitations']}
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    lines = ['# Смесь v1 и модели с прошлыми ударами', '',
        f"Выбран вес ударов {selection['shotWeight']:.0%} на {selection['validation']['n']} ранних прогнозах. "
        'Поздние результаты в выборе веса не участвуют.', '',
        '| Период | Матчей | Модель | Brier ↓ | Log Loss ↓ | Совпал исход |',
        '| --- | ---: | --- | ---: | ---: | ---: |']
    for period, block in results.items():
        for name, metrics in block['metrics'].items():
            lines.append(f"| {period} | {metrics['n']} | {name} | {metrics['brier']:.6f} | "
                         f"{metrics['logloss']:.6f} | {metrics['accuracy']:.1%} |")
    lines += ['', f'Критерий средних ошибок: {point_gate}. Критерий исторического преимущества: {historical_gate}.', '',
        'Это дальнейшая разработка на уже изученных периодах. Исторический результат не заменяет регистрацию '
        'новых прогнозов до матча; основная модель не переключается.', '']
    (output/'report.md').write_text('\n'.join(lines))
    print(json.dumps({'selection': selection, 'pointGatePassed': point_gate,
        'historicalGatePassed': historical_gate, 'results': {k: v['metrics'] for k, v in results.items()}}, ensure_ascii=False))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    execute(parser.parse_args().output)
