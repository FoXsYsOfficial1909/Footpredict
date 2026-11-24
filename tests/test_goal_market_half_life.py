from datetime import date, timedelta
import unittest

import numpy as np

from research.goal_market_half_life import weighted_signals


class GoalMarketHalfLifeTest(unittest.TestCase):
    def setUp(self):
        self.day = date(2025, 5, 5)
        self.target = ['E0', self.day.isoformat(), 2024, 'A', 'B', 6, 7]
        self.history = [(self.day - timedelta(days=120), 2024, 'A', 'B', 0, 1),
                        (self.day - timedelta(days=360), 2023, 'A', 'B', 4, 2)]
        self.plan = {'leadHours': 24, 'resultSafetyHours': 4, 'rateBounds': [.05, 5],
                     'settings': {'priorSeasonWeight': .35, 'shrinkageGames': 5}}

    def test_old_matches_receive_more_weight_with_the_longer_half_life(self):
        for half_life in (120, 365):
            with self.subTest(half_life=half_life):
                recent = .5 ** (120 / half_life)
                old = .35 * .5 ** (360 / half_life)
                rates, frequency = weighted_signals(self.target, self.history, self.plan, half_life)
                np.testing.assert_allclose(rates, [4 * old / (recent + old), (recent + 2 * old) / (recent + old)])
                self.assertAlmostEqual(frequency, (1 + old) / (2 + recent + old))

    def test_longer_half_life_preserves_the_prediction_cutoff(self):
        inside = [(self.day - timedelta(days=1), 2024, 'A', 'B', 10, 10),
                  (self.day, 2024, 'A', 'B', 6, 7)]
        rates, frequency = weighted_signals(self.target, self.history, self.plan, 365)
        other_rates, other_frequency = weighted_signals(self.target, self.history + inside, self.plan, 365)
        np.testing.assert_array_equal(rates, other_rates)
        self.assertEqual(frequency, other_frequency)
