"""One prespecified small neural residual experiment, with chronological folds."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import time

import numpy as np

from research.league_one_experiment import load_inputs as load_corpus
from research.neural_features import FEATURE_NAMES, build_rows
from research.neural_residual import fit_model, predict_model
from research.nightly import sha
from research.quality_diagnostic import metrics, reliability, checked_probabilities

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT/'docs/research/neural-plan-2026-11-16.json'
DIRECTORY = ROOT/'docs/research/neural-2026-11-16'
PROTOCOL = 'neural-residual-1'
DIVISIONS = ('E0', 'E1', 'E2')
SETTINGS = {'hidden_units':16,'l2':.01,'max_iter':200,'seed':42,'feature_clip':5.,'hidden_bias_std':.05}
FEATURE_CODE = ('research/neural_features.py','research/performance.py','research/models.py',
                'research/evaluate.py','research/data.py')


def raw_sha(raw):
    return hashlib.sha256(raw).hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')


def code_sha():
    names=(*FEATURE_CODE,'research/neural_residual.py','research/neural_experiment.py','requirements-research.txt')
    return sha({name:raw_sha((ROOT/name).read_bytes()) for name in names})


def validate_plan(plan):
    if (plan.get('protocol')!=PROTOCOL or plan.get('divisions')!=list(DIVISIONS)
            or plan.get('expectedSourceMatches')!=14002 or plan.get('featureCount')!=56
            or plan.get('settings')!=SETTINGS or plan.get('neuralArchitectures')!=1
            or plan.get('blendWeights')!=[0.,.25,.5,.75,1.] or plan.get('maximumBlendCandidates')!=5
            or plan.get('trainingSeasons')!=list(range(2016,2023)) or plan.get('validationSeasons')!=[2023,2024]
            or plan.get('auditSeasons')!=[2025] or plan.get('monitoringSeasons')!=[2026]
            or plan.get('earlyFitCutoffExclusive')!='2023-07-01' or plan.get('refitCutoffExclusive')!='2025-07-01'
            or plan.get('trainingHalfLifeDays')!=730 or plan.get('bootstrapSamples')!=1500
            or plan.get('bootstrapSeed')!=20261116 or plan.get('automaticPromotion') is not False
            or plan.get('existingFrozenPredictionsMayChange') is not False):
        raise ValueError('Unexpected or expanded neural experiment')


def source_games(plan):
    validate_plan(plan)
    path=ROOT/plan['sourceSnapshot'];raw=path.read_bytes()
    if len(raw)>3_000_000 or raw_sha(raw)!=plan['sourceCompressedSha256']:
        raise ValueError('Pinned neural source changed')
    body=gzip.decompress(raw)
    if len(body)>10_000_000 or raw_sha(body)!=plan['sourceDataSha256']:
        raise ValueError('Neural source data checksum changed')
    prior_plan=json.loads((ROOT/'docs/research/league-one-plan-2026-11-15.json').read_text())
    games,_=load_corpus(prior_plan)
    if len(games)!=plan['expectedSourceMatches']:
        raise ValueError('Neural source cohort size changed')
    return games


def verify_rows(rows, games, plan):
    expected={(g.division,*g.observation.match.key):g.observation.match for g in games}
    seen=set()
    for row in rows:
        key=(row['division'],row['date'],row['home'],row['away'])
        if (key in seen or key not in expected or type(row['actual']) is not int or row['actual']!=expected[key].outcome
                or row['season']!=expected[key].season
                or row['cutoff_date']!=(date.fromisoformat(row['date'])-timedelta(days=1)).isoformat()
                or row['latest_input_match'] is not None and row['latest_input_match']>=row['cutoff_date']
                or len(row['features'])!=plan['featureCount'] or not np.all(np.isfinite(row['features']))
                or row['v1_available']!=(row['v1'] is not None)):
            raise ValueError('Neural feature identity or chronological provenance changed')
        for name in ('base','frequency'):checked_probabilities(row[name])
        if row['v1'] is not None:checked_probabilities(row['v1'])
        if row['base']!=(row['v1'] if row['v1_available'] else row['frequency']):
            raise ValueError('Neural baseline fallback changed')
        seen.add(key)
    if seen!=set(expected):raise ValueError('Neural features cannot drop any source match')


def feature_rows(games, plan, cache=None):
    hashes={name:raw_sha((ROOT/name).read_bytes()) for name in FEATURE_CODE}
    if cache is not None and Path(cache).exists():
        cache=Path(cache);manifest=json.loads(cache.with_name('feature-rows-manifest.json').read_text())
        raw=cache.read_bytes()
        if (manifest.get('protocol')!='neural-features-1' or manifest.get('codeHashes')!=hashes
                or manifest.get('featureNames')!=list(FEATURE_NAMES)
                or raw_sha(raw)!=manifest.get('compressedSha256')):
            raise ValueError('Cached neural features or generating code changed')
        rows=json.loads(gzip.decompress(raw))
        if sha(rows)!=manifest.get('dataSha256'):raise ValueError('Cached neural feature checksum changed')
    else:
        rows=build_rows(games)
    verify_rows(rows,games,plan)
    return rows


def split_rows(rows,plan):
    parts={name:[r for r in rows if r['season'] in plan[key]] for name,key in
           [('context','contextSeasons'),('training','trainingSeasons'),('validation','validationSeasons'),
            ('audit','auditSeasons'),('monitoring','monitoringSeasons')]}
    if {k:len(v) for k,v in parts.items()}!=plan['expectedMatches']:
        raise ValueError('Neural temporal partition counts changed')
    return parts


def training_weights(rows,cutoff,half_life=730):
    cutoff=date.fromisoformat(cutoff) if isinstance(cutoff,str) else cutoff
    ages=np.array([(cutoff-date.fromisoformat(r['date'])).days for r in rows])
    if len(rows)<1 or np.any(ages<=0):raise ValueError('Neural training labels cross the fold cutoff')
    values=np.exp(-np.log(2)*ages/half_life)
    for division in DIVISIONS:
        indices=np.array([r['division']==division for r in rows])
        if not indices.any():raise ValueError('Every training division needs past rows')
        values[indices]/=3*values[indices].sum()
    return values


def train(rows,cutoff,plan):
    weights=training_weights(rows,cutoff,plan['trainingHalfLifeDays'])
    model=fit_model([r['features'] for r in rows],[r['base'] for r in rows],
                    [r['actual'] for r in rows],settings=plan['settings'],sample_weights=weights)
    model['provenance']={'cutoffExclusive':cutoff,'latestTrainingMatch':max(r['date'] for r in rows),
        'trainingRowsSha256':sha(rows),'trainingWeightsSha256':sha(weights.tolist()),
        'divisionRows':{d:sum(r['division']==d for r in rows) for d in DIVISIONS},
        'divisionWeightMass':{d:float(sum(w for r,w in zip(rows,weights) if r['division']==d)) for d in DIVISIONS}}
    return model


def predict(rows,model,weight):
    if not rows:return []
    neural=predict_model(model,[r['features'] for r in rows],[r['base'] for r in rows])
    result=[]
    for row,p in zip(rows,neural):
        selected=list(row['base']) if weight==0 else [(1-weight)*a+weight*b for a,b in zip(row['base'],p)]
        result.append({**row,'neural':p,'selected':selected})
    return result


def macro(rows,key):
    values={d:metrics([r for r in rows if r['division']==d],key) for d in DIVISIONS}
    if any(v['n']==0 for v in values.values()):return {'logLoss':None,'brier':None,'uniqueTopAccuracy':None}
    return {name:sum(v[name] for v in values.values())/3 if all(v[name] is not None for v in values.values()) else None
            for name in ('logLoss','brier','uniqueTopAccuracy')}


def select(rows,model,plan):
    trials=[]
    full=predict(rows,model,1.)
    for weight in plan['blendWeights']:
        blended=[{**r,'selected':list(r['base']) if weight==0 else
                  [(1-weight)*a+weight*b for a,b in zip(r['base'],r['neural'])]} for r in full]
        trials.append({'weight':weight,'macro':macro(blended,'selected')})
    best=min(trials,key=lambda t:(t['macro']['logLoss'],t['weight']))
    return {'schemaVersion':1,'protocol':PROTOCOL,'planSha256':sha(plan),'weight':best['weight'],
        'trials':trials,'validationRowsSha256':sha(rows),'earlyModelSha256':sha(model),
        'chosenBeforeLaterMetrics':True,'promotedToDefault':False,'prospectiveValidationRequired':True}


def macro_interval(rows,candidate='selected',reference='base',*,samples=1500,seed=20261116):
    days=sorted({r['date'] for r in rows});losses=np.zeros((len(days),3));counts=np.zeros((len(days),3))
    index={day:i for i,day in enumerate(days)}
    for r in rows:
        i,j=index[r['date']],DIVISIONS.index(r['division']);y=r['actual']
        losses[i,j]+=-np.log(max(r[candidate][y],1e-12))+np.log(max(r[reference][y],1e-12));counts[i,j]+=1
    if not days or np.any(counts.sum(axis=0)==0):return {'difference':None,'ci95':None,'validSamples':0,'samples':samples}
    draws=np.random.default_rng(seed).integers(0,len(days),size=(samples,len(days)))
    totals=counts[draws].sum(axis=1);numerators=losses[draws].sum(axis=1)
    valid=np.all(totals>0,axis=1)
    values=np.mean(numerators[valid]/totals[valid],axis=1)
    return {'difference':float(np.mean(losses.sum(axis=0)/counts.sum(axis=0))),
        'ci95':np.quantile(values,[.025,.975]).tolist() if valid.all() else None,
        'samples':samples,'validSamples':int(valid.sum()),'invalidSamples':int((~valid).sum()),'seed':seed,
        'method':'paired union UTC-day clusters; three division means recomputed within each draw'}


def evaluate(rows,plan):
    keys=('selected','neural','base','frequency')
    common=[r for r in rows if r['v1_available']]
    return {'matches':len(rows),'from':min(r['date'] for r in rows),'to':max(r['date'] for r in rows),
        'v1Available':len(common),'frequencyFallback':len(rows)-len(common),
        'pooled':{k:metrics(rows,k) for k in keys},'macro':{k:macro(rows,k) for k in keys},
        'byDivision':{d:{'matches':sum(r['division']==d for r in rows),
            'fallback':sum(r['division']==d and not r['v1_available'] for r in rows),
            'metrics':{k:metrics([r for r in rows if r['division']==d],k) for k in keys},
            'calibration':{k:reliability([r for r in rows if r['division']==d],k) for k in keys}}
            for d in DIVISIONS},
        'v1Common':{'matches':len(common),'metrics':{k:metrics(common,k) for k in (*keys,'v1')}},
        'calibration':{k:reliability(rows,k) for k in keys},
        'macroLogLossInterval':macro_interval(rows,samples=plan['bootstrapSamples'],seed=plan['bootstrapSeed'])}


def gate(results,selection):
    if not selection['weight']:return False
    point=all(results[p]['macro']['selected'][m]<results[p]['macro']['base'][m]
              for p in ('audit','monitoring') for m in ('logLoss','brier'))
    divisions=all(results[p]['byDivision'][d]['metrics']['selected']['logLoss']<=
                  results[p]['byDivision'][d]['metrics']['base']['logLoss']
                  for p in ('audit','monitoring') for d in DIVISIONS)
    interval=results['audit']['macroLogLossInterval']['ci95']
    return bool(point and divisions and interval is not None and interval[1]<0)


def markdown(report):
    lines=['# Первая небольшая нейросеть Footpredict','',
        'Одна сеть с 16 tanh-нейронами и 963 параметрами дополняет Пуассон. '
        'Все признаки получены до матча. История уже изучалась; независимой проверки ещё нет.','',
        f"Выбран вес нейросети: {report['selection']['weight']:.0%}. "
        f"Исторический критерий: {report['historicalGatePassed']}.",'',
        '| Период | Игр | Модель | Macro Log Loss ↓ | Macro Brier ↓ | Попадания (все игры) |',
        '| --- | ---: | --- | ---: | ---: | ---: |']
    for label,block in report['results'].items():
        for method in ('selected','neural','base','frequency'):
            a=block['pooled'][method]['uniqueTopAccuracy'];shown='—' if a is None else f'{a:.2%}'
            lines.append(f"| {label} | {block['matches']} | {method} | {block['macro'][method]['logLoss']:.9f} | "
                         f"{block['macro'][method]['brier']:.9f} | {shown} |")
    lines+=['','Macro — среднее трёх отдельных лиг с равным весом. base — v1 при доступной истории, '
        'иначе прошлые частоты лиги; это не скрытый пропуск матча. Доля попаданий исключает только равные максимумы.','',
        'Обучение и остановка оптимизатора:','']
    for name,info in report['fits'].items():
        lines.append(f"- {name}: {info['trainingRows']} примеров, {info['iterations']} итераций; "
                     f"сошёлся: {info['converged']}, причина остановки: {info['stopReason']}.")
    lines+=['','Основная модель и все прежние будущие пакеты не изменяются. '
        'Исторические метрики оценивают зафиксированную процедуру; новые финальные веса требуют будущих исходов.','',
        '```bash','OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.neural_experiment \\',
        '  --output /tmp/footpredict-neural','```','']
    return '\n'.join(lines)


def execute(output=DIRECTORY,*,cache=None):
    output=Path(output).resolve();research=ROOT/'docs/research'
    if (output==research or research in output.parents) and output!=DIRECTORY.resolve():
        raise ValueError('Neural experiment cannot overwrite another frozen study')
    plan=json.loads(PLAN.read_text());validate_plan(plan);started=time.monotonic()
    games=source_games(plan);rows=feature_rows(games,plan,cache);parts=split_rows(rows,plan)
    output.mkdir(parents=True,exist_ok=True)
    early=train(parts['training'],plan['earlyFitCutoffExclusive'],plan)
    selection=select(parts['validation'],early,plan)
    dump(output/'selection.json',selection)
    dump(output/'early-model.json',early)
    refit=train(parts['training']+parts['validation'],plan['refitCutoffExclusive'],plan)
    dump(output/'refit-model.json',refit)
    predictions={'validation':predict(parts['validation'],early,selection['weight'])}
    for label in ('audit','monitoring'):predictions[label]=predict(parts[label],refit,selection['weight'])
    results={label:evaluate(values,plan) for label,values in predictions.items()}
    qualified=gate(results,selection);models={'early':early,'refit':refit}
    if selection['weight'] and qualified:
        cutoff=(date.fromisoformat(plan['asOf'])-timedelta(days=1)).isoformat()
        final_rows=[r for r in rows if r['season']>=2016 and r['date']<cutoff]
        final=train(final_rows,cutoff,plan);models['final']=final;dump(output/'final-model.json',final)
    report={'schemaVersion':1,'protocol':PROTOCOL,'asOf':plan['asOf'],'planSha256':sha(plan),
        'codeSha256':code_sha(),'sourceDataSha256':plan['sourceDataSha256'],'featureRowsSha256':sha(rows),
        'featureNames':list(FEATURE_NAMES),'sourceMatches':len(rows),'partitionCounts':{k:len(v) for k,v in parts.items()},
        'selection':selection,'selectionSha256':sha(selection),'results':results,
        'fits':{k:v['fit'] for k,v in models.items()},'modelSha256':{k:sha(v) for k,v in models.items()},
        'predictionSha256':{k:sha(v) for k,v in predictions.items()},'historicalGatePassed':qualified,
        'durationSeconds':time.monotonic()-started,'retrospective':True,'independentAdvantageEstablished':False,
        'promotedToDefault':False,'existingFrozenPredictionsChanged':False,'limitations':plan['limitations']}
    body=json.dumps(rows,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode();compressed=gzip.compress(body,mtime=0)
    (output/'feature-rows.json.gz').write_bytes(compressed)
    dump(output/'feature-rows-manifest.json',{'protocol':'neural-features-1','featureNames':list(FEATURE_NAMES),
        'sourceDataSha256':plan['sourceDataSha256'],'dataSha256':sha(rows),'compressedSha256':raw_sha(compressed),
        'codeHashes':{name:raw_sha((ROOT/name).read_bytes()) for name in FEATURE_CODE}})
    for label,values in predictions.items():
        (output/(label+'-predictions.json')).write_text(json.dumps(values,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n')
    dump(output/'report.json',report);(output/'report.md').write_text(markdown(report))
    print(json.dumps({'weight':selection['weight'],'historicalGatePassed':qualified,
        'macro':{k:v['macro'] for k,v in results.items()},'seconds':report['durationSeconds']}))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=DIRECTORY)
    parser.add_argument('--feature-cache',type=Path)
    args=parser.parse_args();execute(args.output,cache=args.feature_cache)
