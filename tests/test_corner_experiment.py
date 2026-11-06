from dataclasses import dataclass
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.corner_experiment import (IMPLEMENTATION, event_scores, execute, paired_bootstrap,
                                        predict, summarize, target_rows)
from research.data import Match
from research.nightly import sha
import hashlib


@dataclass
class Observation:
    match: Match
    home_corners: int | None
    away_corners: int | None

    @property
    def total(self):
        return None if self.home_corners is None or self.away_corners is None else self.home_corners + self.away_corners


def packet(probabilities=(.7, .6, .5)):
    return {'expectedCorners': {'home': 6., 'away': 4., 'total': 10.},
            'totals': {str(line): {'over': p, 'under': 1 - p}
                       for line, p in zip((8.5, 9.5, 10.5), probabilities)},
            'range80': {'lower': 6, 'upper': 14}}


class CornerMetricsTests(unittest.TestCase):
    def test_thresholds_use_true_count_events_and_do_not_share_1x2_denominator(self):
        p = packet(); lines = [8.5, 9.5, 10.5]
        scores = event_scores(p, 10, lines)
        self.assertAlmostEqual(scores['9.5']['logloss'], -__import__('math').log(.6))
        self.assertAlmostEqual(scores['10.5']['logloss'], -__import__('math').log(.5))
        result = summarize([{'actualTotal': 10, 'candidate': p}], 'candidate', lines)
        self.assertEqual(result['n'], 1)
        self.assertTrue(all(value['n'] == 1 for value in result['byLine'].values()))
        self.assertEqual(result['meanAbsoluteError'], 0.)
        self.assertEqual(result['predictiveIntervalCoverage'], 1.)
        with self.assertRaisesRegex(ValueError, 'probabilities'):
            event_scores(packet((1.2, .5, .4)), 10, lines)

    def test_identical_day_clustered_forecasts_have_zero_difference(self):
        p = packet()
        rows = [{'date': f'2025-01-{i + 1:02d}', 'actualTotal': 8 + i,
                 'baseline': p, 'challenger': p} for i in range(8)]
        result = paired_bootstrap(rows, [8.5, 9.5, 10.5], samples=100)
        for metric in result.values():
            self.assertEqual(metric['difference'], 0.)
            self.assertEqual(metric['ci95'], [0., 0.])

    def test_missing_corner_counts_are_unknown_instead_of_zero_targets(self):
        rows = [Observation(Match(date(2025, 8, 1), 2025, 'A', 'B', 2, 1), 0, 0),
                Observation(Match(date(2025, 8, 2), 2025, 'A', 'C', 1, 0), None, 4)]
        result = target_rows(rows, {2025})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['actualTotal'], 0)
        self.assertEqual(result[0]['cutoffExclusive'], '2025-07-31')


class CornerRunnerGuardsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name); self.output = self.root / 'output'; self.output.mkdir()
        self.plan_path = self.root / 'plan.json'
        self.plan = {'protocol': 'corner-research-1', 'seasons': [2023, 2024],
            'selectionSeasons': [2023], 'auditSeasons': [2023], 'recentMonitoringSeasons': [2024],
            'expectedValidationTargets': 760, 'expectedAuditTargets': 380,
            'lines': [8.5, 9.5, 10.5],
            'models': [{'id': 'p', 'family': 'poisson', 'halfLife': 365, 'ridge': .03, 'windowDays': 1095}],
            'baseline': {'halfLife': 365, 'windowDays': 1095}}
        self.plan_path.write_text(json.dumps(self.plan))
        self.rows = [Observation(Match(date(2023, 8, 1), 2023, 'A', 'B', 1, 0), 5, 4)]

    def test_truncated_season_and_missing_corner_cohort_reject_before_any_fitting(self):
        for source in ({'season': 2023, 'rows': 379}, {'season': 2023, 'rows': 380}):
            with self.subTest(source=source), \
                 patch('research.corner_experiment.load_corners', return_value=(self.rows, [source])), \
                 patch('research.corner_experiment.CornerModel.fit') as fit:
                with self.assertRaisesRegex(ValueError, '380 verified|cohort count'):
                    execute(self.root, self.output, date(2026, 11, 6), self.plan_path)
                fit.assert_not_called()
                self.assertFalse((self.output / 'selection.json').exists())

    def test_own_target_corners_never_enter_predictor_call(self):
        class FakeModel:
            def __init__(self, **settings):
                pass
            def fit(self, observations, cutoff):
                self.eligible = [o for o in observations if o.match.day < cutoff]
            def predict(self, home, away, day):
                return packet((.6 + len(self.eligible) / 1000, .5, .4))
        row = target_rows(self.rows, {2023})[0]
        settings = self.plan['models'][0]
        with patch('research.corner_experiment.CornerModel', FakeModel):
            before = predict((self.rows, [row], settings))
            changed = {**row, 'actualTotal': 80, 'homeCorners': 40, 'awayCorners': 40}
            after = predict((self.rows, [changed], settings))
        self.assertEqual(before[0]['challenger'], after[0]['challenger'])
        self.assertNotEqual(before[0]['actualTotal'], after[0]['actualTotal'])

    def test_changed_audit_inputs_fail_before_overwriting_frozen_artifacts(self):
        self.plan['expectedValidationTargets'] = 1
        self.plan_path.write_text(json.dumps(self.plan))
        for name in IMPLEMENTATION:
            path = self.root / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture: ' + name)
        implementation = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                          for name in IMPLEMENTATION}
        selection = {'planSha256': sha(self.plan),
            'dataSha256': sha([['2023-08-01', 2023, 'A', 'B', 5, 4]]),
            'implementationSha256': sha(implementation)}
        (self.output / 'selection.json').write_text(json.dumps(selection))
        (self.output / 'protocol.json').write_text('previous protocol')
        self.plan['models'][0]['halfLife'] = 180
        self.plan_path.write_text(json.dumps(self.plan))
        with patch('research.corner_experiment.ROOT', self.root), \
             patch('research.corner_experiment.load_corners', return_value=(self.rows, [{'season': 2023, 'rows': 380}])), \
             patch('research.corner_experiment.baseline_predictions', side_effect=AssertionError('Must not fit')):
            with self.assertRaisesRegex(ValueError, 'changed after selection'):
                execute(self.root, self.output, date(2026, 11, 6), self.plan_path, phase='audit')
        self.assertEqual((self.output / 'protocol.json').read_text(), 'previous protocol')
        self.assertEqual(json.loads((self.output / 'selection.json').read_text()), selection)


if __name__ == '__main__':
    unittest.main()
