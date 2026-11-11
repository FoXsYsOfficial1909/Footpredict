"""Independently verify and settle the immutable one-hundred-match forecast cohort."""
from __future__ import annotations
import argparse
from datetime import datetime,timedelta,timezone
import hashlib
import json
import math
from pathlib import Path
import urllib.request

from scripts.verify_candidate import distribution,valid_probabilities

ROOT=Path(__file__).resolve().parents[1]
DIRECTORY=ROOT/'docs/research/hundred-2026-11-11'
PROTOCOL='hundred-future-1'
REGISTERED_SHA='115bccbb92bcb85c69e4141d50d86cc185c70a62fdfbad6e2630517db820b39d'
LEAGUES=('BSA','ELC','PD','PL')
API='https://footpredict-api.wzjp5747md.workers.dev'


def instant(value):
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    if result.tzinfo is None:raise ValueError('An explicit timezone is required')
    return result.astimezone(timezone.utc)


def reproduce(fixture,inputs,packet):
    p=packet['parameters'];year=fixture['season'];kickoff=instant(fixture['kickoff'])
    frozen=instant(packet['frozenAt']);seen=set()
    for game in inputs:
        if (type(game.get('id')) is not int or game['id'] in seen or game['id']==fixture['fixtureId']
            or game.get('status')!='FINISHED' or game['score'].get('duration')!='REGULAR'
            or game.get('__year') not in (year,year-1) or instant(game['utcDate'])>frozen-timedelta(hours=4)):
            raise ValueError('Input identity or pre-registration information fence mismatch')
        seen.add(game['id'])
        if any(type(game['score']['fullTime'].get(k)) is not int or not 0<=game['score']['fullTime'][k]<=30 for k in ('home','away')):
            raise ValueError('Invalid historical score')
    home=[g for g in inputs if g['homeTeam']['id']==fixture['homeId']]
    away=[g for g in inputs if g['awayTeam']['id']==fixture['awayId']]
    if len(inputs)<p['minLeagueGames'] or len(home)<p['minTeamSideGames'] or len(away)<p['minTeamSideGames']:
        raise ValueError('Insufficient historical inputs')
    weights=[math.exp(-math.log(2)*max(0,(kickoff-instant(g['utcDate'])).total_seconds()/86400)/p['halfLifeDays'])*
        (1 if g['__year']==year else p['priorSeasonWeight']) for g in inputs]
    total=math.fsum(weights)
    means={side:math.fsum(w*g['score']['fullTime'][side] for w,g in zip(weights,inputs))/total for side in ('home','away')}
    def smooth(team,venue,score,prior):
        selected=[(w,g) for w,g in zip(weights,inputs) if g[venue+'Team']['id']==team]
        return (math.fsum(w*g['score']['fullTime'][score] for w,g in selected)+p['regularizationGames']*prior)/(
            math.fsum(w for w,g in selected)+p['regularizationGames'])
    h=max(.05,min(p['goalCap'],smooth(fixture['homeId'],'home','home',means['home'])*smooth(fixture['awayId'],'away','home',means['home'])/means['home']))
    a=max(.05,min(p['goalCap'],smooth(fixture['awayId'],'away','away',means['away'])*smooth(fixture['homeId'],'home','away',means['away'])/means['away']))
    counts=[1.,1.,1.]
    for w,g in zip(weights,inputs):
        score=g['score']['fullTime'];counts[0 if score['home']>score['away'] else 1 if score['home']==score['away'] else 2]+=w
    return distribution(h,a),[v/math.fsum(counts) for v in counts],[h,a]


def verify_files(directory=DIRECTORY,expected_sha=REGISTERED_SHA):
    directory=Path(directory);raw=(directory/'packet.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha or (directory/'packet.sha256').read_text().strip()!=expected_sha:
        raise ValueError('The registered forecast packet changed')
    packet=json.loads(raw)
    if (packet.get('protocol')!=PROTOCOL or packet.get('schemaVersion')!=1 or packet.get('modelVersion')!='poisson-1.0.0'
        or packet.get('market')!='1X2_REGULATION' or len(packet.get('fixtures',[]))!=100
        or packet.get('forecastCount')!=100 or set(packet['leagues'])!=set(LEAGUES)):
        raise ValueError('Invalid hundred-match protocol')
    params={'priorSeasonWeight':.35,'halfLifeDays':120,'regularizationGames':5,'minLeagueGames':15,
        'minTeamSideGames':2,'finishSafetyHours':4,'goalCap':5,'poissonMax':20}
    if packet['parameters']!=params:raise ValueError('Frozen model settings changed')
    frozen=instant(packet['frozenAt']);inputs={}
    for code in LEAGUES:
        body=(directory/'inputs'/(code+'.json')).read_bytes()
        if hashlib.sha256(body).hexdigest()!=packet['inputSha256'][code]:raise ValueError('Frozen input bytes changed')
        inputs[code]=json.loads(body)
    for source in packet['sourceManifest']:
        if instant(source['observedAt'])>frozen:raise ValueError('Source was observed after registration')
    seen=set()
    for f in packet['fixtures']:
        if (type(f['fixtureId']) is not int or f['fixtureId'] in seen or f['league'] not in LEAGUES
            or f['homeId']==f['awayId'] or f['season']!=2026 or f['statusAtFreeze'] not in ('TIMED','SCHEDULED')
            or not timedelta(hours=24)<=instant(f['kickoff'])-frozen<=timedelta(days=45)):
            raise ValueError('Invalid frozen fixture identity or registration deadline')
        seen.add(f['fixtureId'])
        expected=reproduce(f,inputs[f['league']],packet)
        for actual,computed in zip((f['probabilities'],f['baseline'],f['expectedGoals']),expected):
            if any(not math.isfinite(v) for v in actual) or len(actual)!=len(computed) or max(abs(a-b) for a,b in zip(actual,computed))>1e-11:
                raise ValueError('Probabilities disagree with the frozen production model')
        if not valid_probabilities(f['probabilities']) or not valid_probabilities(f['baseline']):
            raise ValueError('Invalid frozen probability vector')
    return packet


def score_rows(rows,key):
    settled=[r for r in rows if r['status']=='settled']
    if not settled:return {'n':0,'brier':None,'logLoss':None,'correct':0,'topEligible':0,'accuracy':None}
    brier=logloss=correct=top=0
    for r in settled:
        p=r[key];target=r['outcome'];brier+=sum((v-(i==target))**2 for i,v in enumerate(p))
        logloss-=math.log(max(p[target],1e-12));largest=max(p)
        winners=[i for i,v in enumerate(p) if abs(v-largest)<=1e-12]
        if len(winners)==1:top+=1;correct+=winners[0]==target
    return {'n':len(settled),'brier':brier/len(settled),'logLoss':logloss/len(settled),
        'correct':correct,'topEligible':top,'accuracy':correct/top if top else None}


def settle(packet,responses,now,previous=None):
    now=instant(now) if isinstance(now,str) else now
    games={};duplicates=set()
    for code,response in responses.items():
        if code not in LEAGUES or response.get('competition',{}).get('code')!=code or NumberSeason(response)!=2026:
            raise ValueError('Result response is from a different competition or season')
        for game in response.get('matches',[]):
            key=(code,game.get('id'))
            if key in games:duplicates.add(key)
            games[key]=game
    old={r['fixtureId']:r for r in (previous or {}).get('fixtures',[])}
    rows=[]
    for f in packet['fixtures']:
        row={**f,'status':'pending','outcome':None,'score':None,'reason':None}
        game=games.get((f['league'],f['fixtureId']))
        if previous and previous.get('packetSha256')!=REGISTERED_SHA:raise ValueError('Previous results refer to another packet')
        if old.get(f['fixtureId'],{}).get('status')=='review':
            row.update(status='review',reason=old[f['fixtureId']]['reason'])
        elif (f['league'],f['fixtureId']) in duplicates:
            row.update(status='review',reason='duplicate_provider_identity')
        elif game is None:
            row.update(status='unavailable',reason='fixture_not_returned')
        elif (game.get('homeTeam',{}).get('id')!=f['homeId'] or game.get('awayTeam',{}).get('id')!=f['awayId']
            or game.get('homeTeam',{}).get('name')!=f['home'] or game.get('awayTeam',{}).get('name')!=f['away']
            or game.get('utcDate') is None or instant(game['utcDate'])!=instant(f['kickoff'])):
            row.update(status='review',reason='fixture_identity_or_kickoff_changed')
        elif game.get('status') in ('POSTPONED','CANCELLED','SUSPENDED','AWARDED'):
            row.update(status='review',reason='provider_'+game['status'].lower())
        elif game.get('status')=='FINISHED' and instant(f['kickoff'])<=now-timedelta(hours=4):
            score=game.get('score',{});result=score.get('fullTime') or {}
            h,a=result.get('home'),result.get('away')
            if score.get('duration')!='REGULAR' or any(type(v) is not int or not 0<=v<=30 for v in (h,a)):
                row.update(status='review',reason='unverified_regulation_score')
            else:row.update(status='settled',outcome=0 if h>a else 1 if h==a else 2,score={'home':h,'away':a})
        rows.append(row)
    counts={key:sum(r['status']==key for r in rows) for key in ('pending','settled','review','unavailable')}
    league={code:{'count':sum(r['league']==code for r in rows),'model':score_rows([r for r in rows if r['league']==code],'probabilities'),
        'baseline':score_rows([r for r in rows if r['league']==code],'baseline')} for code in LEAGUES}
    return {'schemaVersion':1,'protocol':PROTOCOL,'status':'complete' if not counts['pending'] and not counts['unavailable'] else 'active',
        'checkedAt':now.isoformat(),'packetSha256':REGISTERED_SHA,'frozenAt':packet['frozenAt'],
        'total':100,'counts':counts,'model':score_rows(rows,'probabilities'),'baseline':score_rows(rows,'baseline'),
        'byLeague':league,'fixtures':rows,'forecastsChanged':False,
        'note':'Same frozen one-hundred-match cohort. Current verified results may be corrected; probabilities never change.'}


def NumberSeason(packet):
    try:return int(packet.get('filters',{}).get('season'))
    except (ValueError,TypeError):return None


def download_results(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True);responses={}
    for code in LEAGUES:
        url=API+f'/fd/competitions/{code}/matches?season=2026'
        request=urllib.request.Request(url,headers={'User-Agent':'FootpredictResearch/1.0'})
        with urllib.request.urlopen(request,timeout=30) as response:raw=response.read(2500001)
        if len(raw)>2500000:raise ValueError('Result response too large')
        responses[code]=json.loads(raw)
        (directory/(code+'.json')).write_bytes(raw)
    return responses


def can_finalize(packet,previous,now):
    return bool(previous and previous.get('status')=='complete' and previous.get('packetSha256')==REGISTERED_SHA
        and now>max(instant(f['kickoff']) for f in packet['fixtures'])+timedelta(days=14))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=('verify','settle'))
    parser.add_argument('--output',type=Path);parser.add_argument('--previous',type=Path)
    args=parser.parse_args();packet=verify_files()
    if args.command=='verify':
        print(json.dumps({'verified':True,'forecasts':100,'sha256':REGISTERED_SHA,'frozenAt':packet['frozenAt']}));return
    args.output.mkdir(parents=True,exist_ok=True)
    previous=json.loads(args.previous.read_text()) if args.previous and args.previous.exists() else None
    if previous and previous.get('finalized') is True:
        print(json.dumps({'status':'finalized','providerRequests':0}));return
    now=datetime.now(timezone.utc)
    if can_finalize(packet,previous,now):
        report={**previous,'finalized':True,'sourceReadSkipped':True,
            'note':'Cohort archived after the 14-day correction window; checkedAt retains the last real source check.'}
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,separators=(',',':'),allow_nan=False))
        print(json.dumps({'status':'finalized','providerRequests':0}));return
    responses=download_results(args.output/'provider')
    report=settle(packet,responses,datetime.now(timezone.utc),previous)
    report['providerResponseSha256']={code:hashlib.sha256((args.output/'provider'/(code+'.json')).read_bytes()).hexdigest() for code in LEAGUES}
    (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,separators=(',',':'),allow_nan=False))
    text='# Frozen 100-match cohort\n\n'+json.dumps({'counts':report['counts'],'model':report['model'],'baseline':report['baseline']},indent=2)+'\n\nForecasts and inputs remain unchanged.\n'
    (args.output/'report.md').write_text(text)
    print(json.dumps({'counts':report['counts'],'model':report['model'],'packetSha256':REGISTERED_SHA}))


if __name__=='__main__':main()
