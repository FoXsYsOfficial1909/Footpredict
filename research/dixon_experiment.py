"""One prespecified dependence parameter learned from past rolling rate pairs."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import time

from research.data import ALIASES, Match
from research.evaluate import elo_predictions, paired_bootstrap, reference_rows
from research.export_model import export_elo
from research.models import LearnedPoisson
from research.nightly import sha
from research.quality_diagnostic import metrics, reliability

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT/'docs/research/dixon-oof-plan-2026-11-15.json'
DIRECTORY = ROOT/'docs/research/dixon-oof-2026-11-15'
PROTOCOL = 'dixon-oof-1'
METHODS = ('selected', 'fitted', 'uncorrected', 'v1', 'base')


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n')


def raw_sha(body):
    return hashlib.sha256(body).hexdigest()


def validate_plan(plan):
    if (plan.get('protocol') != PROTOCOL or plan.get('league') != 'PL' or plan.get('division') != 'E0'
            or plan.get('maximumCandidates') != 2
            or plan.get('candidates') != ['independence_rho_zero','one_oof_fitted_rho']
            or plan.get('rhoTrainingSeasons') != [2018,2019,2020,2021,2022]
            or plan.get('validationSeasons') != [2023,2024] or plan.get('auditSeasons') != [2025]
            or plan.get('monitoringSeasons') != [2026]
            or plan.get('rhoFitCutoffExclusive') != '2023-07-01'
            or plan.get('rhoBounds') != [-.15,.039999]
            or plan.get('rhoHalfLifeDays') != 365 or plan.get('rhoWindowDays') != 1826
            or plan.get('minimumRhoMatches') != 500
            or plan.get('glm') != {'half_life':365,'ridge':.03,'window_days':1095}
            or plan.get('elo') != {'k':30,'home_advantage':80,'draw_weight':.9}
            or plan.get('glmWeight') != .75 or plan.get('automaticPromotion') is not False
            or plan.get('existingFrozenPredictionsMayChange') is not False):
        raise ValueError('Unexpected or expanded Dixon-Coles experiment')


def load_inputs(plan, root=ROOT):
    root = Path(root); validate_plan(plan)
    compressed = (root/plan['sourceArchive']).read_bytes()
    if len(compressed)>2_000_000 or raw_sha(compressed)!=plan['sourceCompressedSha256']:
        raise ValueError('The pinned historical archive changed')
    body = gzip.decompress(compressed)
    if len(body)>6_000_000 or raw_sha(body)!=plan['sourceRawSha256']:
        raise ValueError('The historical result archive checksum changed')
    settings_raw = (root/plan['settingsSource']).read_bytes()
    if raw_sha(settings_raw)!=plan['settingsRawSha256']:
        raise ValueError('The frozen strength recipe changed')
    settings = json.loads(settings_raw)
    if (settings['glm']!=plan['glm'] or settings['elo']!=plan['elo']
            or settings['glm_weight']!=plan['glmWeight']):
        raise ValueError('The strength recipe does not match the plan')
    matches=[]; seen=set()
    for row in json.loads(body):
        if row[0]!='E0':continue
        _, day, season, home, away, hg, ag = row[:7]
        day=date.fromisoformat(day); key=(day.isoformat(), home, away)
        if (key in seen or type(season) is not int or not home or not away or home==away
                or not date(season,7,1)<=day<=date(season+1,7,31)
                or any(type(v) is not int or not 0<=v<=30 for v in (hg,ag))):
            raise ValueError('Invalid historical match identity or regulation score')
        seen.add(key); matches.append(Match(day,season,home,away,hg,ag))
    matches.sort(key=lambda m:m.key)
    counts={year:sum(m.season==year for m in matches) for year in range(2015,2027)}
    if (len(matches)!=plan['expectedSourceMatches'] or any(counts[y]!=380 for y in range(2015,2026))
            or counts[2026]!=100 or len(seen)!=len(matches)):
        raise ValueError('Changed historical league coverage')
    return matches, settings, counts


def rate_rows(matches, seasons, settings):
    """Reset strength warm starts per period; never fit a target's score."""
    model=LearnedPoisson(**settings); rows=[]; fits=[]; last=None
    targets=sorted((m for m in matches if m.season in seasons),key=lambda m:m.key)
    for target in targets:
        cutoff=target.day-timedelta(days=1)
        if cutoff!=last:
            model.fit(matches,cutoff); last=cutoff
            consumed=[m for m in matches if 0<(cutoff-m.day).days<=settings['window_days']]
            fits.append({'cutoffExclusive':cutoff.isoformat(), **model.fit_info,
                'trainingSha256':sha([(m.day.isoformat(),m.season,m.home,m.away,m.home_goals,m.away_goals) for m in consumed])})
        prediction=model.predict(target.home,target.away,target.day)
        if model.fit_info['latest_training_date']>=cutoff.isoformat():
            raise ValueError('Strength fitting crossed the chronological fence')
        rows.append({'date':target.day.isoformat(),'season':target.season,'home':target.home,'away':target.away,
            'home_goals':target.home_goals,'away_goals':target.away_goals,'actual':target.outcome,
            'home_rate':prediction['home_goals'],'away_rate':prediction['away_goals'],
            'rate_cutoff':cutoff.isoformat(),'unseenTeams':prediction['unseen_teams'],
            'latestTrainingDate':model.fit_info['latest_training_date'],'trainingSha256':fits[-1]['trainingSha256']})
    return rows,fits


def predictions(matches, seasons, plan, rho):
    from research.dixon_coles import probabilities
    rates,fits=rate_rows(matches,seasons,plan['glm'])
    targets=reference_rows(matches,set(seasons)); ratings=elo_predictions(matches,targets,plan['elo'])
    if len(rates)!=len(targets):raise ValueError('Different chronological prediction cohorts')
    rows=[]; weight=plan['glmWeight']
    for rate,reference,elo in zip(rates,targets,ratings):
        if any(rate[k]!=reference[k] for k in ('date','season','home','away','actual')):
            raise ValueError('Different paired match identity')
        uncorrected=probabilities(rate['home_rate'],rate['away_rate'],0)
        corrected=probabilities(rate['home_rate'],rate['away_rate'],rho)
        rows.append({**reference,**rate,'cutoff_date':reference['cutoff_date'],'elo':elo,
            'uncorrected':[weight*a+(1-weight)*b for a,b in zip(uncorrected,elo)],
            'fitted':[weight*a+(1-weight)*b for a,b in zip(corrected,elo)],'rhoFitted':rho})
    return rows,fits


def select(rows, rho, plan):
    validate_plan(plan)
    common=[r for r in rows if r['v1'] is not None]
    if not common:raise ValueError('No shared validation fixtures')
    zero,corrected=metrics(common,'uncorrected'),metrics(common,'fitted')
    chosen=corrected['logLoss']<zero['logLoss']
    return {'schemaVersion':1,'protocol':PROTOCOL,'planSha256':sha(plan),
        'fittedRho':rho,'selectedRho':rho if chosen else 0.,
        'selectedMethod':'one_oof_fitted_rho' if chosen else 'independence_rho_zero',
        'validationMatches':len(common),'excludedFromEveryModel':len(rows)-len(common),
        'trials':[{'rho':0.,'method':'independence_rho_zero','metrics':zero},
                  {'rho':rho,'method':'one_oof_fitted_rho','metrics':corrected}],
        'validationDataSha256':sha(rows),'chosenBeforeLaterMetrics':True,
        'promotedToDefault':False,'prospectiveValidationRequired':True}


def evaluate(rows, selection, plan):
    rows=[{**r,'selected':list(r['fitted'] if selection['selectedMethod']=='one_oof_fitted_rho' else r['uncorrected'])}
          for r in rows]
    common=[r for r in rows if r['v1'] is not None]
    excluded=[{'date':r['date'],'home':r['home'],'away':r['away'],
               'reason':'insufficient_past_home_or_away_history_for_v1'} for r in rows if r['v1'] is None]
    result={'completedMatches':len(rows),'commonMatches':len(common),'excludedFromEveryModel':len(excluded),
        'coverage':len(common)/len(rows) if rows else None,'from':rows[0]['date'],'to':rows[-1]['date'],
        'metrics':{k:metrics(common,k) for k in METHODS},
        'calibration':{k:reliability(common,k) for k in METHODS},
        'fullCoverageMetrics':{k:metrics([r for r in rows if r[k] is not None],k) for k in METHODS},
        'excludedFixtures':excluded,
        'unseenTeamFixtures':sum(bool(r['unseenTeams']) for r in rows),
        'pairedIntervals':{k:paired_bootstrap(common,'selected',k,samples=plan['bootstrapSamples'])
                           for k in ('uncorrected','v1','base')}}
    return result,rows


def research_gate(results,selection):
    selected=selection['selectedMethod']=='one_oof_fitted_rho'
    point=selected and all(results[p]['metrics']['selected'][m]<results[p]['metrics'][r][m]
        for p in ('audit','monitoring') for m in ('brier','logLoss') for r in ('uncorrected','v1'))
    interval=results['audit']['pairedIntervals']['uncorrected']['logloss']['ci95'][1]<0
    return bool(point and interval)


def markdown(report):
    selection=report['selection'];rho=report['rhoModel']['rho']
    lines=['# Один параметр Dixon–Coles по прошлым прогнозам','',
        'Поправка оценена по прежним прогнозам интенсивности голов, рассчитанным до соответствующих игр. '
        'Это дальнейшая разработка на известной истории, а не первый опыт Dixon–Coles и не независимое подтверждение.', '',
        f"Раннее обучение rho: {report['rhoModel']['fit']['training_games']} игр сезонов 2018/19–2022/23. "
        f"Получено rho = {rho:.8f}. На валидации выбран `{selection['selectedMethod']}` "
        f"(rho = {selection['selectedRho']:.8f}); выбор сохранён до расчёта поздних метрик.", '',
        'Выбранный `selected` совпадает с `fitted`, если поправка выбрана, иначе с `uncorrected`. '
        'Это одна и та же выбранная модель, а не дополнительное независимое подтверждение.', '',
        '| Период | Всего | Общих | Исключено у всех | Модель | Попадания | Log Loss ↓ | Brier ↓ |',
        '| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |']
    for period,block in report['results'].items():
        for name in ('selected','fitted','uncorrected','v1','base'):
            m=block['metrics'][name]; accuracy=m['uniqueTopAccuracy']
            shown=f'{accuracy:.2%}' if accuracy is not None else '—'
            lines.append(f"| {period} | {block['completedMatches']} | {m['n']} | {block['excludedFromEveryModel']} | "
                         f"{name} | {shown} | {m['logLoss']:.9f} | {m['brier']:.6f} |")
    lines += ['', 'Ничьи: средняя вероятность / фактическая частота на общей выборке.', '',
        '| Период | Модель | Оценка ничьей | Частота ничьих | ECE ничьих ↓ |',
        '| --- | --- | ---: | ---: | ---: |']
    for period,block in report['results'].items():
        for name in ('selected','uncorrected','v1'):
            value=block['calibration'][name]['X']
            lines.append(f"| {period} | {name} | {value['meanProbability']:.2%} | {value['observedFrequency']:.2%} | {value['ece']:.6f} |")
    lines += ['', f"Описательный исторический критерий: **{report['researchGatePassed']}**. "
        'Независимое преимущество не установлено. Основная модель и все зафиксированные прогнозы неизменны.', '',
        'Положительный rho уменьшает вероятности 0:0 и 1:1 относительно независимого Пуассона. '
        'Знак получен из ранних данных, не задан по наблюдаемой частоте ничьих позднего периода. '
        'Совпадение общей частоты ничьих не является самостоятельным критерием качества.', '',
        'Brier — средняя сумма трёх квадратов ошибок, шкала 0–2; Log Loss — натуральный отрицательный логарифм '
        'вероятности фактического исхода. Для обеих ошибок меньше лучше. Равные максимумы отдельно отмечаются '
        'и исключаются только из знаменателя unique-top accuracy. Полный охват и список пропусков сохранены.', '',
        'Все варианты используют одинаковую оценку силы клубов и один Elo; различается только зависимость четырёх '
        'низких счетов. Точная сила пересчитывается по прошлым играм перед каждой датой. Поправка rho обучена один '
        'раз на ранних OOF-прогнозах; поздние результаты не меняют её. Глобальная область rho сохраняет положительность '
        'при всех допустимых интенсивностях, без отдельного изменения параметра для каждого матча.', '',
        '95% интервал отличия Log Loss выбранного варианта от uncorrected на периоде audit:', '',
        '```json',json.dumps(report['results']['audit']['pairedIntervals']['uncorrected']['logloss'],indent=2),'```','',
        'Ограничения:', '']
    lines.extend('- '+s for s in report['limitations'])
    lines += ['', '## Воспроизведение без сети', '', '```bash',
        'OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -m research.dixon_experiment \\',
        '  --output /tmp/footpredict-dixon-oof', '```','']
    return '\n'.join(lines)


def execute(output=DIRECTORY,plan_path=PLAN):
    from research.dixon_coles import fit_rho
    plan_path=Path(plan_path);plan=json.loads(plan_path.read_text());validate_plan(plan)
    output=Path(output).resolve()
    # This fresh experiment must never overwrite one of the older registered studies.
    protected=(ROOT/'docs/research').resolve()
    if (output==protected or protected in output.parents) and output!=DIRECTORY.resolve():
        raise ValueError('Refusing to overwrite another frozen research study')
    output.mkdir(parents=True,exist_ok=True);started=time.monotonic()
    matches,settings,counts=load_inputs(plan)
    early,fits_early=rate_rows(matches,plan['rhoTrainingSeasons'],plan['glm'])
    rho_model=fit_rho(early,date.fromisoformat(plan['rhoFitCutoffExclusive']),
        half_life=plan['rhoHalfLifeDays'],window_days=plan['rhoWindowDays'],
        bounds=tuple(plan['rhoBounds']),minimum_matches=plan['minimumRhoMatches'])
    rho=rho_model['rho']
    validation,fits_validation=predictions(matches,plan['validationSeasons'],plan,rho)
    selection=select(validation,rho,plan)
    dump(output/'selection.json',selection)
    # No later-period predictions or metrics are produced until that choice is persisted.
    results={};saved={};fits={'rho_training':fits_early,'validation':fits_validation}
    results['validation'],saved['validation']=evaluate(validation,selection,plan)
    for label,key in [('audit','auditSeasons'),('monitoring','monitoringSeasons')]:
        rows,fits[label]=predictions(matches,plan[key],plan,rho)
        results[label],saved[label]=evaluate(rows,selection,plan)
    cutoff=date.fromisoformat(plan['registeredOn'])-timedelta(days=1)
    model=LearnedPoisson(**plan['glm']).fit(matches,cutoff)
    final={'protocol':PROTOCOL,'status':'experimental','league':'PL','division':'E0',
        'as_of':plan['registeredOn'],'training_cutoff_exclusive':cutoff.isoformat(),
        'glm':model.export(),'elo':export_elo(matches,cutoff,plan['elo']),'glm_weight':plan['glmWeight'],
        'rho':selection['selectedRho'],'rho_fitted':rho,'rho_model':rho_model,'aliases':ALIASES,
        'features_used':['past_match_results','opponent_strength','home_advantage','past_only_score_dependence'],
        'news_used_in_probabilities':False,'promoted_to_default':False}
    final['model_version']='dixon-oof-candidate-'+plan['registeredOn']+'-'+sha(final)[:12]
    report={'schemaVersion':1,'protocol':PROTOCOL,'planSha256':sha(plan),'planRawSha256':raw_sha(plan_path.read_bytes()),
        'codeSha256':sha({name:raw_sha((ROOT/name).read_bytes()) for name in
            ('research/dixon_experiment.py','research/dixon_coles.py','research/models.py','research/evaluate.py',
             'research/quality_diagnostic.py','research/export_model.py','requirements-research.txt')}),
        'sourceCompressedSha256':plan['sourceCompressedSha256'],'sourceRawSha256':plan['sourceRawSha256'],
        'sourceMatches':len(matches),'sourceCountsBySeason':counts,'rhoModel':rho_model,
        'rhoTrainingRowsSha256':sha(early),'selection':selection,'selectionSha256':sha(selection),
        'results':results,'researchGatePassed':research_gate(results,selection),
        'retrospective':True,'independentAdvantageEstablished':False,'promotedToDefault':False,
        'prospectiveValidationRequired':True,'existingFrozenPredictionsChanged':False,
        'modelSha256':sha(final),'predictionSha256':{k:sha(v) for k,v in saved.items()},
        'fitsSha256':sha(fits),'durationSeconds':time.monotonic()-started,'limitations':plan['limitations']}
    compressed=gzip.compress(json.dumps(early,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode(),mtime=0)
    (output/'rho-training-rows.json.gz').write_bytes(compressed)
    report['rhoTrainingCompressedSha256']=raw_sha(compressed)
    for name,value in [('report.json',report),('model.json',final),('fits.json',fits)]:dump(output/name,value)
    for name,rows in saved.items():
        (output/(name+'-predictions.json')).write_text(json.dumps(rows,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n')
    (output/'report.md').write_text(markdown(report))
    print(json.dumps({'rho':rho,'selectedRho':selection['selectedRho'],'researchGatePassed':report['researchGatePassed'],
        'seconds':report['durationSeconds'],'results':{k:{'common':b['commonMatches'],'metrics':{m:b['metrics'][m] for m in ('selected','uncorrected','v1','base')}} for k,b in results.items()}}))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=DIRECTORY)
    execute(parser.parse_args().output)
