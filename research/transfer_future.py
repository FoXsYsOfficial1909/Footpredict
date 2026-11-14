"""Freeze a conditional joint-history policy without rewriting earlier forecasts."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path

from research.elc_future import (ROOT, ORIGINAL_SHA, REGISTERED_SHA as PREVIOUS_SHA,
    sha, verify_files as verify_previous, settle as settle_shared, settle_files as settle_shared_files)
from research.hundred import instant, score_rows
from scripts.verify_candidate import distribution, valid_probabilities

PLAN = ROOT/'docs/research/transfer-policy-plan-2026-11-14.json'
SELECTION = ROOT/'docs/research/transfer-policy-2026-11-14/selection.json'
DIRECTORY = ROOT/'docs/research/elc-transfer-future-2026-11-14'
PROTOCOL = 'elc-transfer-future-1'
REGISTERED_SHA = '4c49896bee1c7c4ca3487d5431a660493cb40559c1932dc4745d64e95af1245c'


def applies(histories, rule):
    return any(v['E1'] < rule['E1GamesExclusiveMaximum'] and v['E0'] >= rule['E0GamesInclusiveMinimum']
               for v in histories.values())


def inputs():
    plan=json.loads(PLAN.read_text());selection=json.loads(SELECTION.read_text());source=ROOT/plan['sourceStudy']
    report_raw=(source/'report.json').read_bytes();model=json.loads((source/'model.json').read_text())
    if (hashlib.sha256(report_raw).hexdigest()!=plan['sourceReportSha256']
        or sha(model)!=plan['sourceModelSha256'] or model['data_sha256']!=plan['sourceDataSha256']
        or selection['planSha256']!=sha(plan) or selection.get('selectedBeforeLaterPolicyMetrics') is not True
        or selection['eligibility']!=plan['eligibility'] or [t['weight'] for t in selection['trials']]!=plan['weights']):
        raise ValueError('The frozen conditional policy or joint model changed')
    rows=json.loads((source/'validation-predictions.json').read_text())
    if sha(rows)!=plan['sourcePredictionSha256']['validation']:raise ValueError('Changed early selection inputs')
    for trial in selection['trials']:
        losses=[];brier=[];weight=trial['weight']
        for row in rows:
            p=[(1-weight)*a+weight*b for a,b in zip(row['v1'],row['joint'])] if applies(row['pastClubGames'],plan['eligibility']) else row['v1']
            losses.append(-math.log(max(p[row['actual']],1e-12)))
            brier.append(sum((v-(i==row['actual']))**2 for i,v in enumerate(p)))
        if (trial['metrics']['n']!=len(rows) or abs(math.fsum(losses)/len(rows)-trial['metrics']['logloss'])>1e-12
            or abs(math.fsum(brier)/len(rows)-trial['metrics']['brier'])>1e-12):
            raise ValueError('Cannot reproduce the learned correction weight')
    if selection['weight']!=min(selection['trials'],key=lambda t:(t['metrics']['logloss'],t['weight']))['weight']:
        raise ValueError('The learned weight was replaced after selection')
    records=json.loads(gzip.decompress((source/'training-data.json.gz').read_bytes()))
    if sha(records)!=plan['sourceDataSha256']:raise ValueError('The joint input archive changed')
    cutoff=instant(model['training_cutoff_exclusive']+'T00:00:00Z').date();counts={}
    for record in records:
        division,day,_,home,away=record[:5]
        age=(cutoff-instant(day+'T00:00:00Z').date()).days
        if 0<age<=plan['eligibility']['windowDays']:
            for team in (home,away):counts.setdefault(team,{'E0':0,'E1':0})[division]+=1
    return plan,selection,model,counts


def joint_forecast(model, home, away):
    engine=model['joint'];levels=engine['divisions'];teams=engine['teams'];elo=model['elo']
    if (model.get('target_division')!='E1' or model.get('promoted_to_default') is not False
        or engine.get('is_xg') is not False or set(levels)!={'E0','E1'}
        or any(t not in teams or t not in elo['ratings'] for t in (home,away))):
        raise ValueError('Invalid joint forecast scope or unknown club')
    level=levels['E1'];h=teams[home];a=teams[away]
    lh=max(.05,min(5,math.exp(level['intercept']+level['home_advantage']+h['attack']-a['defence'])))
    la=max(.05,min(5,math.exp(level['intercept']+a['attack']-h['defence'])))
    poisson=distribution(lh,la);params=elo['parameters']
    delta=elo['ratings'][home]-elo['ratings'][away]+params['home_advantage']
    strength=10**(delta/800);values=[strength,params['draw_weight'],1/strength];mass=math.fsum(values)
    weight=model['poisson_weight']
    return [weight*p+(1-weight)*v/mass for p,v in zip(poisson,values)]


def build_packet(now):
    previous=verify_previous();plan,selection,model,counts=inputs()
    now=instant(now) if isinstance(now,str) else now
    if selection['weight']==0:raise ValueError('The selected baseline needs no new future candidate')
    cutoff=model['training_cutoff_exclusive']
    if (instant(model['as_of']+'T00:00:00Z')>now or instant(cutoff+'T00:00:00Z')>now-timedelta(days=1)
        or model['joint']['as_of']!=cutoff or model['elo']['as_of']!=cutoff
        or max(model['joint']['fit']['latestTrainingDate'],model['elo']['latest_training_date'])>=cutoff):
        raise ValueError('Joint weights cross the registration information fence')
    fixtures=[];skipped=[]
    for old in previous['fixtures']:
        if instant(old['kickoff'])-now<timedelta(hours=plan['minimumLeadHours']):
            skipped.append({'fixtureId':old['fixtureId'],'reason':'inside_24_hour_deadline'});continue
        histories={name:counts[name] for name in (old['homeCsv'],old['awayCsv'])}
        eligible=applies(histories,plan['eligibility']);joint=joint_forecast(model,old['homeCsv'],old['awayCsv'])
        weight=selection['weight'] if eligible else 0
        candidate=[(1-weight)*a+weight*b for a,b in zip(old['v1'],joint)] if eligible else list(old['v1'])
        fixtures.append({k:old[k] for k in ('fixtureId','league','season','homeId','awayId','home','away','homeCsv','awayCsv','kickoff','v1','base')}
            | {'previousShots':old['candidate'],'joint':joint,'candidate':candidate,'pastClubGames':histories,
               'policyApplicable':eligible,'policyChanged':max(abs(a-b) for a,b in zip(candidate,old['v1']))>1e-12})
    if not fixtures or not any(f['policyChanged'] for f in fixtures):raise ValueError('No eligible changed future forecasts')
    return {'schemaVersion':1,'protocol':PROTOCOL,'market':'1X2_REGULATION','registeredAt':now.isoformat(),
        'sourceCohortSha256':ORIGINAL_SHA,'previousPacketSha256':PREVIOUS_SHA,
        'previousRegisteredAt':previous['registeredAt'],'originalRegisteredAt':previous['originalRegisteredAt'],
        'planSha256':sha(plan),'selectionSha256':sha(selection),'selection':selection,'weight':selection['weight'],
        'modelSha256':sha(model),'model':model,'forecastCount':len(fixtures),'fixtures':fixtures,'skipped':skipped,
        'applicableMatches':sum(f['policyApplicable'] for f in fixtures),'changedMatches':sum(f['policyChanged'] for f in fixtures),
        'independentAdvantageEstablished':False,'promotedToDefault':False,'existingFrozenPredictionsChanged':False,
        'limitations':plan['limitations']}


def verify_files(directory=DIRECTORY, expected_sha=REGISTERED_SHA):
    directory=Path(directory);raw=(directory/'packet.json').read_bytes()
    if (expected_sha is None or hashlib.sha256(raw).hexdigest()!=expected_sha
        or (directory/'packet.sha256').read_text().strip()!=expected_sha):raise ValueError('The conditional future packet changed')
    packet=json.loads(raw);rebuilt=build_packet(packet['registeredAt'])
    if len(packet['fixtures'])!=len(rebuilt['fixtures']):raise ValueError('Changed conditional cohort size')
    for actual,calculated in zip(packet['fixtures'],rebuilt['fixtures']):
        for key in ('candidate','v1','base','joint','previousShots'):
            if (not valid_probabilities(actual[key]) or max(abs(a-b) for a,b in zip(actual[key],calculated[key]))>1e-11):
                raise ValueError('Cannot reproduce conditional probabilities')
            calculated[key]=actual[key]
    if packet!=rebuilt:raise ValueError('Cannot reproduce conditional registration or eligibility')
    return packet


def freeze(directory=DIRECTORY):
    directory=Path(directory)
    if (directory/'packet.json').exists():raise ValueError('Refusing to overwrite conditional future forecasts')
    packet=build_packet(datetime.now(timezone.utc));directory.mkdir(parents=True,exist_ok=True)
    raw=json.dumps(packet,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    with (directory/'packet.json').open('xb') as stream:stream.write(raw)
    digest=hashlib.sha256(raw).hexdigest()
    with (directory/'packet.sha256').open('x') as stream:stream.write(digest+'\n')
    with (directory/'forecasts.csv').open('x',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream,delimiter=';',lineterminator='\n')
        writer.writerow(['fixtureId','kickoffUTC','home','away','policyApplicable','v1P1','v1Draw','v1P2','candidateP1','candidateDraw','candidateP2'])
        for f in packet['fixtures']:writer.writerow([f['fixtureId'],f['kickoff'],f['home'],f['away'],f['policyApplicable'],*f['v1'],*f['candidate']])
    print(json.dumps({'registeredAt':packet['registeredAt'],'forecastCount':len(packet['fixtures']),
        'applicableMatches':packet['applicableMatches'],'changedMatches':packet['changedMatches'],'packetSha256':digest}))
    return packet,digest


def settle(packet, source, now):
    result=settle_shared(packet,source,now,packet_sha=REGISTERED_SHA,protocol=PROTOCOL)
    result['metrics']['previousShots']=score_rows(result['fixtures'],'previousShots')
    applied=[f for f in result['fixtures'] if f['policyApplicable']]
    result['applicableCounts']={status:sum(f['status']==status for f in applied) for status in ('pending','settled','review','unavailable')}
    result['applicableMetrics']={key:score_rows(applied,key) for key in ('candidate','v1','base','previousShots')}
    result['applicableMatches']=packet['applicableMatches'];result['changedMatches']=packet['changedMatches']
    result['previousPacketSha256']=PREVIOUS_SHA
    result['note']=f"Overlaps the prior 36 games; only {packet['changedMatches']} changed predictions test this policy. Zero-change fixtures are baseline reuse, not additional evidence."
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=('freeze','verify','settle'))
    parser.add_argument('--source',type=Path);parser.add_argument('--previous-source',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.command=='freeze':freeze()
    elif args.command=='verify':
        packet=verify_files();print(json.dumps({'verified':True,'forecastCount':packet['forecastCount'],'changedMatches':packet['changedMatches'],'packetSha256':REGISTERED_SHA}))
    else:
        if not args.source or not args.output:parser.error('settle requires --source and --output')
        settle_shared_files(args.source,args.output,previous_source=args.previous_source,
            packet_loader=verify_files,result_fn=settle,protocol=PROTOCOL,packet_sha=REGISTERED_SHA)
