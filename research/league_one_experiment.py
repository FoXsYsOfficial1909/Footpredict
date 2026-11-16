"""One fixed three-division experiment on unchanged Championship forecasts."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np

from research.cross_division import PLAN as JOINT_PLAN, load_snapshot as load_joint, normalized
from research.division_model import DivisionPoisson
from research.evaluate import elo_predictions, paired_bootstrap
from research.export_model import export_elo
from research.nightly import sha
from research.quality_diagnostic import checked_probabilities, metrics, reliability

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT/'docs/research/league-one-plan-2026-11-15.json'
DIRECTORY = ROOT/'docs/research/league-one-2026-11-15'
PROTOCOL = 'league-one-research-1'
METHODS = ('three', 'joint2', 'shots', 'v1', 'base')
RULE = {'E1GamesExclusiveMaximum':20, 'E2GamesInclusiveMinimum':20, 'windowDays':1095}
RECIPE = {'half_life':730, 'ridge':.03, 'window_days':1095, 'goal_weight':.5,
          'poisson_weight':.75, 'elo':{'k':30, 'home_advantage':80, 'draw_weight':.9}}


def raw_sha(body):
    return hashlib.sha256(body).hexdigest()


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')


def validate_plan(plan):
    if (plan.get('protocol')!=PROTOCOL or plan.get('divisions')!=['E0','E1','E2']
            or plan.get('candidateCount')!=1 or plan.get('candidate')!=RECIPE
            or plan.get('subgroup')!=RULE or plan.get('automaticPromotion') is not False
            or plan.get('existingFrozenPredictionsMayChange') is not False
            or plan.get('expectedCommonMatches')!={'validation':1061,'audit':533,'monitoring':159}
            or plan.get('sourceMatches')!={'E0':4280,'E1':6252,'E2':3470}
            or plan.get('bootstrapSamples')!=1500):
        raise ValueError('Unexpected or expanded League One experiment')


def validate_combined(games, plan):
    """Check identity and information availability independently of model fitting."""
    seen, memberships = set(), {}
    cutoff=date.fromisoformat(plan['asOf'])-timedelta(days=1)
    counts={division:0 for division in plan['divisions']}
    season_counts={}
    for game in games:
        o=game.observation;m=o.match
        if (game.division not in counts or m.key in seen or m.home==m.away or not m.home or not m.away
                or m.day>=cutoff or not date(m.season,7,1)<=m.day<=date(m.season+1,7,31)
                or any(type(v) is not int or not 0<=v<=30 for v in (m.home_goals,m.away_goals))):
            raise ValueError('Duplicate, invalid, foreign or future division fixture')
        values=(o.home_shots,o.away_shots,o.home_sot,o.away_sot)
        if (any(v is not None and (type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=100) for v in values)
                or any(a is not None and b is not None and b>a for a,b in ((o.home_shots,o.home_sot),(o.away_shots,o.away_sot)))):
            raise ValueError('Invalid normalized shot statistics')
        seen.add(m.key);counts[game.division]+=1
        season_counts[game.division,m.season]=season_counts.get((game.division,m.season),0)+1
        for club in (m.home,m.away):
            key=m.season,club
            if key in memberships and memberships[key]!=game.division:
                raise ValueError('A club belongs to conflicting divisions in one season')
            memberships[key]=game.division
    if counts!=plan['sourceMatches']:
        raise ValueError('The registered division corpus count changed')
    for division,start,expected in [('E0',2015,380),('E1',2015,552),('E2',2020,552)]:
        if any(season_counts.get((division,year))!=expected for year in range(start,2026)):
            raise ValueError('A closed division season is incomplete')
    if sha(normalized([g for g in games if g.division!='E2']))!=plan['sourceJointDataSha256']:
        raise ValueError('Original E0/E1 source observations changed')
    if sha(normalized([g for g in games if g.division=='E2']))!=plan['sourceE2DataSha256']:
        raise ValueError('The pinned League One observations changed')
    return counts


def load_inputs(plan):
    validate_plan(plan)
    old=ROOT/plan['sourceJointStudy'];raw=(old/'training-data.json.gz').read_bytes()
    if (raw_sha(raw)!=plan['sourceJointCompressedSha256']
            or raw_sha((old/'report.json').read_bytes())!=plan['sourceJointReportSha256']):
        raise ValueError('The pinned earlier joint study changed')
    games,sources=load_joint(old/'training-data.json.gz',json.loads(JOINT_PLAN.read_text()))
    from research.league_one_data import load_snapshot
    path=ROOT/plan['sourceE2Snapshot']
    if raw_sha(path.read_bytes())!=plan['sourceE2CompressedSha256']:
        raise ValueError('The pinned League One compressed archive changed')
    extra,manifest=load_snapshot(path)
    games=sorted(games+extra,key=lambda g:(*g.observation.match.key,g.division))
    validate_combined(games,plan)
    return games, {'joint':sources,'leagueOne':manifest}


def load_cohorts(games,plan):
    source=ROOT/plan['sourceJointStudy']
    labels={g.observation.match.key:g.observation.match for g in games if g.division=='E1'}
    result={}
    for period,checksum in plan['sourcePredictionSha256'].items():
        rows=json.loads((source/(period+'-predictions.json')).read_text())
        if sha(rows)!=checksum or len(rows)!=plan['expectedCommonMatches'][period]:
            raise ValueError('The original paired Championship cohort changed')
        seen=set()
        for row in rows:
            key=row['date'],row['home'],row['away'];m=labels.get(key)
            if (key in seen or m is None or row['actual']!=m.outcome
                    or row['cutoff_date']!=(m.day-timedelta(days=1)).isoformat()):
                raise ValueError('Cohort identity, outcome or information cutoff changed')
            for method in ('joint','shots','v1','base'):checked_probabilities(row[method])
            seen.add(key)
        result[period]=rows
    return result


def past_counts(games,cutoff,days):
    result={}
    for game in games:
        m=game.observation.match
        if 0<(cutoff-m.day).days<=days:
            for team in (m.home,m.away):
                result.setdefault(team,{'E0':0,'E1':0,'E2':0})[game.division]+=1
    return result


def bridge_graph(games):
    histories={}
    for game in games:
        for team in (game.observation.match.home,game.observation.match.away):
            histories.setdefault(team,set()).add(game.division)
    edges={a+'-'+b:sorted(t for t,seen in histories.items() if a in seen and b in seen)
           for a,b in [('E0','E1'),('E0','E2'),('E1','E2')]}
    visited={'E0'}
    for _ in range(3):
        for pair,clubs in edges.items():
            a,b=pair.split('-')
            if clubs and (a in visited or b in visited):visited.update((a,b))
    if visited!={'E0','E1','E2'}:
        raise ValueError('No earlier club history connects all three divisions')
    return edges


def eligible(histories,rule=RULE):
    return any(v['E1']<rule['E1GamesExclusiveMaximum'] and v['E2']>=rule['E2GamesInclusiveMinimum']
               for v in histories.values())


def forecast(games,rows,plan):
    recipe=plan['candidate'];settings={k:recipe[k] for k in ('half_life','ridge','window_days','goal_weight')}
    model=DivisionPoisson(**settings);previous=None
    e1=[g.observation.match for g in games if g.division=='E1']
    ratings=elo_predictions(e1,rows,recipe['elo']);result=[];fits=[];counts={}
    for row,rating in zip(rows,ratings):
        cutoff=date.fromisoformat(row['cutoff_date'])
        if cutoff!=date.fromisoformat(row['date'])-timedelta(days=1) or (previous and cutoff<previous):
            raise ValueError('Three-division forecasts require chronological conservative cutoffs')
        if cutoff!=previous:
            consumed=[g for g in games if 0<(cutoff-g.observation.match.day).days<=model.window_days]
            edges=bridge_graph(consumed);model.fit(games,cutoff)
            if model.divisions!=['E0','E1','E2'] or model.fit_info['latestTrainingDate']>=cutoff.isoformat():
                raise ValueError('Invalid division scope or a future training observation')
            counts=past_counts(consumed,cutoff,model.window_days)
            fits.append({'cutoffExclusive':cutoff.isoformat(),**model.fit_info,
                'divisionParameters':model.export()['divisions'],'bridgeGraph':edges,
                'trainingSha256':sha(normalized(consumed))})
            previous=cutoff
        prediction=model.predict('E1',row['home'],row['away'],date.fromisoformat(row['date']))
        weight=recipe['poisson_weight'];probability=(weight*prediction['probabilities']+(1-weight)*np.array(rating)).tolist()
        histories={club:counts[club] for club in (row['home'],row['away'])}
        if 'pastClubGames' in row and any(histories[t][d]!=v[d] for t,v in row['pastClubGames'].items() for d in ('E0','E1')):
            raise ValueError('The earlier E0/E1 history count changed')
        result.append({**row,'joint2':list(row['joint']),'three':probability,'elo':rating,
            'pastClubGames':histories,'lowELCHistoryWithL1':eligible(histories,plan['subgroup']),
            'threeExpectedGoals':[prediction['home_goals'],prediction['away_goals']],
            'latestTrainingDate':model.fit_info['latestTrainingDate'],'trainingSha256':fits[-1]['trainingSha256']})
    return result,fits


def score_block(rows,plan):
    group=[r for r in rows if r['lowELCHistoryWithL1']]
    comparisons=('joint2','shots','v1')
    return {'matches':len(rows),'metrics':{k:metrics(rows,k) for k in METHODS},
        'calibration':{k:reliability(rows,k) for k in METHODS},
        'pairedIntervals':{k:paired_bootstrap(rows,'three',k,samples=plan['bootstrapSamples']) for k in comparisons},
        'lowELCHistoryWithL1':{'matches':len(group),'evidence':'no_applicable_fixtures' if not group else 'retrospective_only',
            'metrics':{k:metrics(group,k) for k in METHODS},
            'calibration':{k:reliability(group,k) for k in METHODS},
            'pairedIntervals':{k:paired_bootstrap(group,'three',k,samples=plan['bootstrapSamples']) if group else None for k in comparisons}}}


def gates(results):
    against_joint=all(results[p]['metrics']['three'][m]<results[p]['metrics']['joint2'][m]
                      for p in results for m in ('logLoss','brier'))
    late=all(results[p]['metrics']['three'][m]<results[p]['metrics'][ref][m]
             for p in ('audit','monitoring') for ref in ('shots','v1') for m in ('logLoss','brier'))
    interval=results['audit']['pairedIntervals']['joint2']['logloss']['ci95'][1]<0
    return bool(against_joint and late), bool(against_joint and late and interval)


def code_sha():
    names=('research/league_one_experiment.py','research/league_one_data.py','research/division_model.py',
           'research/models.py','research/evaluate.py','research/performance.py','research/data.py',
           'research/quality_diagnostic.py','research/export_model.py','requirements-research.txt')
    return sha({name:raw_sha((ROOT/name).read_bytes()) for name in names})


def markdown(report):
    lines=['# История League One для Чемпионшипа','',
        'Один фиксированный вариант использует АПЛ, Чемпионшип и League One вместе: общие силы клубов, '
        'отдельные средние голов, домашнее преимущество и голевой масштаб ударов каждой лиги. '
        'Прежний Elo Чемпионшипа и все настройки сохранены.','',
        '| Период | Общих матчей | Модель | Log Loss ↓ | Brier ↓ | Совпал исход |',
        '| --- | ---: | --- | ---: | ---: | ---: |']
    for period,block in report['results'].items():
        for key in METHODS:
            m=block['metrics'][key];accuracy='—' if m['uniqueTopAccuracy'] is None else f"{m['uniqueTopAccuracy']:.2%}"
            lines.append(f"| {period} | {m['n']} | {key} | {m['logLoss']:.9f} | {m['brier']:.9f} | {accuracy} |")
    lines+=['','three — три дивизиона; joint2 — точный сохранённый вариант АПЛ/Чемпионшип; '
        'shots — прежний кандидат только по E1; v1 и base — неизменные контрольные вероятности.','',
        '## Заранее описанная группа с малой историей E1','',
        'У хотя бы одного клуба меньше 20 прошлых игр E1 и минимум 20 E2 в окне 1 095 дней. '
        'Группа только описательная, её результаты не выбирают модель.','',
        '| Период | Применимых | Модель | Log Loss ↓ | Brier ↓ | Совпал исход |',
        '| --- | ---: | --- | ---: | ---: | ---: |']
    for period,block in report['results'].items():
        group=block['lowELCHistoryWithL1']
        for key in METHODS:
            m=group['metrics'][key];ll='—' if m['logLoss'] is None else f"{m['logLoss']:.9f}"
            bs='—' if m['brier'] is None else f"{m['brier']:.9f}";acc='—' if m['uniqueTopAccuracy'] is None else f"{m['uniqueTopAccuracy']:.2%}"
            lines.append(f"| {period} | {m['n']} | {key} | {ll} | {bs} | {acc} |")
    lines+=['',f"Критерий средних ошибок: **{report['pointGatePassed']}**. Исторический критерий: **{report['historicalGatePassed']}**.",
        'Независимое преимущество не установлено. Основная модель и ранее зарегистрированные прогнозы неизменны.','',
        'Каждый прогноз использует только результаты и статистику до предыдущего календарного дня. '
        'Проверяется связь трёх дивизионов через прошлые клубные переходы. Средняя результативность лиги '
        'не объявляется прямой оценкой разницы класса. Удары в створ не объявляются измеренным xG.','',
        'Brier — средняя сумма трёх квадратов ошибок, шкала 0–2. Равные максимумы отдельно отмечены; '
        'они остаются в Log Loss и Brier и исключаются только из unique-top accuracy.','',
        'Все исторические периоды ранее изучались. Дополнительные игры источника — не дополнительные '
        'проверенные будущие матчи. Пустая подгруппа не является подтверждением преимущества.','',
        '## Текущая прошлая история клубов','',
        '| Клуб | E0 | E1 | E2 |','| --- | ---: | ---: | ---: |']
    for club,counts in report['currentELCClubHistory'].items():
        lines.append(f"| {club} | {counts['E0']} | {counts['E1']} | {counts['E2']} |")
    lines+=['','## Воспроизведение без сети','',
        '```bash','OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.league_one_experiment \\',
        '  --output /tmp/footpredict-league-one','```','']
    return '\n'.join(lines)


def execute(output=DIRECTORY,plan_path=PLAN):
    output=Path(output).resolve();research=(ROOT/'docs/research').resolve()
    if (output==research or research in output.parents) and output!=DIRECTORY.resolve():
        raise ValueError('Refusing to overwrite another frozen research study')
    plan_path=Path(plan_path);plan=json.loads(plan_path.read_text());validate_plan(plan)
    games,sources=load_inputs(plan);cohorts=load_cohorts(games,plan)
    output.mkdir(parents=True,exist_ok=True)
    started=time.monotonic();raw=json.dumps(normalized(games),ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
    compressed=gzip.compress(raw,mtime=0);(output/'training-data.json.gz').write_bytes(compressed)
    manifest={'schemaVersion':1,'protocol':PROTOCOL,'planSha256':sha(plan),'dataSha256':sha(normalized(games)),
        'compressedSha256':raw_sha(compressed),'uncompressedSha256':raw_sha(raw),'sources':sources,'sourceMatches':plan['sourceMatches']}
    dump(output/'input-manifest.json',manifest)
    results,predictions,fits={},{},{}
    for period,rows in cohorts.items():
        predicted,diagnostics=forecast(games,rows,plan)
        results[period]=score_block(predicted,plan);predictions[period]=predicted;fits[period]=diagnostics
    point,historical=gates(results);cutoff=date.fromisoformat(plan['asOf'])-timedelta(days=1)
    settings={k:plan['candidate'][k] for k in ('half_life','ridge','window_days','goal_weight')}
    final=DivisionPoisson(**settings).fit(games,cutoff)
    consumed=[g for g in games if 0<(cutoff-g.observation.match.day).days<=final.window_days]
    edges=bridge_graph(consumed);e1=[g.observation.match for g in games if g.division=='E1']
    model={'protocol':PROTOCOL,'status':'experimental','as_of':plan['asOf'],'target_division':'E1',
        'training_cutoff_exclusive':cutoff.isoformat(),'joint':final.export(),
        'elo':export_elo(e1,cutoff,plan['candidate']['elo']),'poisson_weight':plan['candidate']['poisson_weight'],
        'data_sha256':manifest['dataSha256'],'plan_sha256':sha(plan),'historical_gate_passed':historical,'promoted_to_default':False}
    model['model_version']='league-one-experimental-'+plan['asOf']+'-'+sha(model)[:12]
    counts=past_counts(consumed,cutoff,final.window_days)
    clubs=sorted({t for m in e1 if m.season==2026 for t in (m.home,m.away)})
    report={'schemaVersion':1,'protocol':PROTOCOL,'asOf':plan['asOf'],'completedAt':datetime.now(timezone.utc).isoformat(),
        'planSha256':sha(plan),'planRawSha256':raw_sha(plan_path.read_bytes()),'codeSha256':code_sha(),
        'dataSha256':manifest['dataSha256'],'sourceMatches':plan['sourceMatches'],'candidateCount':1,
        'results':results,'pointGatePassed':point,'historicalGatePassed':historical,
        'currentELCClubHistory':{club:counts[club] for club in clubs},'finalBridgeGraph':edges,
        'fitCounts':{k:len(v) for k,v in fits.items()},'modelSha256':sha(model),'modelVersion':model['model_version'],
        'predictionSha256':{k:sha(v) for k,v in predictions.items()},'fitsSha256':sha(fits),
        'durationSeconds':time.monotonic()-started,'retrospective':True,'independentAdvantageEstablished':False,
        'promotedToDefault':False,'prospectiveValidationRequired':True,'existingFrozenPredictionsChanged':False,
        'limitations':plan['limitations']}
    dump(output/'report.json',report);dump(output/'model.json',model)
    for name,value in [('fits',fits),*[(period+'-predictions',rows) for period,rows in predictions.items()]]:
        (output/(name+'.json')).write_text(json.dumps(value,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n')
    (output/'report.md').write_text(markdown(report))
    print(json.dumps({'sourceMatches':report['sourceMatches'],'pointGatePassed':point,'historicalGatePassed':historical,
        'seconds':report['durationSeconds'],'currentClubHistory':{k:counts[k] for k in ('Bolton','Lincoln') if k in counts},
        'results':{k:{'metrics':{m:v['metrics'][m] for m in METHODS},'subgroup':v['lowELCHistoryWithL1']['matches']} for k,v in results.items()}},ensure_ascii=False))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=DIRECTORY)
    execute(parser.parse_args().output)
