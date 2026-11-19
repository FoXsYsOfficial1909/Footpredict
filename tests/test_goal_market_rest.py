import unittest

import numpy as np

from research.goal_market_rest import adjust, fit_residual, short_gap_teams


class GoalMarketRestTest(unittest.TestCase):
    def test_counts_only_short_gaps_and_preserves_unknown_history(self):
        row = {'day': '2026-11-20', 'lastLeagueMatch': {'home': '2026-11-18', 'away': '2026-11-16'}}
        self.assertEqual(short_gap_teams(row, 3), 1)
        row['lastLeagueMatch']['away'] = '2026-11-17'
        self.assertEqual(short_gap_teams(row, 3), 2)
        row['lastLeagueMatch']['away'] = None
        self.assertIsNone(short_gap_teams(row, 3))

    def test_rejects_games_after_the_information_cutoff(self):
        for day in ('2026-11-19', '2026-11-20', '2026-11-21'):
            with self.subTest(day=day), self.assertRaisesRegex(ValueError, 'cutoff'):
                short_gap_teams({'day': '2026-11-20', 'lastLeagueMatch': {'home': day, 'away': None}}, 3)

    def test_fits_a_residual_and_only_adjusts_short_gaps(self):
        calibrated = np.array([.5, .5, .5, .5, .5, .5])
        actual = np.array([0, 0, 1, 1, 1, 1])
        short = np.array([0, 0, 1, 1, 1, 2])
        model = fit_residual(calibrated, actual, short, .01)
        self.assertGreater(model['coefficient'], 0)
        self.assertLess(model['fittedObjective'], model['initialObjective'])
        p = adjust(calibrated, short, model['coefficient'])
        np.testing.assert_array_equal(p[:2], calibrated[:2])
        self.assertGreater(p[-1], p[-2])
