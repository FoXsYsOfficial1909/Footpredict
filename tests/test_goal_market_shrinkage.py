from datetime import date, timedelta
import unittest

from research.goal_market_half_life import weighted_signals
from research.goal_market_shrinkage import has_limited_recent_history


class GoalMarketShrinkageTest(unittest.TestCase):
    def setUp(self):
        self.day = date(2025, 5, 5)
        self.target = ['E0', self.day.isoformat(), 2024, 'A', 'B', 6, 7]
        self.plan = {'leadHours': 24, 'resultSafetyHours': 4, 'rateBounds': [.05, 5],
                     'settings': {'priorSeasonWeight': .35, 'shrinkageGames': 5},
                     'recentHistory': {'windowDays': 120, 'maximumGamesOnEitherSide': 5}}

    def test_stronger_smoothing_moves_goal_rates_toward_league_average(self):
        before = self.day - timedelta(days=2)
        history = [(before, 2024, 'A', 'B', 4, 1), (before, 2024, 'C', 'D', 0, 1)]
        stronger = {**self.plan, 'settings': {**self.plan['settings'], 'shrinkageGames': 10}}
        original, base = weighted_signals(self.target, history, self.plan, 120)
        adjusted, other_base = weighted_signals(self.target, history, stronger, 120)
        self.assertGreater(original[0], adjusted[0])
        self.assertGreater(adjusted[0], 2.)
        self.assertAlmostEqual(adjusted[1], 1.)
        self.assertEqual(base, other_base)

    def test_recent_history_uses_the_team_venue_and_forecast_cutoff(self):
        def match(age, host='A', visitor='B'):
            return (self.day - timedelta(days=age), 2024, host, visitor, 1, 1)

        history = [match(age) for age in range(2, 7)]
        history += [match(1), match(121), match(8, 'B', 'A')]
        self.assertTrue(has_limited_recent_history(self.target, history, self.plan))
        self.assertFalse(has_limited_recent_history(self.target, history + [match(120)], self.plan))
