"""Refresh a candidate with frozen settings; never deploy or reselect on results."""
from __future__ import annotations

import argparse
from datetime import date,datetime,timedelta,timezone
import hashlib
import gzip
import json
from pathlib import Path
import time

import numpy as np

from research.data import ALIASES,download_season
from research.evaluate import reference_rows,glm_predictions,elo_predictions,with_predictions,summarize
from research.export_model import export_elo
from research.models import LearnedPoisson

ROOT=Path(__file__).resolve().parents[1]
PROTOCOL='nightly-fixed-settings-1'


def sha(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def code_revision():
    files=['research/nightly.py','research/models.py','research/evaluate.py','research/export_model.py','research/data.py','requirements-research.txt']
    return sha({name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files})


def build_candidate(matches,selected,as_of,*,manifests=(),evaluation_limit=120,min_common=60):
    started=time.monotonic()
    cutoff=as_of-timedelta(days=1)
    available=sorted((m for m in matches if m.day<cutoff),key=lambda m:m.key)
    if len(available)<200:
        raise ValueError('At least 200 past matches are required')
    if len({m.key for m in available})!=len(available):
        raise ValueError('Duplicate training fixtures')
    # Include the whole first calendar day instead of selecting matches within it.
    first=available[max(0,len(available)-evaluation_limit)].day
    seasons={m.season for m in available if m.day>=first}
    rows=[row for row in reference_rows(available,seasons) if date.fromisoformat(row['date'])>=first]
    poisson=glm_predictions((available,rows,selected['glm']))
    elo=elo_predictions(available,rows,selected['elo'])
    rows=with_predictions(with_predictions(rows,poisson['predictions'],'glm'),elo,'elo')
    for row in rows:
        row['candidate']=(selected['glm_weight']*np.array(row['glm'])+(1-selected['glm_weight'])*np.array(row['elo'])).tolist()
    common=[row for row in rows if row['v1'] is not None]
    if len(common)<min_common:
        raise ValueError('Too few common chronological evaluation fixtures')
    metrics={key:summarize(common,key) for key in ['candidate','v1','base']}
    final=LearnedPoisson(**selected['glm']).fit(available,cutoff)
    model={'status':'candidate','as_of':as_of.isoformat(),'training_cutoff_exclusive':cutoff.isoformat(),
        'glm_weight':selected['glm_weight'],'glm':final.export(),'elo':export_elo(available,cutoff,selected['elo']),
        'aliases':ALIASES,'features_used':['past_match_results','opponent_strength','home_advantage'],
        'news_used_in_probabilities':False,'promoted_to_default':False,'protocol':PROTOCOL}
    model['model_version']='ensemble-candidate-'+as_of.isoformat()+'-'+sha(model)[:12]
    data_hash=sha([(m.day.isoformat(),m.season,m.home,m.away,m.home_goals,m.away_goals) for m in available])
    report={'schemaVersion':1,'protocol':PROTOCOL,'status':'success','asOf':as_of.isoformat(),
        'trainingCutoffExclusive':cutoff.isoformat(),'latestTrainingMatch':available[-1].day.isoformat(),
        'trainingMatches':len(available),'poissonTrainingMatches':final.fit_info['training_games'],
        'modelVersion':model['model_version'],'modelSha256':sha(model),'dataSha256':data_hash,
        'settingsSha256':sha(selected),'codeSha256':code_revision(),'settings':selected,
        'evaluation':{'kind':'rolling_historical_procedure','requestedMatches':evaluation_limit,'targets':len(rows),
            'commonMatches':len(common),'excludedFromAll':len(rows)-len(common),
            'from':rows[0]['date'],'to':rows[-1]['date'],'metrics':metrics,'poissonFitSeconds':poisson['seconds']},
        'dataSources':list(manifests),'durationSeconds':time.monotonic()-started,
        'promotedToDefault':False,'prospectiveValidationRequired':True,
        'limitations':['Settings are fixed from the persisted validation selection; this run performs no grid search.',
            'Historical monitoring evaluates rolling fits, not the newly fitted final weights.',
            'These dates may have been inspected before. Historical metrics are not an untouched or prospective test.',
            'Same-day and immediately preceding-day results are excluded because CSV publication times are unknown.',
            'News, injuries and lineups are not numeric predictors in this candidate.']}
    return model,report,rows


def markdown(report):
    evaluation=report['evaluation']
    lines=['# Ночное обучение кандидата','',
        f"Версия: `{report['modelVersion']}`. Срез: {report['asOf']}. Последний использованный матч: {report['latestTrainingMatch']}.",
        f"История: {report['trainingMatches']} игр; окно модели Пуассона: {report['poissonTrainingMatches']}.",'',
        f"Историческая процедура: {evaluation['commonMatches']} общих матчей ({evaluation['from']} — {evaluation['to']}).",'',
        '| Модель | Brier ↓ | Log Loss ↓ | Совпал исход |','|---|---:|---:|---:|']
    for name in ['candidate','v1','base']:
        m=evaluation['metrics'][name]
        lines.append(f"| {name} | {m['brier']:.4f} | {m['logloss']:.4f} | {m['accuracy']:.1%} |")
    lines+=['','Это мониторинг исторической процедуры. Он не оценивает новые финальные веса на будущих матчах.',
        'Кандидат не опубликован как основная модель; действующие прогнозы и совместная серия сохраняют прежние версии.',
        '',f"Время цикла: {report['durationSeconds']:.1f} с. Повторного подбора параметров нет.",'']
    return '\n'.join(lines)


def run(data_dir,output,as_of,*,offline=False):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    for name in ['model.json','model.mjs','report.json','report.md','evaluation.json','training-data.json.gz','attempt.json']:
        path=output/name
        if path.exists():path.unlink()
    started=time.monotonic()
    generated=datetime.now(timezone.utc).isoformat()
    try:
        season=as_of.year if as_of.month>=7 else as_of.year-1
        matches=[];manifests=[]
        for year in range(2015,season+1):
            if offline and not (Path(data_dir)/(str(year)+'-E0.csv')).exists():
                raise ValueError('Offline data is incomplete')
            rows,manifest=download_season(year,data_dir,refresh=year==season and not offline)
            if year<season and len(rows)!=380:
                if offline:raise ValueError('A completed season is incomplete')
                rows,manifest=download_season(year,data_dir,refresh=True)
                if len(rows)!=380:raise ValueError('A completed season is incomplete')
            matches.extend(rows);manifests.append(manifest)
        selected=json.loads((ROOT/'docs/research/selection.json').read_text())
        model,report,predictions=build_candidate(matches,selected,as_of,manifests=manifests)
        report['computeSeconds']=report['durationSeconds'];report['durationSeconds']=time.monotonic()-started
        report['generatedAt']=generated
        (output/'model.json').write_text(json.dumps(model,ensure_ascii=False,indent=2,allow_nan=False))
        (output/'model.mjs').write_text('export default '+json.dumps(model,ensure_ascii=False,separators=(',',':'),allow_nan=False)+';\n')
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
        (output/'evaluation.json').write_text(json.dumps(predictions,ensure_ascii=False,allow_nan=False))
        cutoff=as_of-timedelta(days=1)
        with gzip.open(output/'training-data.json.gz','wt',encoding='utf-8') as target:
            json.dump([{'date':m.day.isoformat(),'season':m.season,'home':m.home,'away':m.away,'homeGoals':m.home_goals,'awayGoals':m.away_goals}
                for m in matches if m.day<cutoff],target,ensure_ascii=False)
        (output/'report.md').write_text(markdown(report))
        attempt={k:report[k] for k in ['schemaVersion','protocol','status','asOf','generatedAt','trainingCutoffExclusive','latestTrainingMatch',
            'trainingMatches','poissonTrainingMatches','modelVersion','modelSha256','dataSha256','settingsSha256','codeSha256','evaluation','durationSeconds',
            'promotedToDefault','prospectiveValidationRequired','computeSeconds']}
        print(json.dumps({'modelVersion':model['model_version'],'trainingMatches':report['trainingMatches'],
            'commonMatches':report['evaluation']['commonMatches'],'seconds':report['durationSeconds']}),flush=True)
    except Exception as error:
        attempt={'schemaVersion':1,'protocol':PROTOCOL,'status':'failed','asOf':as_of.isoformat(),'generatedAt':generated,
            'errorType':type(error).__name__,'message':'The candidate was not completed. See the workflow step that failed.',
            'promotedToDefault':False}
        raise
    finally:
        (output/'attempt.json').write_text(json.dumps(attempt,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--as-of',type=date.fromisoformat,default=datetime.now(timezone.utc).date())
    parser.add_argument('--offline',action='store_true')
    args=parser.parse_args()
    run(args.data_dir,args.output,args.as_of,offline=args.offline)
