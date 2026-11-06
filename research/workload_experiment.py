"""Registered workload residual experiment over a fixed rolling xG baseline."""
from __future__ import annotations

import argparse
from bisect import bisect_left
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path

import numpy as np

from research.accuracy import dump
from research.evaluate import reference_rows, elo_predictions, with_predictions, summarize, paired_bootstrap
from research.nightly import sha
from research.performance import load_performance
from research.workload import normalize_event, workload_state
from research.workload_data import verify_reference
from research.workload_model import adjust, fit_workload
from research.xg_data import load_xg_matches, merge_xg
from research.xg_model import ExpectedGoalsPoisson

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'docs/research/workload-plan-2026-11-06.json'
IMPLEMENTATION = ('research/workload_experiment.py', 'research/workload_model.py',
    'research/workload.py', 'research/workload_data.py', 'research/xg_data.py',
    'research/xg_model.py', 'research/performance.py', 'research/models.py',
    'research/evaluate.py', 'research/data.py', 'requirements-research.txt')


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_event_index(events):
    """Validate globally once, then index only played events by club/date."""
    teams, seen, sources = defaultdict(list), set(), set()
    for value in events:
        event = normalize_event(value)
        key = (event['date'], event['home'], event['away'])
        if key in seen:
            raise ValueError('Duplicate workload fixture')
        seen.add(key)
        if event['source_id'] is not None:
            source = (event['competition'], str(event['source_id']))
            if source in sources:
                raise ValueError('Duplicate workload source fixture ID')
            sources.add(source)
        if event['played']:
            teams[event['home']].append(event)
            teams[event['away']].append(event)
    result = {}
    for team, games in teams.items():
        games.sort(key=lambda event: (event['date'], event['home'], event['away']))
        result[team] = {'dates': [event['date'] for event in games], 'events': games}
    return result


def feature_rows(rows, event_index, coverage):
    # Retrieval provenance stays in the full manifest, not in each feature hash.
    scopes = {key: coverage.get(key) for key in ('requiredCompetitions', 'requiredCompetitionsVerified', 'scopes')}
    result = []
    for row in rows:
        target = date.fromisoformat(row['date'])
        first, cutoff = (target - timedelta(days=14)).isoformat(), (target - timedelta(days=1)).isoformat()
        selected = {}
        for team in (row['home'], row['away']):
            index = event_index.get(team, {'dates': [], 'events': []})
            left, right = bisect_left(index['dates'], first), bisect_left(index['dates'], cutoff)
            for event in index['events'][left:right]:
                selected[(event['date'], event['home'], event['away'])] = event
        state = workload_state(selected.values(), row['home'], row['away'], target, coverage=scopes)
        values = state['vector']
        keys = ('restDifference', 'matches7Difference', 'matches14Difference')
        vector = [values[keys[0]] / 7, -values[keys[1]] / 3, -values[keys[2]] / 5] if all(values[key] is not None for key in keys) else None
        details = {'complete': vector is not None, 'homeComplete': state['home']['coverage']['allCompetitionsComplete'],
            'awayComplete': state['away']['coverage']['allCompetitionsComplete'],
            'requiredCompetitionsVerified': scopes['requiredCompetitionsVerified'] is True,
            'missingCompetitions': sorted(set(state['home']['coverage']['missingCompetitions'] + state['away']['coverage']['missingCompetitions'])),
            'unknownMinutesGamesLast14Days': sum(state[side]['unknownMinutesGamesLast14Days'] for side in ('home', 'away')),
            'lastKnownMatches': {side: state[side]['lastKnownMatch'] for side in ('home', 'away')},
            'observedLowerBounds': {side: state[side]['observedLowerBounds'] for side in ('home', 'away')}}
        result.append({**row, 'workload': vector, 'workloadCoverage': details})
    return result


def prepare(observations, event_index, coverage, plan, output, fingerprint):
    cache = output / 'feature-cache.json'
    if cache.exists():
        previous = json.loads(cache.read_text())
        if previous.get('fingerprint') == fingerprint and previous.get('rowsSha256') == sha(previous.get('rows')):
            print('CACHE_HIT', flush=True)
            return previous['rows']
    matches = [row.match for row in observations]
    years = set(range(2020, max(plan['seasons']) + 1))
    rows = reference_rows(matches, years)
    settings = plan['baseline']
    model = ExpectedGoalsPoisson(goal_weight=settings['goalWeight'], half_life=settings['halfLife'],
                                ridge=settings['ridge'], window_days=settings['windowDays'])
    values, previous = [], None
    print('PREPARING_FIXED_XG_OOF', len(rows), flush=True)
    for row in rows:
        cutoff = date.fromisoformat(row['cutoff_date'])
        if cutoff != previous:
            model.fit(observations, cutoff)
            previous = cutoff
        values.append(model.predict(row['home'], row['away'], date.fromisoformat(row['date']))['probabilities'])
    elo = elo_predictions(matches, rows, settings['elo'])
    for row, poisson, rating in zip(rows, values, elo):
        row['baseline'] = (settings['poissonWeight'] * poisson + (1 - settings['poissonWeight']) * np.array(rating)).tolist()
    rows = feature_rows(rows, event_index, coverage)
    dump(cache, {'fingerprint': fingerprint, 'rowsSha256': sha(rows), 'rows': rows})
    return rows


def predict_correction(arguments):
    examples, rows, settings = arguments
    result, models, month, model = [], [], None, None
    for row in rows:
        if row['date'][:7] != month:
            model = fit_workload(examples, date.fromisoformat(row['cutoff_date']), ridge=settings['ridge'],
                                half_life=settings['halfLife'], window_days=settings['windowDays'])
            month = row['date'][:7]
            models.append({'month': month, **model})
        result.append({**row, 'challenger': adjust(row['baseline'], row['workload'], model),
                       'correctionStatus': model['status']})
    common = [row for row in result if row['v1'] is not None]
    return {'id': settings['id'], 'settings': settings, 'metrics': summarize(common, 'challenger'),
            'coveredTargets': sum(row['workload'] is not None for row in result),
            'changedPredictions': sum(row['challenger'] != row['baseline'] for row in result),
            'bySeason': {str(year): summarize([row for row in common if row['season'] == year], 'challenger')
                         for year in sorted({row['season'] for row in common})}, 'rows': result, 'models': models}


def compact(result):
    return {key: value for key, value in result.items() if key not in ('rows', 'models')}


def coverage_summary(rows):
    result = {}
    for year in sorted({row['season'] for row in rows}):
        season = [row for row in rows if row['season'] == year]
        result[str(year)] = {'targets': len(season), 'common': sum(row['v1'] is not None for row in season),
            'completeWorkloadRows': sum(row['workload'] is not None for row in season),
            'missingWorkloadRows': sum(row['workload'] is None for row in season),
            'rowsWithUnknownMinutes': sum(row['workloadCoverage']['unknownMinutesGamesLast14Days'] > 0 for row in season)}
    return result


def execute(data_dir, xg_matches, xg_manifest, events, coverage, output,
            plan_path=PLAN, phase='all', workers=1):
    output = Path(output)
    if phase not in ('all', 'selection', 'audit') or workers not in (1, 2):
        raise ValueError('Invalid experiment phase or worker count')
    output.mkdir(parents=True, exist_ok=True)
    plan = json.loads(Path(plan_path).read_text())
    observations, csv_sources = load_performance(data_dir, plan['seasons'])
    if any(source['rows'] != 380 for source in csv_sources if source['season'] < max(plan['seasons'])):
        raise ValueError('A closed EPL season must contain 380 verified results')
    xg_source, workload_source = json.loads(Path(xg_manifest).read_text()), json.loads(Path(coverage).read_text())
    xg_digest, events_digest = file_sha(xg_matches), file_sha(events)
    if xg_source.get('normalizedMatchesSha256') != xg_digest:
        raise ValueError('xG source manifest does not bind normalized match bytes')
    if workload_source.get('normalizedMatchesSha256') != events_digest:
        raise ValueError('Workload source manifest does not bind normalized event bytes')
    observations, joined = merge_xg(observations, load_xg_matches(xg_matches))
    event_rows = json.loads(Path(events).read_text())
    if not isinstance(event_rows, list):
        raise ValueError('Normalized workload events must be a list')
    reference = verify_reference(event_rows, observations)
    if reference['conflicts']:
        raise ValueError('Workload EPL fixtures disagree with the original reference')
    index = build_event_index(event_rows)
    provenance = {'csv': csv_sources, 'xgSha256': xg_digest, 'xgArchive': xg_source, 'xgJoin': joined,
        'workloadEventsSha256': events_digest, 'workloadCoverageSha256': file_sha(coverage),
        'workloadCoverage': workload_source, 'workloadReferenceCheck': reference}
    implementation = {name: file_sha(ROOT / name) for name in IMPLEMENTATION}
    frozen = {'planSha256': sha(plan), 'dataSha256': sha(provenance),
              'referenceSettingsSha256': sha(plan['baseline']), 'implementationSha256': sha(implementation)}
    if phase == 'audit':
        previous = json.loads((output / 'selection.json').read_text())
        if any(previous.get(key) != value for key, value in frozen.items()):
            raise ValueError('Plan, sources, fixed baseline or implementation changed after selection')
    examples = prepare(observations, index, workload_source, plan, output, sha(frozen))
    examples_sha = sha(examples)
    if phase == 'audit' and previous.get('examplesSha256') != examples_sha:
        raise ValueError('Cached OOF examples changed after selection')
    validation = [row for row in examples if row['season'] in plan['selectionSeasons']]
    common = [row for row in validation if row['v1'] is not None]
    if len(common) != plan['expectedValidationCommon']:
        raise ValueError('Validation cohort does not match the registered count')
    baseline = summarize(common, 'baseline')
    dump(output / 'data-manifest.json', provenance)
    dump(output / 'implementation.json', implementation)
    dump(output / 'protocol.json', plan)
    coverage_report = coverage_summary(examples)
    dump(output / 'coverage.json', coverage_report)
    print('REFERENCE', json.dumps(baseline), flush=True)
    if phase in ('all', 'selection'):
        tasks = [(examples, validation, settings) for settings in plan['models']]
        if workers == 2:
            with ProcessPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(predict_correction, tasks))
        else:
            results = [predict_correction(task) for task in tasks]
        for result in results:
            print('VALIDATION', json.dumps(compact(result)), flush=True)
            dump(output / ('validation-predictions-' + result['id'] + '.json'), result['rows'])
        eligible = [result for result in results if result['metrics']['logloss'] < baseline['logloss']
                    and result['metrics']['accuracy'] >= baseline['accuracy']]
        chosen = min(eligible, key=lambda result: result['metrics']['logloss']) if eligible else None
        selection = {'protocol': plan['protocol'], **frozen, 'examplesSha256': examples_sha,
            'baseline': baseline, 'validation': [compact(result) for result in results],
            'chosen': compact(chosen) if chosen else None, 'retrospectiveOnly': True,
            'challengerAuditDeferred': True, 'promotedToDefault': False}
        dump(output / 'selection.json', selection)
        dump(output / 'validation-models.json', {result['id']: result['models'] for result in results})
        if chosen:
            dump(output / 'validation-predictions.json', chosen['rows'])
        print('SELECTION_FROZEN', json.dumps(selection['chosen']), flush=True)
    if phase in ('all', 'audit'):
        selection = json.loads((output / 'selection.json').read_text())
        if any(selection.get(key) != value for key, value in frozen.items()) or selection['examplesSha256'] != examples_sha:
            raise ValueError('Frozen experiment changed after selection')
        summary = {'protocol': plan['protocol'], 'selection': selection, 'coverage': coverage_report,
            'retrospectiveOnly': True, 'prospectiveValidationRequired': True, 'promotedToDefault': False,
            'passesRetrospectiveGate': False, 'challengerAuditComputedAfterSelection': False}
        if selection['chosen']:
            for label, years in [('audit', plan['auditSeasons']), ('recentMonitoring', plan['recentMonitoringSeasons'])]:
                targets = [row for row in examples if row['season'] in years]
                common = [row for row in targets if row['v1'] is not None]
                if label == 'audit' and len(common) != plan['expectedAuditCommon']:
                    raise ValueError('Audit cohort does not match the registered count')
                result = predict_correction((examples, targets, selection['chosen']['settings']))
                common = [row for row in result['rows'] if row['v1'] is not None]
                summary[label] = {'targets': len(targets), 'commonMatches': len(common),
                    'metrics': {key: summarize(common, key) for key in ('challenger', 'baseline', 'v1', 'base')},
                    'bySeason': result['bySeason'], 'challengerMinusBaseline': paired_bootstrap(common, 'challenger', 'baseline')}
                dump(output / (label + '-predictions.json'), result['rows'])
                dump(output / (label + '-models.json'), result['models'])
                print(label.upper(), json.dumps(summary[label]), flush=True)
            a, b = (summary['audit']['metrics'][key] for key in ('challenger', 'baseline'))
            summary.update(passesRetrospectiveGate=a['accuracy'] >= b['accuracy'] and a['logloss'] < b['logloss'] and a['brier'] < b['brier'],
                           challengerAuditComputedAfterSelection=True)
        else:
            summary['reason'] = 'No correction improved validation log loss without reducing accuracy; retain the fixed xG baseline.'
        dump(output / 'summary.json', summary)
        print('AUDIT_GATE', summary['passesRetrospectiveGate'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('data-dir', 'xg-matches', 'xg-manifest', 'events', 'coverage', 'output'):
        parser.add_argument('--' + flag, required=True, type=Path)
    parser.add_argument('--plan', default=PLAN, type=Path)
    parser.add_argument('--phase', choices=['all', 'selection', 'audit'], default='all')
    parser.add_argument('--workers', choices=[1, 2], type=int, default=1)
    args = parser.parse_args()
    execute(args.data_dir, args.xg_matches, args.xg_manifest, args.events, args.coverage,
            args.output, args.plan, args.phase, args.workers)
