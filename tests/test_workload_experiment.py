from contextlib import ExitStack, redirect_stdout
import copy
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.data import Match
from research.nightly import sha
from research.performance import Performance
from research.workload_experiment import (IMPLEMENTATION, build_event_index, execute,
    feature_rows, file_sha, predict_correction, prepare)


def event(when, home='Arsenal', away='Liverpool', competition='PL'):
    return {'date': when, 'home': home, 'away': away, 'competition': competition,
            'status': 'played', 'minutes': 90, 'minutes_known': True,
            'source_id': when + home + away + competition}


def proof():
    return {'requiredCompetitions': ['PL', 'LC'], 'requiredCompetitionsVerified': True,
            'scopes': [{'competition': comp, 'start': '2026-01-01', 'end': '2026-12-31',
                        'complete': True} for comp in ('PL', 'LC')]}


class WorkloadFeaturePreparationTests(unittest.TestCase):
    def test_indexed_features_include_cups_but_exclude_previous_day_and_future(self):
        rows = [{'date': '2026-11-06', 'home': 'Arsenal', 'away': 'Chelsea'}]
        events = [event('2026-11-01'), event('2026-11-02', 'Chelsea', 'Everton'),
                  event('2026-11-03', 'Arsenal', 'Brighton', 'LC')]
        first = feature_rows(rows, build_event_index(events), proof())
        self.assertEqual(first[0]['workload'], [-1 / 7, -1 / 3, -1 / 5])
        changed = events + [event(day) for day in ('2026-11-05', '2026-11-06', '2026-11-07')]
        self.assertEqual(first, feature_rows(rows, build_event_index(changed), proof()))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            build_event_index(events + [events[0]])
        partial = {**proof(), 'requiredCompetitionsVerified': False}
        self.assertIsNone(feature_rows(rows, build_event_index(events), partial)[0]['workload'])

    def test_monthly_refits_use_first_cutoff_and_missing_coverage_is_exact_identity(self):
        rows = [{'date': day, 'cutoff_date': cutoff, 'season': 2026, 'actual': 0,
                 'baseline': [.5, .3, .2], 'workload': None, 'v1': [.4, .3, .3]}
                for day, cutoff in [('2026-08-05', '2026-08-04'), ('2026-08-12', '2026-08-11'),
                                    ('2026-09-02', '2026-09-01')]]
        settings = {'id': 'test', 'ridge': .2, 'halfLife': 730, 'windowDays': 1460}
        identity = {'beta': [0., 0., 0.], 'status': 'insufficient_history'}
        with patch('research.workload_experiment.fit_workload', return_value=identity) as fitting:
            result = predict_correction((rows, rows, settings))
        self.assertEqual([call.args[1] for call in fitting.call_args_list], [date(2026, 8, 4), date(2026, 9, 1)])
        self.assertEqual(result['changedPredictions'], 0)
        self.assertTrue(all(row['challenger'] == row['baseline'] for row in result['rows']))

    def test_cached_rows_require_both_input_fingerprint_and_content_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            rows = [{'fixture': 'synthetic'}]
            cache = {'fingerprint': 'current', 'rows': rows, 'rowsSha256': sha(rows)}
            (output / 'feature-cache.json').write_text(json.dumps(cache))
            with patch('research.workload_experiment.reference_rows', side_effect=RuntimeError('rebuild needed')) as reference:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(prepare([], {}, {}, {}, output, 'current'), rows)
                reference.assert_not_called()
                cache['rows'][0]['fixture'] = 'tampered'
                (output / 'feature-cache.json').write_text(json.dumps(cache))
                with self.assertRaisesRegex(RuntimeError, 'rebuild needed'):
                    prepare([], {}, {}, {'seasons': [2026]}, output, 'current')


class WorkloadExperimentGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'output'
        self.output.mkdir()
        self.plan_path, self.xg, self.xg_manifest = [self.root / name for name in ('plan.json', 'xg.json', 'xg-manifest.json')]
        self.events, self.coverage = self.root / 'events.json', self.root / 'coverage.json'
        self.observations = [Performance(Match(date(2023, 8, 15), 2023, 'A', 'B', 1, 0), 10, 8, 4, 2)]
        self.csv = [{'season': 2023, 'rows': 380}]
        self.rows = [{'date': '2023-08-15', 'cutoff_date': '2023-08-14', 'season': 2023,
            'home': 'A', 'away': 'B', 'actual': 0, 'baseline': [.5, .3, .2], 'v1': [.4, .3, .3],
            'workload': None, 'workloadCoverage': {'unknownMinutesGamesLast14Days': 0}}]
        self.plan = {'protocol': 'test', 'seasons': [2023, 2024, 2025], 'selectionSeasons': [2023],
            'auditSeasons': [2024], 'recentMonitoringSeasons': [2025], 'models': [],
            'expectedValidationCommon': 1, 'expectedAuditCommon': 1, 'baseline': {'halfLife': 180}}
        self.reset_inputs()
        (self.output / 'data-manifest.json').write_text('previous frozen evidence')

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def reset_inputs(self):
        self.write(self.plan_path, self.plan)
        self.xg.write_text('[]')
        self.events.write_text('[]')
        self.write(self.xg_manifest, {'normalizedMatchesSha256': file_sha(self.xg)})
        self.write(self.coverage, {'normalizedMatchesSha256': file_sha(self.events)})
        for name in IMPLEMENTATION:
            path = self.root / name
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text('synthetic code: ' + name)

    def mocks(self):
        stack = ExitStack()
        stack.enter_context(patch('research.workload_experiment.ROOT', self.root))
        stack.enter_context(patch('research.workload_experiment.load_performance', return_value=(self.observations, self.csv)))
        self.preparation = stack.enter_context(patch('research.workload_experiment.prepare', return_value=self.rows))
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack

    def run_experiment(self, phase='selection'):
        execute(self.root / 'csv', self.xg, self.xg_manifest, self.events, self.coverage,
                self.output, self.plan_path, phase=phase)

    def test_bad_source_bindings_or_truncated_closed_seasons_stop_before_preparation(self):
        for changed in ('xg source', 'workload source', 'closed season'):
            self.reset_inputs()
            self.csv[0]['rows'] = 380
            if changed == 'closed season':
                self.csv[0]['rows'] = 379
            else:
                self.write(self.xg_manifest if changed == 'xg source' else self.coverage, {})
            before = (self.output / 'data-manifest.json').read_bytes()
            with self.subTest(changed=changed), self.mocks():
                with self.assertRaises(ValueError):
                    self.run_experiment()
                self.preparation.assert_not_called()
            self.assertEqual((self.output / 'data-manifest.json').read_bytes(), before)

    def test_audit_rejects_changed_code_recipe_or_source_before_preparation_and_overwrite(self):
        for changed in ('code', 'fixed baseline', 'source bytes'):
            self.reset_inputs()
            with self.mocks():
                self.run_experiment()
            frozen_outputs = {path.name: path.read_bytes() for path in self.output.iterdir()}
            if changed == 'code':
                (self.root / IMPLEMENTATION[0]).write_text('changed code')
            elif changed == 'fixed baseline':
                modified = copy.deepcopy(self.plan)
                modified['baseline']['halfLife'] = 365
                self.write(self.plan_path, modified)
            else:
                self.events.write_text('[]\n')
                self.write(self.coverage, {'normalizedMatchesSha256': file_sha(self.events)})
            with self.subTest(changed=changed), self.mocks():
                with self.assertRaisesRegex(ValueError, 'changed after selection'):
                    self.run_experiment('audit')
                self.preparation.assert_not_called()
            self.assertEqual({path.name: path.read_bytes() for path in self.output.iterdir()}, frozen_outputs)


if __name__ == '__main__':
    unittest.main()
