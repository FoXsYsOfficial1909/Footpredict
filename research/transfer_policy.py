"""Learn a bounded correction weight for clubs with little E1 and enough E0 history."""
import argparse
import hashlib
import json
from pathlib import Path

from research.championship import ROOT, dump
from research.evaluate import summarize, paired_bootstrap
from research.nightly import sha

PLAN = ROOT / 'docs/research/transfer-policy-plan-2026-11-14.json'
PROTOCOL = 'elc-transfer-policy-1'


def eligible(histories, rule):
    return any(counts['E1'] < rule['E1GamesExclusiveMaximum'] and counts['E0'] >= rule['E0GamesInclusiveMinimum']
               for counts in histories.values())


def predict(rows, weight, rule):
    result = []
    for row in rows:
        applies = eligible(row['pastClubGames'], rule)
        candidate = [(1-weight)*a+weight*b for a, b in zip(row['v1'], row['joint'])] if applies else list(row['v1'])
        changed = max(abs(a-b) for a, b in zip(candidate, row['v1'])) > 1e-12
        result.append({**row, 'candidate': candidate, 'policyApplicable': applies, 'policyChanged': changed})
    return result


def select(rows, plan):
    if (plan['protocol'] != PROTOCOL or plan['weights'] != [0., .25, .5, .75, 1.]
            or plan['maximumCandidates'] != 5 or plan['automaticPromotion'] is not False
            or plan['eligibility'] != {'E1GamesExclusiveMaximum':20,'E0GamesInclusiveMinimum':20,'windowDays':1095}):
        raise ValueError('Unexpected bounded policy or altered history threshold')
    trials = [{'weight': w, 'metrics': summarize(predict(rows, w, plan['eligibility']), 'candidate')} for w in plan['weights']]
    best = min(trials, key=lambda t: (t['metrics']['logloss'], t['weight']))
    return {'schemaVersion': 1, 'protocol': PROTOCOL, 'planSha256': sha(plan), 'weight': best['weight'],
        'eligibility': plan['eligibility'], 'trials': trials, 'validation': best['metrics'],
        'validationApplicableMatches': sum(eligible(r['pastClubGames'], plan['eligibility']) for r in rows),
        'selectionDataSha256': sha(rows), 'selectedBeforeLaterPolicyMetrics': True,
        'promotedToDefault': False, 'prospectiveValidationRequired': True}


def evaluate(periods, selection, plan):
    results, predictions = {}, {}
    for name, rows in periods.items():
        rows = predict(rows, selection['weight'], plan['eligibility']); predictions[name] = rows
        applied = [r for r in rows if r['policyApplicable']]
        changed = sum(r['policyChanged'] for r in rows)
        keys = ('candidate', 'v1', 'joint', 'shots', 'base')
        results[name] = {'matches': len(rows), 'applicableMatches': len(applied), 'changedMatches': changed,
            'metrics': {key: summarize(rows, key) for key in keys},
            'applicableMetrics': {key: summarize(applied, key) for key in keys},
            'pairedIntervals': paired_bootstrap(rows, 'candidate', 'v1', samples=plan['bootstrapSamples']),
            'adjustmentEvidence': 'no_applicable_fixtures' if not applied else 'retrospective_only'}
    non_regression = all(results[p]['metrics']['candidate'][key] <= results[p]['metrics']['v1'][key]+1e-12
                         for p in results for key in ('brier', 'logloss'))
    signal = non_regression and any(v['changedMatches'] for v in results.values()) and all(
        results[p]['metrics']['candidate'][key] < results[p]['metrics']['v1'][key]
        for p in results if results[p]['changedMatches'] for key in ('brier', 'logloss'))
    return results, predictions, signal


def execute(output):
    plan = json.loads(PLAN.read_text()); source = ROOT / plan['sourceStudy']
    if hashlib.sha256((source/'report.json').read_bytes()).hexdigest() != plan['sourceReportSha256']:
        raise ValueError('The pinned joint research changed')
    periods = {}
    for name, digest in plan['sourcePredictionSha256'].items():
        rows = json.loads((source/(name+'-predictions.json')).read_text())
        if sha(rows) != digest or len(rows) != plan['expectedMatches'][name]:raise ValueError('Changed source prediction cohort')
        periods[name] = rows
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    selection = select(periods['validation'], plan); dump(output/'selection.json', selection)
    results, predictions, signal = evaluate(periods, selection, plan)
    report = {'schemaVersion': 1, 'protocol': PROTOCOL, 'planSha256': sha(plan), 'selectionSha256': sha(selection),
        'selection': selection, 'results': results, 'researchSignal': signal, 'retrospective': True,
        'independentAdvantageEstablished': False, 'prospectiveValidationRequired': True, 'promotedToDefault': False,
        'sourcePredictionSha256': plan['sourcePredictionSha256'], 'limitations': plan['limitations']}
    dump(output/'report.json', report)
    for name, rows in predictions.items():
        fields = ('date','cutoff_date','home','away','actual','pastClubGames','candidate','v1','joint','shots','base','policyApplicable','policyChanged')
        (output/(name+'-predictions.json')).write_text(json.dumps([{k:r[k] for k in fields} for r in rows],ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n')
    lines = ['# Поправка при малой истории в новой лиге', '',
        f"Выбран вес совместной модели: {selection['weight']:.0%}. Ранняя выборка: "
        f"{selection['validation']['n']} матч, из них применимых {selection['validationApplicableMatches']}.", '',
        '| Период | Общих | Применимых | Изменено | Модель | Brier ↓ | Log Loss ↓ | Совпал исход |',
        '| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |']
    for period, block in results.items():
        for key in ('candidate', 'v1', 'joint', 'shots'):
            m = block['metrics'][key]
            lines.append(f"| {period} | {m['n']} | {block['applicableMatches']} | {block['changedMatches']} | "
                         f"{key} | {m['brier']:.6f} | {m['logloss']:.6f} | {m['accuracy']:.1%} |")
    lines += ['', f'Описательный исследовательский сигнал: {signal}. Независимое преимущество не установлено.', '',
        'В промежуточном сезоне нет применимых матчей: равенство с v1 не проверяет полезность поправки. '
        'Все периоды уже изучались, нужна новая регистрация до будущих матчей. Основная модель не переключается.', '']
    (output/'report.md').write_text('\n'.join(lines))
    print(json.dumps({'weight':selection['weight'],'researchSignal':signal,'results':{k:{'applicable':v['applicableMatches'],'changed':v['changedMatches'],'metrics':{m:v['metrics'][m] for m in ('candidate','v1')}} for k,v in results.items()}},ensure_ascii=False))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--output',type=Path,required=True)
    execute(parser.parse_args().output)
