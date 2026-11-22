from datetime import date
import unittest

import numpy as np

from research.goal_market_opponent_control import expected_goals, team_factors


class OpponentExposureTest(unittest.TestCase):
    def test_joint_scoring_factors_satisfy_both_weighted_updates(self):
        home = np.array([0, 0, 1])
        away = np.array([1, 2, 2])
        weights = np.array([1., .35, 1.])
        goals = np.array([0, 3, 1])
        mean = np.average(goals, weights=weights)
        (raw_home, _), (attack, defence) = team_factors(goals, weights, home, away, 3, mean, 5)
        home_goals = np.bincount(home, weights=weights * goals, minlength=3) / mean
        away_goals = np.bincount(away, weights=weights * goals, minlength=3) / mean
        expected_attack = (home_goals + 5) / (np.bincount(home, weights=weights * defence[away], minlength=3) + 5)
        expected_defence = (away_goals + 5) / (np.bincount(away, weights=weights * attack[home], minlength=3) + 5)
        np.testing.assert_allclose(attack, expected_attack, rtol=0, atol=1e-11)
        np.testing.assert_allclose(defence, expected_defence, rtol=0, atol=1e-11)
        self.assertNotAlmostEqual(raw_home[0], attack[0])

    def test_goal_estimates_ignore_results_inside_the_replay_cutoff(self):
        plan = {'leadHours': 24, 'resultSafetyHours': 4, 'rateBounds': [.05, 5],
                'settings': {'halfLifeDays': 120, 'priorSeasonWeight': .35, 'shrinkageGames': 5}}
        target = ['E0', '2025-05-05', 2024, 'A', 'B', 6, 7]
        history = [(date(2025, 5, 1), 2024, 'A', 'C', 3, 1),
                   (date(2025, 5, 3), 2024, 'C', 'B', 1, 4)]
        late = [(date(2025, 5, 4), 2024, 'A', 'C', 10, 9),
                (date(2025, 5, 5), 2024, 'A', 'B', 6, 7)]
        np.testing.assert_array_equal(expected_goals(target, history, plan), expected_goals(target, history + late, plan))
