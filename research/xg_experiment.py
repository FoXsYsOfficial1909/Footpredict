"""Bounded, chronological expected-goals research; never promotes a model."""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from datetime import date
import hashlib
import json
from pathlib import Path

import numpy as np

from research.accuracy import dump
from research.evaluate import (reference_rows, glm_predictions, elo_predictions,
                               with_predictions, summarize, paired_bootstrap)
from research.nightly import sha
from research.performance import load_performance
from research.xg_data import load_xg_matches, merge_xg
from research.xg_model import ExpectedGoalsPoisson

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / 'docs/research/xg-plan-2026-11-06.json'


def predict(arguments):
    observations, rows, settings = arguments
    model = ExpectedGoalsPoisson(goal_weight=settings['goalWeight'],
        half_life=settings['halfLife'], ridge=settings['ridge'], window_days=settings['windowDays'])
    predictions, diagnostics, previous = [], [], None
    for row in rows:
        cutoff = date.fromisoformat(row['cutoff_date'])
        if cutoff != previous:
            model.fit(observations, cutoff)
            diagnostics.append({'cutoffExclusive': cutoff.isoformat(), **model.xg_info})
            previous = cutoff
        p = model.predict(row['home'], row['away'], date.fromisoformat(row['date']))['probabilities']
        predictions.append((settings['poissonWeight'] * p +
            (1 - settings['poissonWeight']) * np.array(row['elo'])).tolist())
    result = [{**row, 'challenger': p} for row, p in zip(rows, predictions)]
    common = [r for r in result if r['v1'] is not None]
    return {'id': settings['id'], 'settings': settings, 'metrics': summarize(common, 'challenger'),
        'fullCoverage': summarize(result, 'challenger'),
        'bySeason': {str(year): summarize([r for r in common if r['season'] == year], 'challenger')
                     for year in sorted({r['season'] for r in common})},
        'diagnostics': diagnostics, 'rows': result}


def compact(result):
    return {key: value for key, value in result.items() if key not in ('rows', 'diagnostics')}


def execute(data_dir, xg_path, output, plan_path=PLAN, phase='all', workers=2, source_manifest=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    plan = json.loads(Path(plan_path).read_text())
    observations, csv_manifest = load_performance(data_dir, plan['seasons'])
    if any(m['rows'] != 380 for m in csv_manifest if m['season'] < max(plan['seasons'])):
        raise ValueError('A closed EPL season must contain 380 verified results')
    observations, join_manifest = merge_xg(observations, load_xg_matches(xg_path))
    provenance = {'csv': csv_manifest, 'xgSha256': hashlib.sha256(Path(xg_path).read_bytes()).hexdigest(),
                  'join': join_manifest}
    if not source_manifest:
        raise ValueError('A source manifest binding the normalized xG bytes is required')
    provenance['archive'] = json.loads(Path(source_manifest).read_text())
    if provenance['archive'].get('normalizedMatchesSha256') != provenance['xgSha256']:
        raise ValueError('The source manifest does not match the normalized xG bytes')
    selected = json.loads((ROOT / 'docs/research/selection.json').read_text())
    implementation = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
        ('research/xg_experiment.py', 'research/xg_model.py', 'research/xg_data.py',
         'research/performance.py', 'research/models.py', 'research/evaluate.py', 'research/data.py')}
    frozen = {'planSha256': sha(plan), 'dataSha256': sha(provenance),
              'referenceSettingsSha256': sha(selected), 'implementationSha256': sha(implementation)}
    if phase == 'audit':
        previous = json.loads((output / 'selection.json').read_text())
        if any(previous.get(key) != value for key, value in frozen.items()):
            raise ValueError('Plan, sources, reference recipe or implementation changed after selection')
    dump(output / 'data-manifest.json', provenance)
    dump(output / 'implementation.json', implementation)
    dump(output / 'protocol.json', plan)
    matches = [r.match for r in observations]
    seasons = set(plan['selectionSeasons'] + plan['auditSeasons'] + plan['recentMonitoringSeasons'])
    rows = reference_rows(matches, seasons)
    rows = with_predictions(rows, glm_predictions((matches, rows, selected['glm']))['predictions'], 'glm')
    rows = with_predictions(rows, elo_predictions(matches, rows, selected['elo']), 'elo')
    for row in rows:
        row['candidate'] = (selected['glm_weight'] * np.array(row['glm']) +
                           (1 - selected['glm_weight']) * np.array(row['elo'])).tolist()
    validation = [r for r in rows if r['season'] in plan['selectionSeasons']]
    baseline = summarize([r for r in validation if r['v1'] is not None], 'candidate')
    if baseline['n'] != plan['expectedValidationCommon']:
        raise ValueError('Validation cohort does not match the registered count')
    print('REFERENCE', json.dumps(baseline), flush=True)
    if phase in ('all', 'selection'):
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(predict, [(observations, validation, s) for s in plan['models']]))
        for result in results:
            print('VALIDATION', json.dumps(compact(result)), flush=True)
        eligible = [r for r in results if r['metrics']['accuracy'] >= baseline['accuracy']
                    and r['metrics']['logloss'] < baseline['logloss']]
        chosen = min(eligible, key=lambda r: r['metrics']['logloss']) if eligible else None
        selection = {'protocol': plan['protocol'], **frozen,
            'baseline': baseline, 'validation': [compact(r) for r in results],
            'chosen': compact(chosen) if chosen else None, 'retrospectiveOnly': True,
            'challengerAuditDeferred': True, 'promotedToDefault': False}
        dump(output / 'selection.json', selection)
        if chosen:
            dump(output / 'validation-predictions.json', chosen['rows'])
            dump(output / 'validation-coverage.json', chosen['diagnostics'])
        print('SELECTION_FROZEN', json.dumps(selection['chosen']), flush=True)
    if phase in ('all', 'audit'):
        selection = json.loads((output / 'selection.json').read_text())
        if any(selection.get(key) != value for key, value in frozen.items()):
            raise ValueError('Frozen experiment changed after selection')
        summary = {'protocol': plan['protocol'], 'selection': selection, 'data': provenance,
            'retrospectiveOnly': True, 'promotedToDefault': False, 'prospectiveValidationRequired': True,
            'passesRetrospectiveGate': False, 'challengerAuditComputedAfterSelection': False}
        if selection['chosen']:
            summary['challengerAuditComputedAfterSelection'] = True
            for label, years in [('audit', plan['auditSeasons']), ('recentMonitoring', plan['recentMonitoringSeasons'])]:
                result = predict((observations, [r for r in rows if r['season'] in years], selection['chosen']['settings']))
                common = [r for r in result['rows'] if r['v1'] is not None]
                if label == 'audit' and len(common) != plan['expectedAuditCommon']:
                    raise ValueError('Audit cohort does not match the registered count')
                metrics = {key: summarize(common, key) for key in ('challenger', 'candidate', 'v1', 'base')}
                summary[label] = {'commonMatches': len(common), 'targets': len(result['rows']),
                    'metrics': metrics, 'bySeason': result['bySeason'], 'fullCoverage': result['fullCoverage'],
                    'challengerMinusCurrent': paired_bootstrap(common, 'challenger', 'candidate')}
                dump(output / (label + '-predictions.json'), result['rows'])
                dump(output / (label + '-coverage.json'), result['diagnostics'])
                print(label.upper(), json.dumps(summary[label]), flush=True)
            metrics = summary['audit']['metrics']
            a, b = metrics['challenger'], metrics['candidate']
            summary['passesRetrospectiveGate'] = (a['accuracy'] >= b['accuracy'] and
                a['brier'] < b['brier'] and a['logloss'] < b['logloss'])
        dump(output / 'summary.json', summary)
        print('AUDIT_GATE', summary['passesRetrospectiveGate'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--xg-matches', required=True, type=Path)
    parser.add_argument('--source-manifest', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--plan', default=PLAN, type=Path)
    parser.add_argument('--phase', choices=['all', 'selection', 'audit'], default='all')
    parser.add_argument('--workers', choices=[1, 2], default=2, type=int)
    args = parser.parse_args()
    execute(args.data_dir, args.xg_matches, args.output, args.plan, args.phase, args.workers, args.source_manifest)
