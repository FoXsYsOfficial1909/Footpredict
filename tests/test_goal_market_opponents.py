import unittest

from research.goal_market_opponents import join_rate


class OpponentRateJoinTest(unittest.TestCase):
    def test_requires_exact_fixture_score_and_past_only_rates(self):
        fixture = ['E0', '2024-10-13', 2024, 'A', 'B', 1, 0]
        row = {'day': fixture[1], 'score': {'home': 1, 'away': 0}}
        key = (fixture[1], 'A', 'B')
        rate = {'season': 2024, 'rate_cutoff': '2024-10-12', 'latestTrainingDate': '2024-10-11',
                'home_goals': 1, 'away_goals': 0, 'home_rate': 1.3, 'away_rate': 1.1}
        self.assertTrue(0 < join_rate(row, 2024, fixture, {key: rate}) < 1)
        for change in ({'latestTrainingDate': '2024-10-12'}, {'home_goals': 2}, {'season': 2023}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                join_rate(row, 2024, fixture, {key: {**rate, **change}})
        with self.assertRaises(KeyError):
            join_rate(row, 2024, fixture, {(fixture[1], 'B', 'A'): rate})
