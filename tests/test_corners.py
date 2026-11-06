from dataclasses import replace
from datetime import date, timedelta
import math
import unittest

import numpy as np

from research.data import Match
from research.corners import (CornerObservation, CornerModel, corner_count,
    parse_corners, count_distribution, distribution_prediction, empiricalLeague)


def sample():
    rng = np.random.default_rng(319)
    teams = ['A', 'B', 'C', 'D', 'E', 'F']
    result = []
    for i in range(220):
        home = teams[i % 6]; away = teams[(i % 6 + 1 + i // 6 % 5) % 6]
        intensity = rng.gamma(4, .25)
        h = min(40, int(rng.poisson((9 if home == 'A' else 7) * intensity)))
        a = min(40, int(rng.poisson(4.5 * intensity)))
        match = Match(date(2023, 8, 1) + timedelta(days=i), 2023, home, away,
                      int(rng.poisson(1.5)), int(rng.poisson(1.1)))
        result.append(CornerObservation(match, h, a))
    return result


class CornerDataTests(unittest.TestCase):
    def test_missing_and_invalid_corners_preserve_score_and_fixture(self):
        body = b'Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HC,AC\n01/08/2023,A,B,2,1,H,,7\n02/08/2023,C,D,0,0,D,-1,3\n'
        rows = parse_corners(body, 2023)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0].home_corners)
        self.assertEqual(rows[0].away_corners, 7)
        self.assertEqual((rows[0].match.home_goals, rows[0].match.away_goals), (2, 1))
        self.assertIsNone(rows[0].total)
        self.assertIsNone(rows[1].home_corners)
        self.assertIn('HC:invalid', rows[1].issues)
        self.assertEqual(rows[1].match.outcome, 1)

    def test_zero_is_observed_not_missing_and_invalid_counts_are_rejected(self):
        self.assertEqual(corner_count('0'), 0)
        self.assertEqual(corner_count('4.0'), 4)
        self.assertIsNone(corner_count(''))
        for value in (-1, 41, .5, math.nan, math.inf, True, 'unknown'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                corner_count(value)


class CornerModelTests(unittest.TestCase):
    def test_target_future_counts_and_scores_cannot_change_fit_or_dispersion(self):
        rows = sample(); cutoff = rows[170].match.day
        changed = [replace(row, home_corners=-10, away_corners=math.nan,
                           match=replace(row.match, home_goals=30))
                   if row.match.day >= cutoff else row for row in rows]
        a = CornerModel(family='negative_binomial').fit(rows, cutoff)
        b = CornerModel(family='negative_binomial').fit(changed, cutoff)
        np.testing.assert_array_equal(a.theta, b.theta)
        self.assertEqual(a.alpha, b.alpha)
        self.assertLess(a.fit_info['latest_training_date'], cutoff.isoformat())

    def test_past_corners_affect_fit_but_result_labels_do_not(self):
        rows = sample(); original = [(r.match.home_goals, r.match.away_goals) for r in rows]
        cutoff = date(2024, 4, 1)
        first = CornerModel().fit(rows, cutoff)
        goals_only = [replace(r, match=replace(r.match, home_goals=30, away_goals=0)) for r in rows]
        unchanged = CornerModel().fit(goals_only, cutoff)
        np.testing.assert_array_equal(first.theta, unchanged.theta)
        modified = [replace(r, home_corners=20) if r.match.home == 'A' else r for r in rows]
        changed = CornerModel().fit(modified, cutoff)
        self.assertGreater(np.max(np.abs(first.theta - changed.theta)), .01)
        self.assertEqual(original, [(r.match.home_goals, r.match.away_goals) for r in rows])
        self.assertGreater(first.predict('A', 'B')['expectedCorners']['home'], 5)

    def test_missing_pair_does_not_become_a_zero_and_duplicate_history_is_rejected(self):
        rows = sample(); rows[0] = replace(rows[0], home_corners=None)
        model = CornerModel().fit(rows, date(2024, 4, 1))
        self.assertEqual(model.fit_info['training_games'], 219)
        self.assertEqual(model.fit_info['missing_corner_games'], 1)
        changed = [replace(r, away_corners=40) if i == 0 else r for i, r in enumerate(rows)]
        same = CornerModel().fit(changed, date(2024, 4, 1))
        np.testing.assert_array_equal(model.theta, same.theta)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            CornerModel().fit(rows + [rows[1]], date(2024, 4, 1))

    def test_total_probabilities_use_correct_full_poisson_and_negative_binomial_cdf(self):
        for family, alpha in [('poisson', 0.), ('negative_binomial', .2)]:
            predicted = distribution_prediction(6, 4, family, alpha)
            if family == 'poisson':
                exact = sum(math.exp(-10) * 10 ** k / math.factorial(k) for k in range(10))
            else:
                exact = sum(math.comb(k + 4, k) * (1 / 3) ** 5 * (2 / 3) ** k for k in range(10))
            self.assertAlmostEqual(predicted['totals']['9.5']['under'], exact, places=13)
            self.assertEqual(predicted['expectedCorners']['total'], 10)
            self.assertGreater(predicted['totals']['8.5']['over'], predicted['totals']['9.5']['over'])
            self.assertGreater(predicted['totals']['9.5']['over'], predicted['totals']['10.5']['over'])
            for event in predicted['totals'].values():
                self.assertAlmostEqual(event['over'] + event['under'], 1.)
            bounds = predicted['range80']; distribution = count_distribution(10, family, alpha)
            self.assertGreaterEqual(float(distribution.cdf(bounds['upper']) - distribution.cdf(bounds['lower'] - 1)), .8 - 1e-12)
        self.assertAlmostEqual(float(count_distribution(10, 'negative_binomial', 0.).cdf(9)),
                               float(count_distribution(10).cdf(9)), places=15)

    def test_export_reproduces_means_and_prediction_rejects_backwards_dates(self):
        cutoff = date(2024, 4, 1)
        model = CornerModel(family='negative_binomial').fit(sample(), cutoff)
        exported = model.export(); predicted = model.predict('A', 'B', cutoff + timedelta(days=1))
        h = math.exp(exported['intercept'] + exported['home_advantage'] + exported['teams']['A']['attack'] - exported['teams']['B']['defence'])
        a = math.exp(exported['intercept'] + exported['teams']['B']['attack'] - exported['teams']['A']['defence'])
        self.assertAlmostEqual(predicted['expectedCorners']['home'], max(.1, min(20, h)), places=13)
        self.assertAlmostEqual(predicted['expectedCorners']['away'], max(.1, min(20, a)), places=13)
        self.assertEqual(exported['side_mean_bounds'], [.1, 20.])
        self.assertEqual(exported['target'], 'corners_total_regulation')
        self.assertEqual(model.predict('Unseen', 'B')['unseenTeams'], ['Unseen'])
        with self.assertRaisesRegex(ValueError, 'future-fitted'):
            model.predict('A', 'B', cutoff - timedelta(days=1))

    def test_empirical_benchmark_is_past_only_and_smoothing_prevents_certainties(self):
        rows = sample(); cutoff = rows[170].match.day
        first = empiricalLeague(rows, cutoff)
        changed = [replace(r, home_corners=40, away_corners=40) if r.match.day >= cutoff else r for r in rows]
        self.assertEqual(first, empiricalLeague(changed, cutoff))
        self.assertEqual(first['fit']['training_games'], 170)
        self.assertEqual(first['as_of'], cutoff.isoformat())
        self.assertEqual(first['training_cutoff_exclusive'], cutoff.isoformat())
        for event in first['totals'].values():
            self.assertGreater(event['over'], 0)
            self.assertLess(event['over'], 1)


if __name__ == '__main__':
    unittest.main()
