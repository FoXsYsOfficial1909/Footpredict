"""Reproduce a downloaded nightly-live-1 packet offline with the standard library."""
from datetime import datetime
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]


def require(condition,message):
    if not condition:raise ValueError(message)


def reject_constant(value):
    raise ValueError('Non-finite JSON number')


def loads(text):
    return json.loads(text,parse_constant=reject_constant)


def instant(value):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    require(parsed.tzinfo is not None,'A timestamp requires a timezone')
    return parsed.timestamp()


def distribution(home,away):
    h=[math.exp(-home)*home**i/math.factorial(i) for i in range(21)]
    a=[math.exp(-away)*away**i/math.factorial(i) for i in range(21)]
    values=[math.fsum(h[i]*a[j] for i in range(21) for j in range(21) if (0 if i>j else 1 if i==j else 2)==key) for key in range(3)]
    total=math.fsum(values)
    return [value/total for value in values]


def ensemble(model,home,away):
    home=model['aliases'].get(home,home);away=model['aliases'].get(away,away)
    glm=model['glm'];hs=glm['teams'].get(home,{'attack':0,'defence':0});aws=glm['teams'].get(away,{'attack':0,'defence':0})
    h=max(.05,min(5,math.exp(glm['intercept']+glm['home_advantage']+hs['attack']-aws['defence'])))
    a=max(.05,min(5,math.exp(glm['intercept']+aws['attack']-hs['defence'])))
    poisson=distribution(h,a);elo=model['elo']
    delta=elo['ratings'].get(home,1500)-elo['ratings'].get(away,1500)+elo['parameters']['home_advantage']
    strength=10**(delta/800);values=[strength,elo['parameters']['draw_weight'],1/strength];total=math.fsum(values)
    return [model['glm_weight']*value+(1-model['glm_weight'])*values[i]/total for i,value in enumerate(poisson)]


def valid_probabilities(values):
    return isinstance(values,list) and len(values)==3 and all(type(value) in (int,float) and math.isfinite(value) and 0<=value<=1 for value in values) and abs(math.fsum(values)-1)<1e-8


def verify(packet):
    require(packet['protocol']=='nightly-live-1' and packet['market']=='1X2_REGULATION','Unknown prediction protocol or market')
    require(set(packet['models'])=={'v1','v2','base','candidate'},'All four models are required')
    saved,kickoff=instant(packet['savedAt']),instant(packet['kickoff'])
    require(kickoff-saved>=86400,'Prediction was saved inside the 24-hour deadline')
    require(all(instant(source['fetchedAt'])<=saved for source in packet['sources']),'A source was fetched after registration')
    inputs=packet['inputs'];params=packet['parameters'];year=packet['year']
    require(params['poissonMax']==20,'Unsupported Poisson truncation')
    require(len({game['id'] for game in inputs})==len(inputs),'Duplicate inputs')
    require(all(game['id']!=packet['fixtureId'] and game['status']=='FINISHED' and game['score']['duration']=='REGULAR' and
        game['__year'] in (year,year-1) and instant(game['utcDate'])<=saved-params['finishSafetyHours']*3600 for game in inputs),'Input violates the information cutoff')
    weights=[math.exp(-math.log(2)*max(0,(kickoff-instant(game['utcDate']))/86400)/params['halfLifeDays'])*
        (1 if game['__year']==year else params['priorSeasonWeight']) for game in inputs]
    mass=math.fsum(weights);require(mass>0,'No past inputs')
    league_h=math.fsum(weight*game['score']['fullTime']['home'] for weight,game in zip(weights,inputs))/mass
    league_a=math.fsum(weight*game['score']['fullTime']['away'] for weight,game in zip(weights,inputs))/mass
    def smooth(team,venue,side,prior):
        rows=[(weight,game) for weight,game in zip(weights,inputs) if game[venue+'Team']['id']==team]
        return (math.fsum(weight*game['score']['fullTime'][side] for weight,game in rows)+params['regularizationGames']*prior)/(math.fsum(weight for weight,game in rows)+params['regularizationGames'])
    h=max(.05,min(params['goalCap'],smooth(packet['homeId'],'home','home',league_h)*smooth(packet['awayId'],'away','home',league_h)/league_h))
    a=max(.05,min(params['goalCap'],smooth(packet['awayId'],'away','away',league_a)*smooth(packet['homeId'],'home','away',league_a)/league_a))
    counts=[1.,1.,1.]
    for weight,game in zip(weights,inputs):
        score=game['score']['fullTime'];counts[0 if score['home']>score['away'] else 1 if score['home']==score['away'] else 2]+=weight
    artifact=packet['candidateArtifact'];report=artifact['trainingReport'];raw=artifact['modelJson'];model=loads(raw)
    checksum=hashlib.sha256(raw.encode()).hexdigest()
    require(checksum==artifact['modelSha256']==report['modelSha256']==packet['models']['candidate']['modelSha256'],'Candidate checksum mismatch')
    require(model['model_version']==report['modelVersion']==packet['models']['candidate']['modelVersion'],'Candidate version mismatch')
    require(model['promoted_to_default'] is False and report['promotedToDefault'] is False,'Unexpected model promotion')
    require(model['as_of']==report['asOf'] and model['training_cutoff_exclusive']==report['trainingCutoffExclusive'] and
        model['glm']['as_of']==model['elo']['as_of']==report['trainingCutoffExclusive'] and
        max(model['glm']['fit']['latest_training_date'],model['elo']['latest_training_date'])<report['trainingCutoffExclusive'] and
        report['latestTrainingMatch']<report['trainingCutoffExclusive']<report['asOf'] and
        instant(report['asOf']+'T00:00:00Z')-instant(report['trainingCutoffExclusive']+'T00:00:00Z')==86400,'Training data fence mismatch')
    require(instant(report['generatedAt'])<=saved and saved-instant(report['generatedAt'])<=36*3600,'Candidate was not fresh before registration')
    require(instant(artifact['modelFetchedAt'])<=saved and instant(artifact['reportFetchedAt'])<=saved,'Candidate was fetched after registration')
    v2_path=ROOT/'worker/model-v2-data.json';v2=loads(v2_path.read_text())
    require(v2['model_version']==packet['models']['v2']['modelVersion'] and v2['as_of']==packet['models']['v2']['trainingCutoff'],'The local reference v2 is a different version')
    require(instant(v2['as_of']+'T00:00:00Z')<=saved,'Reference v2 was trained after registration')
    values={'v1':distribution(h,a),'base':[value/math.fsum(counts) for value in counts],
        'candidate':ensemble(model,packet['home'],packet['away']),'v2':ensemble(v2,packet['home'],packet['away'])}
    differences={}
    for key,probabilities in values.items():
        require(valid_probabilities(probabilities) and valid_probabilities(packet['models'][key]['probabilities']),'Invalid probability distribution')
        differences[key]=max(abs(actual-expected) for actual,expected in zip(probabilities,packet['models'][key]['probabilities']))
    require(max(differences.values())<1e-10,'Frozen probabilities do not match the reproduced calculations')
    return {'fixtureId':packet['fixtureId'],'match':packet['home']+' — '+packet['away'],'savedAt':packet['savedAt'],
        'kickoff':packet['kickoff'],'horizonHours':(kickoff-saved)/3600,'inputGames':len(inputs),
        'candidateVersion':model['model_version'],'candidateSha256':checksum,'sourceRunId':report['runId'],
        'referenceV2FileSha256':hashlib.sha256(v2_path.read_bytes()).hexdigest(),
        'trainingCutoffExclusive':report['trainingCutoffExclusive'],'allFourReproduced':True,'maximumDifference':max(differences.values()),
        'probabilities':values,'noFutureInputs':True,'promoted':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('packet',help='Downloaded JSON path, or - for stdin')
    args=parser.parse_args()
    try:
        raw=sys.stdin.read(2000001) if args.packet=='-' else Path(args.packet).read_text()
        require(len(raw)<=2000000,'Packet is too large')
        print(json.dumps(verify(loads(raw)),ensure_ascii=False,indent=2,allow_nan=False))
    except (ValueError,TypeError,KeyError,OSError,OverflowError,ZeroDivisionError) as error:
        parser.exit(1,'Verification failed: '+str(error)+'\n')
