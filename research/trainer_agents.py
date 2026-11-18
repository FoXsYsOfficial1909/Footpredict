"""Bounded data, neural-training, examination and prospective-observation agents.

These are ordinary scheduled Python jobs, not LLM calls or self-modifying code.
They fit fixed settings, preserve first forecasts, and never deploy a public model.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import time

import numpy as np

from research.data import ALIASES, DIVISIONS, download_season
from research.division_model import DivisionGame
from research.evaluate import base_distribution
from research.monitor import API, fetch
from research.models import v1_statistics
from research.neural_experiment import (PLAN, ROOT, DIVISIONS as ORDER, FEATURE_CODE, source_games,
    train, predict, evaluate, validate_plan)
from research.neural_feature_cache import prepare_rows,implementation_sha as cache_implementation_sha
from research.neural_features import FEATURE_NAMES, _coverage
from research.neural_residual import predict_model
from research.nightly import sha
from research.performance import parse_performance, feature_state, vector
from research.quality_diagnostic import metrics

PROTOCOL='trainer-agents-1'
BRANCH='trainer-agent-reports'
POLICY={'maximumFitsPerCycle':2,'maximumIterationsPerFit':200,'maximumNewForecastsPerCycle':5,
        'minimumLeadHours':24,'maximumLeadDays':7,'resultsSafetyHours':4,'automaticPromotion':False,
        'paidAIRequests':0,'dataRefreshesPerWarmCycle':3,'monitoringTargetsPerDivision':120}


def dump(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')


def instant(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('Trainer timestamps need explicit UTC provenance')
    return result


def validate_state(state):
    if (state.get('schemaVersion')!=1 or state.get('protocol')!=PROTOCOL
            or not isinstance(state.get('forecasts'),dict) or not isinstance(state.get('observations'),dict)
            or len(state['forecasts'])>5000):
        raise ValueError('Invalid trainer state')
    for key,f in state['forecasts'].items():
        if (type(f.get('fixtureId')) is not int or f['fixtureId']<=0 or key!=str(f.get('fixtureId')) or f.get('protocol')!='neural-agent-future-1'
                or instant(f['kickoff'])-instant(f['registeredAt'])<timedelta(hours=24)
                or f.get('promotedToDefault') is not False):
            raise ValueError('Invalid immutable neural forecast')
        for method in ('candidate','base','frequency'):
            p=f[method]
            if len(p)!=3 or any(type(v) not in (int,float) or not np.isfinite(v) or not 0<=v<=1 for v in p) or abs(sum(p)-1)>1e-8:
                raise ValueError('Invalid saved neural probability vector')
    if not set(state['observations']).issubset(state['forecasts']):raise ValueError('An observation has no frozen forecast')
    for observed in state['observations'].values():
        if observed.get('status') not in ('settled','review','unavailable'):
            raise ValueError('Invalid neural observation state')
        instant(observed['checkedAt'])
        if observed['status']=='settled':
            score=observed.get('score')
            if (not isinstance(score,dict) or any(type(score.get(k)) is not int or not 0<=score[k]<=30 for k in ('home','away'))
                    or type(observed.get('actual')) is not int
                    or observed['actual']!=(0 if score['home']>score['away'] else 1 if score['home']==score['away'] else 2)):
                raise ValueError('Invalid neural settled score or outcome')
    return state


def new_state():
    return {'schemaVersion':1,'protocol':PROTOCOL,'forecasts':{},'observations':{},'training':None}


def load_state(directory):
    path=Path(directory)/'state.json'
    return validate_state(json.loads(path.read_text())) if path.exists() else new_state()


def implementation_sha():
    names=(*FEATURE_CODE,'research/neural_residual.py','research/neural_experiment.py',
           'research/trainer_agents.py','research/neural_feature_cache.py','requirements-research.txt')
    return sha({name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names})


def load_live_sources(directory,as_of):
    """Refresh three current CSVs; closed seasons remain cached and validated."""
    year=as_of.year if as_of.month>=7 else as_of.year-1
    cutoff=as_of-timedelta(days=1);games=[];sources=[];membership={};seen=set()
    for division in ORDER:
        for season in range(2020 if division=='E2' else 2015,year+1):
            rows,source=download_season(season,directory,refresh=season==year,division=division)
            if season<year and len(rows)!=DIVISIONS[division]['closed_season_matches']:
                rows,source=download_season(season,directory,refresh=True,division=division)
                if len(rows)!=DIVISIONS[division]['closed_season_matches']:
                    raise ValueError('A closed trainer source season is incomplete')
            raw=(Path(directory)/f'{season}-{division}.csv').read_bytes()
            if hashlib.sha256(raw).hexdigest()!=source['sha256']:raise ValueError('Trainer source changed during read')
            observations=parse_performance(raw,season,division=division)
            for o in observations:
                m=o.match
                if m.day>=cutoff:continue
                key=(division,*m.key)
                if key in seen:raise ValueError('Duplicate trainer input fixture')
                seen.add(key)
                for team in (m.home,m.away):
                    identity=(season,team)
                    if identity in membership and membership[identity]!=division:
                        raise ValueError('Trainer club belongs to conflicting divisions')
                    membership[identity]=division
                games.append(DivisionGame(division,o))
            sources.append(source)
    return sorted(games,key=lambda g:(*g.observation.match.key,g.division)),sources


def normalize(games):
    return [[g.division,o.match.day.isoformat(),o.match.season,o.match.home,o.match.away,
             o.match.home_goals,o.match.away_goals,o.home_shots,o.away_shots,o.home_sot,o.away_sot,list(o.issues)]
            for g in games for o in [g.observation]]


def forecast_features(games,fixture,cutoff):
    """Features available at actual registration, not at a future kickoff date."""
    home,away=(ALIASES.get(fixture[k],fixture[k]) for k in ('home','away'))
    observations=[g.observation for g in games if g.division=='E0']
    matches=[o.match for o in observations]
    state=feature_state(observations,cutoff);target=instant(fixture['kickoff']).date()
    if any(t not in state['histories'] for t in (home,away)):raise ValueError('Unknown prospective club')
    season=target.year if target.month>=7 else target.year-1
    frequency=base_distribution(matches,cutoff,season)
    old=v1_statistics(matches,cutoff,season,home,away)
    base=old['probabilities'].tolist() if old is not None else frequency
    values=list(vector(state,home,away,target))+_coverage(state,home,away)
    values += [1.,0.,0.]+[float(np.log(max(v,1e-12))) for v in base]+[float(old is not None)]
    if len(values)!=len(FEATURE_NAMES):raise ValueError('Prospective neural feature schema changed')
    return {'features':values,'base':base,'frequency':frequency,'v1_available':old is not None,
        'homeCsv':home,'awayCsv':away,'latestInputMatch':state['latestMatch'],'inputCutoffExclusive':cutoff.isoformat()}


def observe_and_register(state,model,model_sha,games,quality,now,*,registration_clock=None):
    source=quality.get('nightly')
    if (quality.get('schemaVersion')!=1 or not isinstance(source,dict) or source.get('protocol')!='nightly-live-1'
            or source['policy']['minimumHorizonHours']!=24 or source['policy']['automaticPromotion'] is not False
            or abs((instant(quality['generatedAt'])-now).total_seconds())>3600):
        raise ValueError('Invalid or stale prospective result archive')
    fixtures=source['fixtures']
    if any(type(f.get('fixtureId')) is not int or f['fixtureId']<=0 for f in fixtures):
        raise ValueError('Invalid prospective source fixture ID')
    index={str(f['fixtureId']):f for f in fixtures}
    if len(index)!=len(fixtures):raise ValueError('Duplicate prospective source identity')
    added=0;skipped=0
    for key,record in list(state['forecasts'].items()):
        f=index.get(key);old=state['observations'].get(key)
        if old and old['status']=='review':continue
        if f is None:
            if not old and now>=instant(record['kickoff'])+timedelta(hours=4):
                state['observations'][key]={'status':'unavailable','checkedAt':quality['generatedAt'],'reason':'outside_limited_source'}
            continue  # A truncated source never deletes older observations or forecasts.
        if any(f[k]!=record[k] for k in ('home','away','kickoff')):
            state['observations'][key]={'status':'review','checkedAt':quality['generatedAt'],'reason':'identity_or_schedule_changed'};continue
        if f['state'] in ('review','excluded'):
            state['observations'][key]={'status':'review','checkedAt':quality['generatedAt'],'reason':'source_requires_review'};continue
        if old and old['status']=='settled' and f['state']!='settled':
            state['observations'][key]={'status':'review','checkedAt':quality['generatedAt'],'reason':'source_retracted_final_result'};continue
        if f['state']=='settled':
            latest=f['latest'];score=latest.get('score')
            if (latest.get('status')!='settled' or latest.get('providerStatus')!='FINISHED'
                    or instant(quality['generatedAt'])<instant(f['kickoff'])+timedelta(hours=4)
                    or instant(latest['checkedAt'])<instant(f['kickoff'])+timedelta(hours=4)
                    or instant(latest['checkedAt'])>now+timedelta(minutes=5)
                    or not isinstance(score,dict)
                    or any(type(score.get(k)) is not int or not 0<=score[k]<=30 for k in ('home','away'))):
                raise ValueError('Unverified neural result score or safety fence')
            outcome=0 if score['home']>score['away'] else 1 if score['home']==score['away'] else 2
            state['observations'][key]={'status':'settled','checkedAt':latest['checkedAt'],'score':score,'actual':outcome}
    # First registration wins, even after a later model fit or a score correction.
    for f in sorted(fixtures,key=lambda v:(v['kickoff'],v['fixtureId'])):
        current=registration_clock() if registration_clock else now
        key=str(f['fixtureId']);lead=instant(f['kickoff'])-current
        if key in state['forecasts'] or f['state']!='pending' or not timedelta(hours=24)<=lead<=timedelta(days=7):continue
        if added>=POLICY['maximumNewForecastsPerCycle'] or len(state['forecasts'])>=5000:break
        try:features=forecast_features(games,f,current.date()-timedelta(days=1))
        except ValueError:skipped+=1;continue
        p=predict_model(model['network'],[features['features']],[features['base']])[0];weight=model['blendWeight']
        candidate=[(1-weight)*a+weight*b for a,b in zip(features['base'],p)] if weight else list(features['base'])
        registered=registration_clock() if registration_clock else now
        lead=instant(f['kickoff'])-registered
        if not timedelta(hours=24)<=lead<=timedelta(days=7):continue
        state['forecasts'][key]={'protocol':'neural-agent-future-1','fixtureId':f['fixtureId'],
            'home':f['home'],'away':f['away'],'kickoff':f['kickoff'],'registeredAt':registered.isoformat(),
            'league':'PL','candidate':candidate,'neural':p,**features,'modelSha256':model_sha,
            'modelVersion':model['modelVersion'],'blendWeight':weight,'dataSha256':model['dataSha256'],
            'sourceQualitySha256':sha(quality),'sourceQualityGeneratedAt':quality['generatedAt'],
            'forecastHorizonHours':lead.total_seconds()/3600,'promotedToDefault':False}
        added+=1
    scored=[{**f,**state['observations'][key]} for key,f in state['forecasts'].items()
            if state['observations'].get(key,{}).get('status')=='settled']
    unavailable=sum(v['status']=='unavailable' for v in state['observations'].values())
    review=sum(v['status']=='review' for v in state['observations'].values())
    return {'registered':len(state['forecasts']),'newlyRegistered':added,'unknownClubsSkipped':skipped,
        'settled':len(scored),'review':sum(v['status']=='review' for v in state['observations'].values()),
        'pending':len(state['forecasts'])-len(scored)-review-unavailable,'unavailable':unavailable,
        'metrics':{k:metrics(scored,k) for k in ('candidate','base','frequency')},
        'scope':'new pre-match PL forecasts only; overlaps older experiments and is not an independent multi-league proof',
        'sourceGeneratedAt':quality['generatedAt'],'sourceSha256':sha(quality),'existingForecastsPreserved':True}


def run(directory,output,as_of,*,snapshot=False,quality_path=None,now=None):
    started=time.perf_counter()
    timings={k:0. for k in ('loadData','fingerprint','restoreModel','features','monitoringFit',
                            'monitoringEvaluation','finalFit','observer','persist')}
    fixed_clock=now is not None
    now=now or datetime.now(timezone.utc);directory=Path(directory);output=Path(output)
    if any((ROOT/'docs/research').resolve()==p.resolve() or (ROOT/'docs/research').resolve() in p.resolve().parents for p in (directory,output)):
        raise ValueError('Trainer agents cannot overwrite frozen research studies')
    directory.mkdir(parents=True,exist_ok=True);output.mkdir(parents=True,exist_ok=True)
    state=load_state(directory);plan=json.loads(PLAN.read_text());validate_plan(plan)
    selection=json.loads((ROOT/'docs/research/neural-2026-11-16/selection.json').read_text())
    study=json.loads((ROOT/'docs/research/neural-2026-11-16/report.json').read_text())
    if (selection['planSha256']!=sha(plan) or selection['chosenBeforeLaterMetrics'] is not True
            or sha(selection)!=study['selectionSha256'] or study['planSha256']!=sha(plan)):
        raise ValueError('Trainer recipe has no frozen early validation selection')
    report={'schemaVersion':1,'protocol':PROTOCOL,'startedAt':now.isoformat(),'checkedAt':now.isoformat(),'asOf':as_of.isoformat(),
        'status':'failed','roles':{},'policy':POLICY,'promotedToDefault':False,'independentAdvantageEstablished':False}
    try:
        phase=time.perf_counter()
        if snapshot:games=source_games(plan);sources=[{'source':'pinned offline corpus','dataSha256':plan['sourceDataSha256']}]
        else:games,sources=load_live_sources(directory/'data',as_of)
        timings['loadData']=time.perf_counter()-phase;phase=time.perf_counter()
        data=normalize(games);data_sha=sha(data)
        fingerprint=sha({'dataSha256':data_sha,'codeSha256':implementation_sha(),'planSha256':sha(plan),'blendWeight':selection['weight']})
        report['roles']['data']={'status':'checked','matches':len(games),'dataSha256':data_sha,
            'sources':len(sources),'sourceManifest':sources}
        old=state.get('training');same=old and old['inputFingerprint']==fingerprint
        timings['fingerprint']=time.perf_counter()-phase;phase=time.perf_counter()
        if same:
            model=json.loads((directory/'model.json').read_text())
            if sha(model)!=old['modelSha256']:raise ValueError('Stored trainer model checksum changed')
            monitoring=json.loads((directory/'monitoring.json').read_text())
            trained=old
            timings['restoreModel']=time.perf_counter()-phase
            report['featurePreparation']={'mode':'not_needed','reason':'unchanged_training_inputs'}
        else:
            hashes={name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in FEATURE_CODE}
            feature_cache=directory/'feature-rows.json.gz'
            rows,preparation=prepare_rows(games,plan,directory)
            report['featurePreparation']=preparation
            timings['features']=time.perf_counter()-phase
            eligible=[r for r in rows if r['season']>=2016 and r['date']<(as_of-timedelta(days=1)).isoformat()]
            targets=[]
            for d in ORDER:
                league=[r for r in eligible if r['division']==d]
                if len(league)<500:raise ValueError('Not enough trainer history per division')
                targets.extend(league[-POLICY['monitoringTargetsPerDivision']:])
            cutoff=min(r['cutoff_date'] for r in targets)
            earlier=[r for r in eligible if r['date']<cutoff]
            phase=time.perf_counter()
            examiner=train(earlier,cutoff,plan)
            timings['monitoringFit']=time.perf_counter()-phase;phase=time.perf_counter()
            observed=predict(sorted(targets,key=lambda r:(r['date'],r['division'],r['home'])),examiner,selection['weight'])
            monitoring=evaluate(observed,plan)
            timings['monitoringEvaluation']=time.perf_counter()-phase;phase=time.perf_counter()
            network=train(eligible,(as_of-timedelta(days=1)).isoformat(),plan)
            timings['finalFit']=time.perf_counter()-phase
            model={'schemaVersion':1,'protocol':PROTOCOL,'network':network,'blendWeight':selection['weight'],
                'featureNames':list(FEATURE_NAMES),'dataSha256':data_sha,'asOf':as_of.isoformat(),
                'trainingCutoffExclusive':(as_of-timedelta(days=1)).isoformat(),'promotedToDefault':False}
            model['modelVersion']='neural-agent-'+as_of.isoformat()+'-'+sha(model)[:12]
            finished=now if fixed_clock else datetime.now(timezone.utc)
            trained={'inputFingerprint':fingerprint,'modelSha256':sha(model),'modelVersion':model['modelVersion'],
                'trainedAt':finished.isoformat(),'monitoringEvaluatedAt':finished.isoformat(),'trainingMatches':len(eligible)}
            dump(output/'monitoring-predictions.json',observed);dump(output/'examiner-model.json',examiner)
        report['roles']['trainer']={'status':'unchanged' if same else 'trained','fits':0 if same else 2,
            **trained,'parameterCount':model['network']['fit']['parameterCount'],'converged':model['network']['fit']['converged'],
            'stopReason':model['network']['fit']['stopReason']}
        report['roles']['examiner']={'status':'reused' if same else 'evaluated','evaluatedAt':trained['monitoringEvaluatedAt'],
            'kind':'rolling historical monitoring; not prospective','monitoring':monitoring,
            'recipeHistoricalGatePassed':study['historicalGatePassed'],
            'publicPromotionAllowed':False,'reason':'independent future evidence required'}
        phase=time.perf_counter()
        quality=json.loads(Path(quality_path).read_text()) if quality_path else json.loads(fetch(API+'/v1/quality',3_000_000)[0])
        observed=now if fixed_clock else datetime.now(timezone.utc)
        prospective=observe_and_register(state,model,trained['modelSha256'],games,quality,observed,
            registration_clock=None if fixed_clock else lambda:datetime.now(timezone.utc))
        timings['observer']=time.perf_counter()-phase;phase=time.perf_counter()
        # State advances only after every role verified its inputs; failed observers cannot replace local weights.
        if not same:
            dump(directory/'model.json',model);dump(directory/'monitoring.json',monitoring)
            (directory/'training-data.json.gz').write_bytes(gzip.compress(json.dumps(data,ensure_ascii=False,separators=(',',':')).encode(),mtime=0))
            compressed=gzip.compress(json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode(),mtime=0)
            feature_cache.write_bytes(compressed)
            dump(directory/'feature-rows-manifest.json',{'protocol':'neural-features-1','featureNames':list(FEATURE_NAMES),
                'sourceDataSha256':data_sha,'dataSha256':sha(rows),'compressedSha256':hashlib.sha256(compressed).hexdigest(),
                'codeHashes':hashes,'incrementalCodeSha256':cache_implementation_sha()})
        report['roles']['observer']={'status':'checked',**prospective}
        state['training']=trained;validate_state(state);dump(directory/'state.json',state)
        dump(output/'quality-source.json',quality);dump(output/'model.json',model)
        report.update(status='success',inputFingerprint=fingerprint,dataSha256=data_sha,
            modelSha256=trained['modelSha256'],modelVersion=trained['modelVersion'],trainedAt=trained['trainedAt'],
            trainingMatches=trained['trainingMatches'],prospective=prospective,
            planSha256=sha(plan),codeSha256=implementation_sha())
        timings['persist']=time.perf_counter()-phase
    except Exception as error:
        report['errorType']=type(error).__name__;report['message']='Trainer cycle failed; previous published model and forecasts are retained.'
        raise
    finally:
        report['timingsSeconds']={**timings,'total':time.perf_counter()-started}
        report['checkedAt']=(now if fixed_clock else datetime.now(timezone.utc)).isoformat()
        dump(output/'report.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--as-of',type=date.fromisoformat,default=datetime.now(timezone.utc).date())
    parser.add_argument('--snapshot',action='store_true');parser.add_argument('--quality-source',type=Path)
    args=parser.parse_args();result=run(args.state_dir,args.output,args.as_of,snapshot=args.snapshot,quality_path=args.quality_source)
    print(json.dumps({'status':result['status'],'roles':{k:v['status'] for k,v in result['roles'].items()},
        'modelVersion':result['modelVersion'],'prospective':result['prospective']}))
