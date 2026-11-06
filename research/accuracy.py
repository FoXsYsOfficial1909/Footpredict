"""Bounded accuracy experiment. Freeze selection before the retrospective audit."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import date,timedelta
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np

from research.data import ALIASES
from research.evaluate import reference_rows,glm_predictions,elo_predictions,with_predictions,summarize,paired_bootstrap
from research.performance import load_performance,ShotPoisson,feature_rows,FEATURES
from research.nightly import sha
from research.calibration import DixonColes,calibration_predictions

ROOT=Path(__file__).resolve().parents[1]
PLAN=ROOT/'docs/research/accuracy-plan-2026-11-05.json'
MODEL_FEATURES=FEATURES+['glm_log_home_draw','glm_log_away_draw']


def dump(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))


def prepare(observations,manifest,output):
    selected=json.loads((ROOT/'docs/research/selection.json').read_text())
    files=['research/performance.py','research/accuracy.py','research/models.py','research/evaluate.py','research/data.py']
    signature=sha({'sources':manifest,'referenceSettings':selected,'code':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in files}})
    path=output/'feature-cache.json'
    if path.exists():
        packet=json.loads(path.read_text())
        if packet['signature']==signature:return packet['rows']
    matches=[r.match for r in observations]
    rows=reference_rows(matches,set(range(2016,max(m.season for m in matches)+1)))
    print('Preparing past-only Poisson reference predictions:',len(rows),flush=True)
    glm=glm_predictions((matches,rows,selected['glm']))['predictions'];elo=elo_predictions(matches,rows,selected['elo'])
    rows=with_predictions(with_predictions(rows,glm,'glm'),elo,'elo')
    print('Preparing past-only shot/form feature rows.',flush=True)
    vectors={(r['date'],r['home'],r['away']):r['features'] for r in feature_rows(observations)}
    for r in rows:
        r['candidate']=(selected['glm_weight']*np.array(r['glm'])+(1-selected['glm_weight'])*np.array(r['elo'])).tolist()
        r['features']=vectors[(r['date'],r['home'],r['away'])]+[math.log(max(1e-12,r['glm'][0])/max(1e-12,r['glm'][1])),math.log(max(1e-12,r['glm'][2])/max(1e-12,r['glm'][1]))]
    dump(path,{'signature':signature,'rows':rows})
    return rows


def shot_predictions(observations,rows,settings):
    model=ShotPoisson(goal_weight=settings['goalWeight'],half_life=settings['halfLife'],ridge=settings['ridge'],window_days=settings['windowDays'])
    last=None;predictions=[]
    for r in rows:
        cutoff=date.fromisoformat(r['cutoff_date'])
        if cutoff!=last:model.fit(observations,cutoff);last=cutoff
        p=model.predict(r['home'],r['away'],date.fromisoformat(r['date']))['probabilities']
        predictions.append((settings['poissonWeight']*p+(1-settings['poissonWeight'])*np.array(r['elo'])).tolist())
    return predictions


def classifier_fit(examples,cutoff,settings):
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import HistGradientBoostingClassifier
    prior=[r for r in examples if 0<(cutoff-date.fromisoformat(r['date'])).days<=settings['windowDays']]
    if len(prior)<300:raise ValueError('Not enough earlier feature rows')
    x=np.array([r['features'] for r in prior]);y=np.array([r['actual'] for r in prior])
    weights=np.exp(-math.log(2)*np.array([(cutoff-date.fromisoformat(r['date'])).days for r in prior])/settings['halfLife'])
    if settings['kind']=='logistic':
        scaler=StandardScaler().fit(x,sample_weight=weights);x=scaler.transform(x)
        model=LogisticRegression(C=settings['C'],max_iter=1000,random_state=settings['randomState'])
    else:
        scaler=None
        model=HistGradientBoostingClassifier(max_iter=settings['iterations'],learning_rate=settings['learningRate'],max_leaf_nodes=settings['leaves'],
            min_samples_leaf=settings['minimumLeafSamples'],l2_regularization=settings['l2'],early_stopping=False,random_state=settings['randomState'])
    model.fit(x,y,sample_weight=weights)
    if model.classes_.tolist()!=[0,1,2]:raise ValueError('All three outcome classes are required')
    return model,scaler,{'trainingRows':len(prior),'latestTrainingMatch':prior[-1]['date'],'cutoffExclusive':cutoff.isoformat()}


def classifier_predictions(examples,rows,settings):
    predictions=[];month=None;model=scaler=None
    for r in rows:
        if r['date'][:7]!=month:
            model,scaler,_=classifier_fit(examples,date.fromisoformat(r['cutoff_date']),settings);month=r['date'][:7]
        x=np.array([r['features']]);x=scaler.transform(x) if scaler is not None else x
        predictions.append(model.predict_proba(x)[0].tolist())
    return predictions


def predict_task(args):
    observations,examples,rows,settings=args;started=time.monotonic()
    if settings['kind']=='shot_poisson':p=shot_predictions(observations,rows,settings)
    elif settings['kind']=='calibration':p=calibration_predictions(examples,rows,settings)
    elif settings['kind']=='dixon_coles':
        model=DixonColes(half_life=settings['halfLife'],ridge=settings['ridge'],window_days=settings['windowDays']);last=None;p=[]
        for r in rows:
            cutoff=date.fromisoformat(r['cutoff_date'])
            if cutoff!=last:model.fit([o.match for o in observations],cutoff);last=cutoff
            value=model.predict(r['home'],r['away'],date.fromisoformat(r['date']))['probabilities']
            p.append((settings['poissonWeight']*value+(1-settings['poissonWeight'])*np.array(r['elo'])).tolist())
    else:p=classifier_predictions(examples,rows,settings)
    common=[{**r,'challenger':pred} for r,pred in zip(rows,p) if r['v1'] is not None]
    return {'id':settings['id'],'settings':settings,'metrics':summarize(common,'challenger'),
        'bySeason':{str(year):summarize([r for r in common if r['season']==year],'challenger') for year in sorted({r['season'] for r in common})},
        'seconds':time.monotonic()-started,'predictions':p}


def execute(data_dir,output,phase='all',workers=2,plan_path=PLAN):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    plan=json.loads(Path(plan_path).read_text());dump(output/'protocol.json',plan)
    observations,manifest=load_performance(data_dir,plan['seasons']);dump(output/'data-manifest.json',manifest)
    examples=prepare(observations,manifest,output)
    if plan.get('shotContext'):
        print('Preparing rolling shot-model calibration context.',flush=True)
        p=shot_predictions(observations,examples,plan['shotContext'])
        for r,values in zip(examples,p):r['shots']=values
    validation=[r for r in examples if r['season'] in plan['selectionSeasons']]
    common=[r for r in validation if r['v1'] is not None];baseline=summarize(common,'candidate')
    print('Reference validation:',json.dumps(baseline),flush=True)
    if phase in ('all','selection'):
        results=[]
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for result in pool.map(predict_task,[(observations,examples,validation,s) for s in plan['models']]):
                print('VALIDATION',json.dumps({k:v for k,v in result.items() if k!='predictions'}),flush=True);results.append(result)
        eligible=[r for r in results if r['metrics']['accuracy']>=baseline['accuracy'] and r['metrics']['logloss']<baseline['logloss']]
        chosen=min(eligible,key=lambda r:r['metrics']['logloss']) if eligible else None
        selection={'protocol':plan['protocol'],'planSha256':sha(plan),'baseline':baseline,'chosen':{k:v for k,v in chosen.items() if k!='predictions'} if chosen else None,
            'validation':[{k:v for k,v in r.items() if k!='predictions'} for r in results],'auditInspectedBeforeChoice':False,'promotedToDefault':False}
        dump(output/'selection.json',selection)
        print('SELECTION_FROZEN',json.dumps(selection['chosen']),flush=True)
    if phase in ('all','audit'):
        selection=json.loads((output/'selection.json').read_text())
        if selection['planSha256']!=sha(plan):raise ValueError('The preregistered plan changed after selection')
        chosen=selection['chosen']
        if not chosen:
            print('NO_CHALLENGER: none improved validation log loss without reducing accuracy.',flush=True);return
        summary={'protocol':plan['protocol'],'selection':selection,'retrospectiveAudit':True,'promotedToDefault':False,'prospectiveValidationRequired':True}
        for label,seasons in [('audit',plan['auditSeasons']),('recentMonitoring',plan['recentMonitoringSeasons'])]:
            targets=[r for r in examples if r['season'] in seasons]
            result=predict_task((observations,examples,targets,chosen['settings']))
            rows=[{**r,'challenger':p} for r,p in zip(targets,result['predictions']) if r['v1'] is not None]
            metrics={key:summarize(rows,key) for key in ('challenger','candidate','v1','base')}
            ci=paired_bootstrap(rows,'challenger','candidate')
            summary[label]={'targets':len(targets),'commonMatches':len(rows),'metrics':metrics,'challengerMinusCurrent':ci,'bySeason':result['bySeason']}
            dump(output/(label+'-predictions.json'),rows)
            print(label.upper(),json.dumps(summary[label]),flush=True)
        a=summary['audit']['metrics'];summary['passesRetrospectiveGate']=a['challenger']['brier']<a['candidate']['brier'] and a['challenger']['logloss']<a['candidate']['logloss'] and a['challenger']['accuracy']>=a['candidate']['accuracy']
        dump(output/'summary.json',summary)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--data-dir',required=True,type=Path);parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--phase',choices=['all','selection','audit'],default='all');parser.add_argument('--workers',type=int,choices=[1,2],default=2)
    parser.add_argument('--plan',type=Path,default=PLAN)
    args=parser.parse_args();execute(args.data_dir,args.output,args.phase,args.workers,args.plan)
