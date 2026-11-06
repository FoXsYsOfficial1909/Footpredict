from datetime import date, timedelta
import unittest

import numpy as np

from research.workload_model import adjust, fit_workload


def examples(count=400):
    rows = []
    for i in range(count):
        actual = i % 3
        direction = 1. if actual == 0 else -1. if actual == 2 else 0.
        rows.append({'date': (date(2024, 1, 1) + timedelta(days=i)).isoformat(),
                     'actual': actual, 'baseline': [1 / 3, 1 / 3, 1 / 3],
                     'workload': [direction, 0., 0.]})
    return rows


class WorkloadModelTests(unittest.TestCase):
    def test_target_future_and_expired_examples_cannot_change_the_fit(self):
        rows = examples()
        cutoff = date(2025, 3, 1)
        first = fit_workload(rows, cutoff)
        unusable = [{'date': cutoff.isoformat(), 'actual': 'future outcome', 'workload': [float('nan')]},
                    {'date': (cutoff + timedelta(days=1)).isoformat(), 'actual': 1, 'workload': 'future'},
                    {'date': (cutoff - timedelta(days=1461)).isoformat(), 'workload': 'expired'}]
        self.assertEqual(first, fit_workload(rows + unusable, cutoff))
        self.assertLess(first['latestTrainingMatch'], first['cutoffExclusive'])
        self.assertEqual(first['trainingRows'], 400)

    def test_past_workload_features_have_a_bounded_effect(self):
        rows = examples()
        fitted = fit_workload(rows, date(2025, 3, 1), ridge=.05)
        flipped = [{**row, 'workload': [-row['workload'][0], 0., 0.]} for row in rows]
        after = fit_workload(flipped, date(2025, 3, 1), ridge=.05)
        self.assertEqual(fitted['status'], 'fitted')
        self.assertGreater(fitted['beta'][0], 0.)
        self.assertLess(after['beta'][0], 0.)
        self.assertTrue(all(abs(value) <= .75 for value in fitted['beta']))
        prediction = adjust([1 / 3] * 3, [1., 0., 0.], fitted)
        self.assertGreater(prediction[0], 1 / 3)
        self.assertLess(prediction[2], 1 / 3)
        self.assertAlmostEqual(sum(prediction), 1.)

    def test_zero_correction_and_missing_coverage_preserve_base_exactly(self):
        for base in ([.5, .3, .2], [1., 0., 0.]):
            self.assertEqual(adjust(base, [1., -1., .5], {'beta': [0., 0., 0.]}), base)
            self.assertEqual(adjust(base, None, {'beta': [.75, -.4, .2]}), base)

    def test_insufficient_covered_history_is_explicit_identity_without_imputation(self):
        rows = examples()
        covered = [{**row, 'workload': None} if i >= 299 else row for i, row in enumerate(rows)]
        model = fit_workload(covered, date(2025, 3, 1))
        self.assertEqual(model['status'], 'insufficient_history')
        self.assertEqual(model['trainingRows'], 299)
        self.assertEqual(model['missingWorkloadRows'], 101)
        self.assertEqual(model['eligibleRows'], 400)
        self.assertEqual(model['beta'], [0., 0., 0.])
        self.assertEqual(adjust([.5, .3, .2], [2., -1., -1.], model), [.5, .3, .2])
        empty = fit_workload([{**row, 'workload': None} for row in rows], date(2025, 3, 1))
        self.assertIsNone(empty['latestTrainingMatch'])
        self.assertEqual(empty['trainingRows'], 0)

    def test_invalid_observations_and_settings_are_rejected(self):
        for vector in ([1., 2.], [1., float('nan'), 0.], [1., float('inf'), 0.]):
            with self.assertRaises(ValueError):
                adjust([.5, .3, .2], vector, {'beta': [0., 0., 0.]})
            with self.assertRaises(ValueError):
                fit_workload([{**examples(1)[0], 'workload': vector}], date(2025, 3, 1))
        for settings in ({'ridge': 0}, {'half_life': float('inf')}, {'window_days': -1}):
            with self.assertRaises(ValueError):
                fit_workload(examples(), date(2025, 3, 1), **settings)
        with self.assertRaises(ValueError):
            adjust([.5, .3, .2], [1., 0., 0.], {'beta': [1., 0., 0.]})
        with self.assertRaises(ValueError):
            fit_workload([{**examples(1)[0], 'actual': True}], date(2025, 3, 1))


if __name__ == '__main__':
    unittest.main()
