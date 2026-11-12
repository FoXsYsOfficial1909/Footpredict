"""One prespecified correction to production v1, tested on later calendar periods."""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path

from research.calibration import fit_calibration,calibrated
from research.evaluate import summarize,paired_bootstrap
from research.nightly import sha

ROOT=Path(__file__).resolve().parents[1]
PLAN=ROOT/'docs/research/mixed-calibration-plan-2026-11-12.json'


def evaluate(replay,plan):
    if (replay.get('protocol')!='mixed-v1-replay-1' or replay['sourceCohortSha256']!=plan['inputCohortSha256']
        or plan['candidateCount']!=1 or plan['automaticPromotion'] is not False):raise ValueError('Unexpected experiment identity')
    rows=sorted(replay['rows'],key=lambda r:(r['kickoff'],r['league'],r['fixtureId']))
    cutoff=date.fromisoformat(plan['trainingCutoffExclusive'])
    model=fit_calibration(rows,cutoff,plan['candidate'])
    results={}
    for name,bounds in [('validation',plan['validationPeriod']),('audit',plan['auditPeriod'])]:
        selected=[{**r,'calibrated':calibrated(r['v1'],model)} for r in rows if bounds[0]<=r['date']<bounds[1]]
        if not selected:raise ValueError('Empty evaluation period: '+name)
        results[name]={'period':bounds,'matches':len(selected),
            'metrics':{key:summarize(selected,key) for key in ('v1','base','calibrated')},
            'pairedIntervals':paired_bootstrap(selected,'calibrated','v1',samples=1500),
            'byLeague':{code:{'matches':sum(r['league']==code for r in selected),
                'metrics':{key:summarize([r for r in selected if r['league']==code],key) for key in ('v1','base','calibrated')}} for code in plan['leagues']}}
    passed=all(results[period]['metrics']['calibrated'][key]<results[period]['metrics']['v1'][key]
        for period in ('validation','audit') for key in plan['primaryMetrics'])
    return {'schemaVersion':1,'protocol':plan['protocol'],'planSha256':sha(plan),'sourceCohortSha256':plan['inputCohortSha256'],
        'sourceMatches':sum(s['rows'] for s in replay['sourceManifest']),'replayedMatches':len(rows),'excludedMatches':len(replay['skipped']),
        'calibration':model,'settings':plan['candidate'],'results':results,'preliminaryGatePassed':passed,
        'promotedToDefault':False,'prospectiveValidationRequired':True,'retrospective':True,
        'limitations':plan['limitations']}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--replay',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    raw=args.replay.read_bytes();result=evaluate(json.loads(raw),json.loads(PLAN.read_text()))
    result['replaySha256']=hashlib.sha256(raw).hexdigest();args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    lines=['# Проверка исторических матчей и калибровки v1','',
        f"Исходных завершённых игр: {result['sourceMatches']}; воспроизведено прогнозов: {result['replayedMatches']}; исключено: {result['excludedMatches']}.",
        '',f"Обучение калибровки: {result['calibration']['trainingRows']} более ранних прогнозов. Три параметра, один заранее выбранный метод.",'',
        '| Период | Матчей | Модель | Brier ↓ | Log Loss ↓ | Совпал исход |','| --- | ---: | --- | ---: | ---: | ---: |']
    for period,value in result['results'].items():
        for name,metric in value['metrics'].items():
            lines.append(f"| {period} | {metric['n']} | {name} | {metric['brier']:.6f} | {metric['logloss']:.6f} | {metric['accuracy']:.1%} |")
    lines+=['',f"Предварительное условие улучшения двух метрик в обоих периодах: {result['preliminaryGatePassed']}.",'',
        'Ретроспективное исследование на текущих окончательных счетах. Это не доказательство будущей точности и не изменение исходной серии из 100 матчей.']
    (args.output/'report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'sourceMatches':result['sourceMatches'],'replayed':result['replayedMatches'],'trainingRows':result['calibration']['trainingRows'],
        'preliminaryGatePassed':result['preliminaryGatePassed'],'results':{name:value['metrics'] for name,value in result['results'].items()}},ensure_ascii=False))
