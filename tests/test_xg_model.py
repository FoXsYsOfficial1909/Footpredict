from dataclasses import replace
from datetime import date, timedelta
import math
import unittest

import numpy as np

from research.data import Match
from research.models import LearnedPoisson
from research.performance import Performance
from research.xg_model import ExpectedGoalsPoisson


def sample():
    rng = np.random.default_rng(83)
    teams = ['A', 'B', 'C', 'D', 'E', 'F']
    rows = []
    for i in range(220):
        home, away = teams[i % 6], teams[(i % 6 + 1 + (i // 6) % 5) % 6]
        match = Match(date(2024, 1, 1) + timedelta(days=i), 2023, home, away,
                      int(rng.poisson(1.8)), int(rng.poisson(1.1)))
        rows.append(Performance(match, 12, 10, 5, 3,
                                float(rng.uniform(.1, 3.5)), float(rng.uniform(.1, 2.5))))
    return rows


class ExpectedGoalsModelTests(unittest.TestCase):
    def test_target_and_future_xg_or_scores_cannot_change_the_fit(self):
        rows = sample()
        cutoff = rows[160].match.day
        first = ExpectedGoalsPoisson().fit(rows, cutoff)
        changed = [replace(row, match=replace(row.match, home_goals=25, away_goals=0),
                           home_xg=math.nan, away_xg=-10)
                   if row.match.day >= cutoff else row for row in rows]
        after = ExpectedGoalsPoisson().fit(changed, cutoff)
        np.testing.assert_array_equal(first.theta, after.theta)
        self.assertEqual(first.xg_info, after.xg_info)
        self.assertLess(first.xg_info['latest_xg_date'], cutoff.isoformat())

    def test_historical_xg_changes_strengths_without_mutating_actual_scores(self):
        rows = sample()
        originals = [(row.match.key, row.match.home_goals, row.match.away_goals) for row in rows]
        cutoff = date(2024, 9, 1)
        first = ExpectedGoalsPoisson().fit(rows, cutoff)
        changed = [replace(row, home_xg=10.) if row.match.home == 'A' else row for row in rows]
        after = ExpectedGoalsPoisson().fit(changed, cutoff)
        self.assertGreater(np.max(np.abs(first.theta - after.theta)), 1e-4)
        self.assertEqual(originals, [(row.match.key, row.match.home_goals, row.match.away_goals) for row in rows])

    def test_goal_only_limit_recovers_original_poisson(self):
        rows = sample()
        cutoff = date(2024, 9, 1)
        goal_only = ExpectedGoalsPoisson(goal_weight=1).fit(rows, cutoff)
        baseline = LearnedPoisson().fit([row.match for row in rows], cutoff)
        np.testing.assert_allclose(goal_only.theta, baseline.theta, rtol=0, atol=1e-12)
        np.testing.assert_allclose(goal_only.predict('A', 'B')['probabilities'],
                                   baseline.predict('A', 'B')['probabilities'], rtol=0, atol=1e-12)

    def test_partial_missing_pair_keeps_both_actual_goals_and_training_fixture(self):
        rows = sample()
        first = replace(rows[-1], home_xg=None, away_xg=10.)
        second = replace(first, away_xg=0.)
        cutoff = date(2024, 9, 1)
        a = ExpectedGoalsPoisson().fit(rows[:-1] + [first], cutoff)
        b = ExpectedGoalsPoisson().fit(rows[:-1] + [second], cutoff)
        np.testing.assert_array_equal(a.theta, b.theta)
        self.assertEqual(a.xg_info['training_games'], 220)
        self.assertEqual(a.xg_info['xg_games'], 219)
        self.assertEqual(a.xg_info['missing_xg_games'], 1)
        self.assertGreater(a.xg_info['weighted_coverage'], 0)
        self.assertLess(a.xg_info['weighted_coverage'], 1)

    def test_row_with_zero_xg_is_valid_but_zero_total_cannot_define_the_scale(self):
        rows = sample()
        rows[0] = replace(rows[0], home_xg=0., away_xg=0.)
        fitted = ExpectedGoalsPoisson().fit(rows, date(2024, 9, 1))
        self.assertEqual(fitted.xg_info['xg_games'], 220)
        p = fitted.predict('A', 'B')['probabilities']
        self.assertTrue(np.all(np.isfinite(p)))
        self.assertAlmostEqual(float(p.sum()), 1.)
        with self.assertRaisesRegex(ValueError, 'positive finite total'):
            ExpectedGoalsPoisson().fit([replace(row, home_xg=0., away_xg=0.) for row in rows], date(2024, 9, 1))

    def test_invalid_historical_xg_and_goal_weights_are_rejected(self):
        for value in (-.1, 1.1, math.nan, math.inf, None, 'bad'):
            with self.subTest(goal_weight=value), self.assertRaises(ValueError):
                ExpectedGoalsPoisson(goal_weight=value)
        rows = sample()
        for value in (-.1, 15.01, math.nan, math.inf, 'bad'):
            with self.subTest(xg=value), self.assertRaisesRegex(ValueError, 'finite and between'):
                ExpectedGoalsPoisson().fit([replace(rows[0], home_xg=value)] + rows[1:], date(2024, 9, 1))

    def test_minimum_paired_history_and_window_are_enforced(self):
        rows = sample()
        with self.assertRaisesRegex(ValueError, '100 earlier games'):
            ExpectedGoalsPoisson().fit(rows[:99], date(2024, 9, 1))
        missing = [replace(row, home_xg=None) if i >= 99 else row for i, row in enumerate(rows)]
        with self.assertRaisesRegex(ValueError, '100 earlier games with paired xG'):
            ExpectedGoalsPoisson().fit(missing, date(2024, 9, 1))
        cutoff = date(2024, 9, 1)
        first = ExpectedGoalsPoisson(window_days=200).fit(rows, cutoff)
        changed = [replace(row, home_xg=math.nan) if (cutoff - row.match.day).days > 200 else row for row in rows]
        second = ExpectedGoalsPoisson(window_days=200).fit(changed, cutoff)
        np.testing.assert_array_equal(first.theta, second.theta)

    def test_export_records_exact_target_recipe_and_coverage(self):
        model = ExpectedGoalsPoisson(goal_weight=.5, half_life=180, ridge=.03).fit(sample(), date(2024, 9, 1))
        exported = model.export()
        self.assertEqual(exported['model_version'], 'expected-goals-poisson-0.1')
        self.assertEqual(exported['xg_target'], model.xg_info)
        self.assertEqual(exported['xg_target']['goal_weight'], .5)
        self.assertEqual(exported['xg_target']['weighted_coverage'], 1.)
        self.assertEqual(exported['fit']['training_games'], 220)
        self.assertEqual(exported['parameters']['half_life'], 180)


if __name__ == '__main__':
    unittest.main()
