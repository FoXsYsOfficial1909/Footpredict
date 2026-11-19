import unittest

import numpy as np

from research.goal_market_calibration import experiment, fit, predict, scores


class GoalMarketCalibrationTest(unittest.TestCase):
    def test_fit_learns_from_binary_outcomes(self):
        rows = [{'poisson': {'over25': .8}, 'base': {'over25': .5}, 'events': {'over25': i < 7}}
                for i in range(10)]
        model = fit(rows, 'over25', ridge=.01)
        p = predict(np.array([.8]), np.array([.5]), model)[0]
        self.assertGreater(p, .6)
        self.assertLess(p, .7)
        self.assertLess(model['fittedObjective'], model['initialObjective'])

    def test_scoring_matches_the_worker_brier_scale(self):
        metrics, _ = scores(np.array([.75, .25]), np.array([1, 0]))
        self.assertAlmostEqual(metrics['brier'], .125)
        self.assertAlmostEqual(metrics['logLoss'], -np.log(.75))
        self.assertEqual(metrics['accuracy'], 1.)
        self.assertEqual(sum(group['n'] for group in metrics['calibration']), 2)

    def test_rejects_labels_inside_the_first_forecast_information_fence(self):
        plan = {'league': 'PL', 'trainSeasons': [2022], 'validationSeasons': [2023], 'laterReplaySeasons': []}
        replay = {'league': 'PL', 'periods': [{'season': 2022, 'rows': [{'day': '2023-08-10'}]},
                                             {'season': 2023, 'from': '2023-08-11'}]}
        with self.assertRaisesRegex(ValueError, 'overlap'):
            experiment(replay, plan)
