from datetime import date
import unittest

from research.dixon_coles import score_matrix
from research.goal_market_dixon import both_score, evaluate, score_rate_pair


class GoalMarketDixonTest(unittest.TestCase):
    def test_btts_only_receives_the_change_in_the_one_one_cell(self):
        home, away, rho = 1.5, 1.2, .03
        original = both_score(home, away, 0., 20)
        mass_one_one = score_matrix(home, away, 0., 20)[1][1]
        self.assertAlmostEqual(both_score(home, away, rho, 20), original - rho * mass_one_one, places=14)

    def test_zero_correction_reproduces_both_references(self):
        rows = []
        for day, actual in [('2023-08-19', True), ('2023-08-20', False)]:
            rates = (1.6, 1.1)
            rows.append({'day': day, 'expectedGoals': rates, 'events': {'bothScore': actual},
                         'poisson': {'bothScore': both_score(*rates, 0., 20)}, 'base': {'bothScore': .5}})
        plan = {'market': 'bothScore', 'scoreLimit': 20, 'bootstrapSeed': 1, 'bootstrapSamples': 20}
        weights = {'intercept': .1, 'slope': .2}
        report = evaluate(rows, 2, plan, weights, 0.)
        self.assertEqual(report['models']['poisson'], report['models']['dixon'])
        self.assertEqual(report['models']['calibrated'], report['models']['calibratedDixon'])
        rows[0]['poisson']['bothScore'] += .01
        with self.assertRaisesRegex(ValueError, 'Worker reference'):
            evaluate(rows, 2, plan, weights, 0.)

    def test_training_pair_retains_the_forecast_date_and_observed_score(self):
        pair = score_rate_pair({'day': '2023-05-28', 'expectedGoals': (1.6, 1.1), 'score': {'home': 1, 'away': 0}})
        self.assertEqual((date.fromisoformat(pair['date']) - date.fromisoformat(pair['rate_cutoff'])).days, 1)
        self.assertEqual((pair['home_rate'], pair['away_rate']), (1.6, 1.1))
        self.assertEqual((pair['home_goals'], pair['away_goals']), (1, 0))
