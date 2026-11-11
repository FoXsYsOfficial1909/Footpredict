"""Scheduled, chronological availability correction. Never replaces public odds."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request

import numpy as np
from scipy.optimize import minimize
from scipy.special import logsumexp

from research.features import feature_row, digest
from research.features_time import instant
from research.player_features import SHARES

PROTOCOL = 'availability-residual-1'
BASELINE = 'ensemble-v2-experimental-0.1'
MIN_TRAIN = 60
MIN_PROSPECTIVE = 60
RIDGE = 10.
API = 'https://footpredict-api.wzjp5747md.workers.dev'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def read_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'FootpredictResearch/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read(4_000_001)
    if len(body)>4_000_000:
        raise ValueError('Input response too large')
    return json.loads(body)


def ingest_entry(entry, directory):
    if (set(entry) != {'id', 'sha256', 'payload'} or not isinstance(entry['id'], str)
        or not isinstance(entry['payload'], str) or len(entry['payload'])>200000
        or hashlib.sha256(entry['payload'].encode()).hexdigest()!=entry['sha256']):
        raise ValueError('Research archive checksum or envelope mismatch')
    packet = json.loads(entry['payload'])
    name = hashlib.sha256(entry['id'].encode()).hexdigest()+'.json'
    path = Path(directory)/'snapshots'/name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = canonical(entry)
    if path.exists() and path.read_bytes()!=body:
        raise ValueError('Immutable source snapshot changed')
    path.write_bytes(body)
    return packet


def sync_archive(directory, state, fetch=read_json):
    cursor, pages, seen = state.get('cursor'), 0, set()
    while pages<250:
        address = API+'/v1/research/training'+('?' + urllib.parse.urlencode({'after': cursor}) if cursor else '')
        page = fetch(address)
        if page.get('protocol')!='availability-training-export-1' or not isinstance(page.get('snapshots'), list) or len(page['snapshots'])>4:
            raise ValueError('Invalid research export page')
        for entry in page['snapshots']:
            ingest_entry(entry, directory)
        pages += 1
        cursor = page.get('cursor')
        if not page.get('nextCursor'):
            state['cursor'] = cursor
            return pages
        if not cursor or cursor in seen or cursor!=page['nextCursor']:
            raise ValueError('Research cursor did not advance')
        seen.add(cursor)
    raise ValueError('Archive is incomplete; refusing to publish partial progress')


def archive_rows(directory, as_of):
    rows, reasons, fixtures = [], Counter(), set()
    for path in sorted((Path(directory)/'snapshots').glob('*.json')):
        entry = json.loads(path.read_text())
        # Validate restored files as well as newly downloaded observations.
        if hashlib.sha256(entry['payload'].encode()).hexdigest()!=entry['sha256']:
            raise ValueError('Stored archive checksum mismatch')
        packet = json.loads(entry['payload'])
        fixtures.add(packet.get('fixture', {}).get('id'))
        try:
            row = feature_row(packet)
            if instant(row['collected_at'])>as_of:
                raise ValueError('Snapshot is in the future')
            row['archive_sha256'] = entry['sha256']
            rows.append(row)
        except (KeyError, TypeError, ValueError) as error:
            reasons[str(error)[:140]] += 1
    return rows, {'snapshots':sum(1 for _ in (Path(directory)/'snapshots').glob('*.json')),
        'fixtures':len(fixtures), 'validFeatureSnapshots':len(rows), 'rejectedSnapshots':dict(reasons)}


def vector(row):
    f = row['features']
    if not all(f.get(side+'_fpl_minutes_complete') for side in ('home', 'away')):
        return None
    values = []
    for key in SHARES:
        home, away = (f.get(side+'_fpl_'+key) for side in ('home', 'away'))
        if any(not isinstance(n, (int, float)) or isinstance(n, bool) or not np.isfinite(n) or not 0<=n<=1 for n in (home,away)):
            return None
        values.append(away-home)
    return values


def select_day_ahead(rows):
    chosen, rejected = {}, Counter()
    for row in rows:
        if row['forecast']['model_version']!=BASELINE:
            rejected['different_baseline'] += 1
        elif not 24<=row['features']['horizon_hours']<=168:
            rejected['outside_24_to_168_hour_window'] += 1
        elif vector(row) is None:
            rejected['missing_dated_minutes'] += 1
        else:
            old = chosen.get(row['fixture_id'])
            if old is None or (row['collected_at'],row['snapshot_sha256'])>(old['collected_at'],old['snapshot_sha256']):
                chosen[row['fixture_id']] = row
    return sorted(chosen.values(),key=lambda r:(r['kickoff'],r['fixture_id'])), dict(rejected)


def target(row, games, as_of):
    game = games.get(row['fixture_id'])
    if not game or game.get('status')!='FINISHED' or instant(row['kickoff'])>as_of-timedelta(hours=4):
        return None
    if (game.get('competition', {}).get('code', 'PL')!='PL' or instant(game['utcDate'])!=instant(row['kickoff'])
        or game['homeTeam']['name']!=row['home'] or game['awayTeam']['name']!=row['away']
        or game.get('score', {}).get('duration')!='REGULAR'):
        return None
    score = game['score'].get('fullTime', {})
    home, away = score.get('home'), score.get('away')
    if any(not isinstance(n,int) or isinstance(n,bool) or not 0<=n<=30 for n in (home,away)):
        return None
    return 0 if home>away else 1 if home==away else 2


def predict(probabilities, values, model):
    if model['protocol']!=PROTOCOL or model.get('baselineModelVersion')!=BASELINE or model['featureNames']!=list(SHARES) or len(model['coefficients'])!=3:
        raise ValueError('Incompatible availability model')
    p = np.asarray(probabilities,dtype=float)
    if p.shape!=(3,) or not np.all(np.isfinite(p)) or np.any(p<0) or np.any(p>1) or abs(p.sum()-1)>1e-8:
        raise ValueError('Invalid baseline probability vector')
    if len(values)!=3 or not np.all(np.isfinite(values)) or np.max(np.abs(values))>1:
        raise ValueError('Invalid availability feature vector')
    delta = float(np.dot(values,model['coefficients']))
    if not np.isfinite(delta):
        raise ValueError('Nonfinite correction')
    logits = np.log(np.clip(probabilities,1e-12,1))+np.array([delta,0,-delta])
    return np.exp(logits-logsumexp(logits)).tolist()


def fit_correction(rows, as_of, *, min_train=MIN_TRAIN):
    past = [r for r in rows if r.get('target') is not None and instant(r['kickoff'])<=as_of-timedelta(hours=4)]
    if len(past)<min_train or len({r['target'] for r in past})<3:
        return None
    x = np.array([vector(r) for r in past],dtype=float)
    if np.max(np.ptp(x,axis=0))<1e-6:
        return None
    offsets = np.log(np.clip([r['forecast']['probabilities'] for r in past],1e-12,1))
    y = np.array([r['target'] for r in past]); signs = np.array([1.,0.,-1.])
    def objective(beta):
        logits = offsets + (x@beta)[:,None]*signs
        normalizer = logsumexp(logits,axis=1)
        probs = np.exp(logits-normalizer[:,None])
        loss = np.sum(normalizer-logits[np.arange(len(y)),y])+RIDGE*np.dot(beta,beta)/2
        gradient = x.T@(probs@signs-signs[y])+RIDGE*beta
        return float(loss),gradient
    fitted = minimize(objective,np.zeros(3),jac=True,method='L-BFGS-B',bounds=[(-4.,4.)]*3)
    if not fitted.success or not np.all(np.isfinite(fitted.x)):
        raise ValueError('Availability optimizer did not converge')
    model = {'protocol':PROTOCOL,'baselineModelVersion':BASELINE,'featureNames':list(SHARES),
        'coefficients':fitted.x.tolist(),'ridge':RIDGE,'trainingMatches':len(past),
        'trainedAt':as_of.isoformat(),'latestTrainingKickoff':max(r['kickoff'] for r in past),
        'trainingInputSha256':digest([(r['fixture_id'],r['snapshot_sha256'],r['target']) for r in past]),
        'interpretation':'Differences in dated provider-reported season-minute shares; not confirmed absences.',
        'promotedToDefault':False,'lineupsUsed':False}
    model['modelVersion'] = 'availability-'+digest(model)[:16]
    return model


def metrics(pairs):
    if not pairs:
        return {'n':0,'baseline':None,'availability':None,'delta95':None}
    scores,days = [],{}
    for pair in pairs:
        y = np.eye(3)[pair['target']]
        p,q = np.array(pair['baseline']),np.array(pair['availability'])
        values = [np.sum((p-y)**2),-np.log(max(p[pair['target']],1e-12)),
            np.sum((q-y)**2),-np.log(max(q[pair['target']],1e-12))]
        scores.append(values)
        days.setdefault(pair['kickoff'][:10],[]).append(np.array(values[2:])-np.array(values[:2]))
    mean = np.mean(scores,axis=0)
    bounds = None
    if len(days)>=10:
        blocks = [np.array(v) for v in days.values()]
        rng = np.random.default_rng(1711)
        samples = [np.concatenate([blocks[i] for i in rng.integers(0,len(blocks),len(blocks))]).mean(axis=0) for _ in range(1000)]
        interval = np.quantile(samples,[.025,.975],axis=0)
        bounds = {k:interval[:,i].tolist() for i,k in enumerate(('brier','logLoss'))}
    return {'n':len(pairs),'days':len(days),'baseline':{'brier':float(mean[0]),'logLoss':float(mean[1])},
        'availability':{'brier':float(mean[2]),'logLoss':float(mean[3])},'delta95':bounds}


def chronological_pairs(rows):
    pairs, models = [],{}
    for row in rows:
        if row.get('target') is None:
            continue
        cutoff = instant(row['collected_at'])
        history = [r for r in rows if r.get('target') is not None and instant(r['kickoff'])<=cutoff-timedelta(hours=4)]
        key = digest([(r['fixture_id'],r['target'],r['snapshot_sha256']) for r in history])
        if key not in models:
            models[key] = fit_correction(history,cutoff)
        if models[key] is not None:
            pairs.append({'fixtureId':row['fixture_id'],'kickoff':row['kickoff'],'target':row['target'],
                'baseline':row['forecast']['probabilities'],'availability':predict(row['forecast']['probabilities'],vector(row),models[key])})
    return pairs


def evaluate(directory, state, games, as_of):
    rows, counts = archive_rows(directory,as_of)
    selected, exclusions = select_day_ahead(rows)
    for row in selected:
        row['target'] = target(row,games,as_of)
    fitted = fit_correction(selected,as_of)
    if fitted is not None:
        models = Path(directory)/'models';models.mkdir(exist_ok=True)
        path = models/(digest(fitted)+'.json')
        if path.exists() and path.read_bytes()!=canonical(fitted):
            raise ValueError('Immutable model archive changed')
        path.write_bytes(canonical(fitted))
    forecasts = state.setdefault('forecasts',{})
    sources = {row['archive_sha256']:row for row in rows}
    if fitted is not None:
        for row in selected:
            kickoff = instant(row['kickoff'])
            if (str(row['fixture_id']) not in forecasts and as_of+timedelta(hours=24)<=kickoff<=as_of+timedelta(hours=48)):
                forecasts[str(row['fixture_id'])] = {'fixture_id':row['fixture_id'],'home':row['home'],'away':row['away'],
                    'kickoff':row['kickoff'],'generatedAt':as_of.isoformat(),'modelVersion':fitted['modelVersion'],
                    'modelSha256':digest(fitted),'snapshotSha256':row['snapshot_sha256'],'archiveSha256':row['archive_sha256'],
                    'baseline':list(row['forecast']['probabilities']),
                    'availability':predict(row['forecast']['probabilities'],vector(row),fitted),'features':vector(row)}
    prospective, pending = [],0
    for frozen in forecasts.values():
        # Restored forecasts remain immutable; evaluate against current verified outcomes.
        if instant(frozen['kickoff'])-instant(frozen['generatedAt'])<timedelta(hours=24):
            raise ValueError('A prospective forecast was saved after the day-ahead deadline')
        if instant(frozen['generatedAt'])>as_of:
            raise ValueError('A restored forecast postdates the current run')
        source = sources.get(frozen['archiveSha256'])
        if (source is None or source['fixture_id']!=frozen['fixture_id'] or source['snapshot_sha256']!=frozen['snapshotSha256']
            or source['forecast']['probabilities']!=frozen['baseline'] or vector(source)!=frozen['features']
            or instant(source['collected_at'])>instant(frozen['generatedAt'])
            or source['home']!=frozen['home'] or source['away']!=frozen['away'] or source['kickoff']!=frozen['kickoff']):
            raise ValueError('Frozen forecast differs from its original source snapshot')
        checksum = frozen.get('modelSha256','')
        if not re.fullmatch('[0-9a-f]{64}',checksum):
            raise ValueError('Invalid frozen model checksum')
        saved_model = json.loads((Path(directory)/'models'/(checksum+'.json')).read_text())
        if digest(saved_model)!=checksum or instant(saved_model['trainedAt'])>instant(frozen['generatedAt']):
            raise ValueError('Frozen forecast model identity or training time mismatch')
        if instant(saved_model['latestTrainingKickoff'])>instant(frozen['generatedAt'])-timedelta(hours=4):
            raise ValueError('Frozen model used a match inside the result information fence')
        if not np.allclose(predict(frozen['baseline'],frozen['features'],saved_model),frozen['availability'],atol=1e-12,rtol=0):
            raise ValueError('Frozen forecast differs from its archived weights')
        label = target(frozen,games,as_of)
        if label is None:
            pending += 1
        else:
            prospective.append({**frozen,'target':label})
    prospect = metrics(prospective)
    eligible = (prospect['n']>=MIN_PROSPECTIVE and prospect['delta95'] is not None
        and all(prospect['delta95'][k][1]<0 for k in ('brier','logLoss')))
    counts.update({'fplAvailableFixtures':len({r['fixture_id'] for r in rows if r['features']['home_fpl_source_available'] and r['features']['away_fpl_source_available']}),
        'minutesReadyFixtures':len({r['fixture_id'] for r in rows if vector(r) is not None}),
        'eligibleDayAheadFixtures':len(selected),'settledTrainingFixtures':sum(r['target'] is not None for r in selected),
        'lateLineupFixtures':len({r['fixture_id'] for r in rows if r['features']['home_provider_xi_received'] and r['features']['away_provider_xi_received']})})
    report = {'schemaVersion':1,'protocol':PROTOCOL,'status':'candidate' if fitted else 'collecting',
        'generatedAt':as_of.isoformat(),'baselineModelVersion':BASELINE,'featureNames':list(SHARES),
        'counts':counts,'exclusions':exclusions,'minimumTrainingMatches':MIN_TRAIN,
        'fitReason':'trained' if fitted else 'insufficient_training_matches' if counts['settledTrainingFixtures']<MIN_TRAIN
            else 'missing_outcome_classes' if len({r['target'] for r in selected if r['target'] is not None})<3 else 'no_feature_variation',
        'modelSha256':digest(fitted) if fitted else None,'chronological':metrics(chronological_pairs(selected)),
        'prospective':{'registered':len(forecasts),'pending':pending,**prospect},
        'evidenceAgainstBaselinePassed':bool(eligible),'promotionEligible':False,
        'promotedToDefault':False,'lineupsUsedInCorrection':False,
        'notes':['Old snapshots without minutes are not backfilled.',
            'Chronological checks are retrospective; only prospective forecasts were frozen before their outcomes.',
            'A lack of reports is not medical clearance. Minute shares are a role proxy, not replacement quality.',
            'Published XIs are validated and archived separately for a future late-forecast experiment.',
            'Insufficient data never creates invented correction weights.']}
    return report,fitted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    args.state_dir.mkdir(parents=True,exist_ok=True);args.output.mkdir(parents=True,exist_ok=True)
    state_path = args.state_dir/'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'schemaVersion':1,'protocol':PROTOCOL,'cursor':None,'forecasts':{}}
    if state.get('protocol')!=PROTOCOL or state.get('schemaVersion')!=1:
        raise ValueError('Invalid availability experiment state')
    as_of = datetime.now(timezone.utc)
    pages = sync_archive(args.state_dir,state)
    # Current and previous EPL seasons; no per-fixture provider queries.
    year = as_of.year-(as_of.month<7)
    seasons = {year}
    for path in (args.state_dir/'snapshots').glob('*.json'):
        fixture = json.loads(json.loads(path.read_text())['payload'])['fixture']
        day = instant(fixture['kickoff']);seasons.add(day.year-(day.month<7))
    if len(seasons)>3:
        raise ValueError('Too many seasons for a bounded daily check')
    games = {}
    for season in sorted(seasons):
        packet = read_json(API+f'/fd/competitions/PL/matches?season={season}')
        matches = packet.get('matches')
        if not isinstance(matches,list) or len(matches)>500:
            raise ValueError('Invalid match results')
        games.update({g['id']:g for g in matches})
    report,model = evaluate(args.state_dir,state,games,as_of)
    plan = json.loads((Path(__file__).resolve().parents[1]/'docs/research/availability-plan-2026-11-11.json').read_text())
    if (plan['protocol']!=PROTOCOL or plan['baselineModelVersion']!=BASELINE or plan['features']!=list(SHARES)
        or plan['ridge']!=RIDGE or plan['minimumTrainingMatches']!=MIN_TRAIN or plan['automaticPromotion'] is not False):
        raise ValueError('Registered recipe differs from the experiment')
    report['planSha256'] = digest(plan)
    report['exportPages'] = pages
    report['sourceCommit'] = __import__('os').environ.get('GITHUB_SHA')
    (args.output/'report.json').write_bytes(canonical(report))
    if model is not None:
        (args.output/'model.json').write_bytes(canonical(model))
        models = args.state_dir/'models';models.mkdir(exist_ok=True)
        path = models/(digest(model)+'.json')
        if path.exists() and path.read_bytes()!=canonical(model):
            raise ValueError('Immutable model archive changed')
        path.write_bytes(canonical(model))
    state_path.write_bytes(canonical(state))
    text = ('# Availability experiment\n\nStatus: '+report['status']+'\n\n'
        +json.dumps(report['counts'],ensure_ascii=False,indent=2)+'\n\n'
        +'Main probabilities are unchanged. Original snapshots without dated minutes are excluded from fitting.\n')
    (args.output/'report.md').write_text(text)
    print(json.dumps({'status':report['status'],'counts':report['counts'],'promotedToDefault':False}))


if __name__=='__main__':
    main()
