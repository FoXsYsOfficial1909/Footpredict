from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from research.corners import CornerObservation, empiricalLeague
from research.corner_nightly import build_candidate, recipe, run
from research.data import Match


SETTINGS = {'id': 'corners-poisson-180', 'family': 'poisson', 'halfLife': 180,
            'ridge': .03, 'windowDays': 1095}


def observations():
    rng = np.random.default_rng(309); teams = ['A', 'B', 'C', 'D', 'E', 'F']; rows = []
    for i in range(220):
        when = date(2023, 1, 1) + timedelta(days=i)
        home, away = teams[i % 6], teams[(i % 6 + 1 + (i // 6) % 5) % 6]
        match = Match(when, 2023 if when.month >= 7 else 2022, home, away, 1, 0)
        rows.append(CornerObservation(match, int(rng.poisson(5.8)), int(rng.poisson(4.4))))
    return rows


class CornerNightlyTests(unittest.TestCase):
    def build(self, rows):
        return build_candidate(rows, SETTINGS, date(2023, 9, 1), evaluation_limit=12, min_common=5)

    def test_recipe_is_persisted_and_no_default_or_unknown_family_is_selected(self):
        self.assertEqual(recipe({'chosen': {'settings': SETTINGS}}), SETTINGS)
        with self.assertRaises(ValueError):
            recipe({'chosen': None})
        with self.assertRaises(ValueError):
            recipe({**SETTINGS, 'family': 'new-unselected-model'})

    def test_excluded_dates_cannot_change_final_weights_or_monitoring(self):
        rows = observations(); first, report, predictions = self.build(rows)
        extra = [CornerObservation(Match(date(2023, 9, 1) + timedelta(days=d), 2023, 'A', 'B', 0, 0), 40, 40)
                 for d in (-1, 0, 1)]
        second, after, changed = self.build(rows + extra)
        self.assertEqual(first, second)
        self.assertEqual(predictions, changed)
        self.assertEqual(report['dataSha256'], after['dataSha256'])
        self.assertLess(report['latestTrainingMatch'], report['trainingCutoffExclusive'])
        self.assertFalse(report['promotedToDefault'])
        self.assertEqual(report['evaluation']['metrics']['challenger']['n'],
                         report['evaluation']['metrics']['baseline']['n'])

    def test_current_target_corners_change_its_label_not_its_own_forecast(self):
        rows = observations(); _, _, first = self.build(rows)
        rows[-1] = replace(rows[-1], home_corners=35, away_corners=35)
        _, _, second = self.build(rows)
        self.assertEqual(first[-1]['challenger'], second[-1]['challenger'])
        self.assertEqual(first[-1]['baseline'], second[-1]['baseline'])
        self.assertNotEqual(first[-1]['actualTotal'], second[-1]['actualTotal'])

    def test_failed_recipe_load_removes_local_stale_weights_and_writes_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'model.json').write_text('{"old":true}')
            with self.assertRaises(FileNotFoundError):
                run(root / 'csv', root, date(2023, 9, 1), selection_path=root / 'missing.json', offline=True)
            self.assertFalse((root / 'model.json').exists())
            attempt = json.loads((root / 'attempt.json').read_text())
            self.assertEqual(attempt['status'], 'failed')
            self.assertFalse(attempt['promotedToDefault'])

    def test_unvalidated_team_recipe_serves_exact_empirical_estimates_without_glm(self):
        rows = observations(); selection = {'chosen': {'settings': SETTINGS},
            'servingPolicy': 'empirical_league', 'researchAudit': {'passesRetrospectiveGate': False}}
        with patch('research.corner_nightly.CornerModel.fit', side_effect=AssertionError('No unvalidated team fit')):
            model, report, predictions = build_candidate(rows, selection, date(2023, 9, 1),
                evaluation_limit=12, min_common=5)
        expected = empiricalLeague(rows, date(2023, 8, 31))
        self.assertEqual(model['league_estimate'], expected)
        self.assertEqual(model['family'], 'empirical')
        self.assertEqual(model['scope'], 'league_baseline')
        self.assertEqual(report['researchSelection'], selection)
        self.assertEqual(report['evaluation']['metrics']['challenger'], report['evaluation']['metrics']['baseline'])
        self.assertTrue(all(row['challenger'] == row['baseline'] for row in predictions))
        self.assertLess(report['latestTrainingMatch'], report['trainingCutoffExclusive'])

    def test_empirical_refresh_uses_new_past_corners_and_excludes_cutoff_day(self):
        rows = observations(); selected = {'chosen': {'settings': SETTINGS}, 'servingPolicy': 'empirical_league'}
        first, _, _ = build_candidate(rows, selected, date(2023, 9, 1), evaluation_limit=12, min_common=5)
        excluded = CornerObservation(Match(date(2023, 8, 31), 2023, 'A', 'B', 1, 0), 35, 35)
        same, _, _ = build_candidate(rows + [excluded], selected, date(2023, 9, 1), evaluation_limit=12, min_common=5)
        self.assertEqual(first, same)
        added = replace(excluded, match=replace(excluded.match, day=date(2023, 8, 30)))
        changed, _, _ = build_candidate(rows + [added], selected, date(2023, 9, 1), evaluation_limit=12, min_common=5)
        self.assertNotEqual(first['league_estimate']['totals'], changed['league_estimate']['totals'])
