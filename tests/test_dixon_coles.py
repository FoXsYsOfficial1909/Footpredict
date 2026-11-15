import copy
from datetime import date, timedelta
import math
import unittest

import numpy as np

from research.dixon_coles import fit_rho, probabilities, score_matrix, tau
from research.models import score_probabilities


class DixonColesTests(unittest.TestCase):
    def row(self, score=(0, 0), rates=(1.4, 1.1), day='2022-10-01'):
        return {'date':day, 'rate_cutoff':(date.fromisoformat(day)-timedelta(days=1)).isoformat(),
                'home_goals':score[0], 'away_goals':score[1], 'home_rate':rates[0], 'away_rate':rates[1]}

    def test_zero_rho_reproduces_existing_poisson_and_score_axes(self):
        for h, a in ((.05, .05), (1.4, 1.1), (5., 5.), (.2, 4.9), (10., 1.)):
            p = probabilities(h, a)
            np.testing.assert_allclose(p, score_probabilities(h, a), rtol=0, atol=2e-15)
            self.assertAlmostEqual(math.fsum(p), 1., places=14)
        self.assertGreater(probabilities(3., .5)[0], probabilities(3., .5)[2])
        left, right = probabilities(1.7, .9, -.1), probabilities(.9, 1.7, -.1)
        np.testing.assert_allclose(left, right[::-1], rtol=0, atol=2e-15)

    def test_prescribed_four_cells_conserve_mass_and_change_only_low_scores(self):
        h, a, rho = 1.3, .8, -.12
        original, corrected = score_matrix(h, a), score_matrix(h, a, rho)
        factors = {(0,0):1-h*a*rho, (0,1):1+h*rho, (1,0):1+a*rho, (1,1):1-rho}
        for i, row in enumerate(original):
            for j, value in enumerate(row):
                self.assertAlmostEqual(corrected[i][j], value*factors.get((i,j),1.), places=15)
                self.assertGreater(corrected[i][j], 0)
        self.assertAlmostEqual(math.fsum(v for row in corrected for v in row), 1., places=14)
        p, q = probabilities(h, a), probabilities(h, a, rho)
        # Four-cell cancellation gives the exact three-class correction.
        delta = rho*original[1][1]
        np.testing.assert_allclose(np.array(q)-p, [delta, -2*delta, delta], rtol=0, atol=2e-15)
        self.assertGreater(q[1], p[1])
        self.assertEqual(tau(4, 3, h, a, rho), 1.)

    def test_fit_recovers_known_correlation_from_deterministic_score_frequencies(self):
        expected = -.09; matrix = score_matrix(1.25, .95, expected)
        rows = []
        for h, line in enumerate(matrix):
            for a, p in enumerate(line):
                rows.extend([self.row((h,a),(1.25,.95)) for _ in range(round(30000*p))])
        result = fit_rho(rows, date(2023,7,1), window_days=1826, bounds=(-.15,.039999))
        self.assertAlmostEqual(result['rho'], expected, delta=.002)
        self.assertTrue(result['fit']['identified']); self.assertFalse(result['fit']['at_boundary'])
        self.assertGreater(result['fit']['log_likelihood_gain_over_independence'], 0)
        self.assertEqual(result['fit']['training_games'],len(rows))
        self.assertEqual(result['fit']['requested_bounds'],[-.15,.039999])

    def test_future_results_and_target_rates_are_excluded_before_fitting(self):
        rows = [self.row((0,0)), self.row((1,0)), self.row((0,1)), self.row((1,1))]*30
        cutoff = date(2023,7,1); before = copy.deepcopy(rows)
        first = fit_rho(rows,cutoff)
        future = [{'date':day} for day in ('2023-07-01','2023-07-02','2025-01-01','2010-01-01')]
        after = fit_rho(rows+future,cutoff)
        self.assertEqual(first,after); self.assertEqual(rows,before)
        self.assertLess(first['fit']['latest_training_date'],first['fit']['cutoff_exclusive'])
        leaked = self.row(); leaked['rate_cutoff']=leaked['date']
        with self.assertRaises(ValueError):fit_rho(rows+[leaked],cutoff)
        missing = self.row(); del missing['rate_cutoff']
        with self.assertRaises(KeyError):fit_rho(rows+[missing],cutoff)

    def test_positivity_uses_all_four_cells_and_prediction_never_clips_rho(self):
        # All observed scores are 0:0, but 0:1 and 1:0 must remain positive too.
        rows = [self.row((0,0),(5.,5.))]*100
        result = fit_rho(rows,date(2023,7,1),bounds=(-.5,.5))
        self.assertGreater(result['rho'],-.2)
        self.assertLess(result['fit']['feasible_bounds'][1],.04)
        self.assertTrue(result['fit']['at_boundary'])
        self.assertGreater(tau(0,1,5.,5.,result['rho']),0)
        with self.assertRaises(ValueError):probabilities(5.,5.,.04)
        with self.assertRaises(ValueError):probabilities(5.,5.,-.2)
        # A proposed interval entirely outside the feasible range is rejected.
        with self.assertRaises(ValueError):fit_rho(rows,date(2023,7,1),bounds=(.05,.2))

    def test_time_weights_and_window_change_only_the_available_score_evidence(self):
        old = [self.row((0,1),day='2022-01-01'),self.row((1,0),day='2022-01-01')]*60
        recent = [self.row((0,0),day='2023-06-01')]*20
        bounds = (-.15,.039999); cutoff = date(2023,7,1)
        decayed = fit_rho(old+recent,cutoff,half_life=30,bounds=bounds)
        slow = fit_rho(old+recent,cutoff,half_life=10000,bounds=bounds)
        self.assertEqual(decayed['rho'],bounds[0]);self.assertEqual(slow['rho'],bounds[1])
        self.assertLess(decayed['fit']['effective_sample_size'],slow['fit']['effective_sample_size'])
        windowed = fit_rho(old+recent,cutoff,window_days=60,bounds=bounds,minimum_matches=20)
        self.assertEqual(windowed['rho'],bounds[0]);self.assertEqual(windowed['fit']['training_games'],20)

    def test_empty_or_uninformative_data_and_invalid_inputs_are_explicit(self):
        with self.assertRaises(ValueError):fit_rho([],date(2023,7,1))
        rows = [self.row((2,2))]*100
        result = fit_rho(rows,date(2023,7,1))
        self.assertEqual(result['rho'],0.)
        self.assertFalse(result['fit']['identified']);self.assertEqual(result['fit']['informative_games'],0)
        self.assertEqual(result['fit']['log_likelihood_gain_over_independence'],0.)
        for h, a, rho in ((0,1,0),(1,float('nan'),0),(True,1,0),(1,1,float('inf'))):
            with self.subTest(h=h,a=a,rho=rho), self.assertRaises(ValueError):probabilities(h,a,rho)
        for bounds in ((.2,.1),(float('nan'),.1),(0.,0.)):
            with self.assertRaises(ValueError):fit_rho(rows,date(2023,7,1),bounds=bounds)
        for limit in (0,21,True):
            with self.assertRaises(ValueError):score_matrix(1.,1.,limit=limit)


if __name__ == '__main__':unittest.main()
