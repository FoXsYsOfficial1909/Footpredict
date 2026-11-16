import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from research.neural_residual import (fit_model, predict_model, objective, parameter_count,
                                    MODEL_VERSION, MAX_PARAMETERS)


SETTINGS = {'hidden_units': 16, 'l2': .01, 'max_iter': 200, 'seed': 42, 'feature_clip': 5.}


class NeuralResidualTests(unittest.TestCase):
    def data(self):
        x = np.tile(np.array([[-1., -1.], [-1., 1.], [1., -1.], [1., 1.]]), (20, 1))
        y = np.tile(np.array([0, 2, 2, 0]), 20)
        base = np.tile(np.array([.4, .2, .4]), (len(x), 1))
        return x, base, y

    def test_analytic_gradient_matches_finite_difference_for_all_parameters(self):
        rng = np.random.default_rng(3); n, d, h = 7, 3, 4
        x = rng.normal(size=(n, d)); base = rng.dirichlet([2., 2., 2.], size=n)
        labels = np.array([0, 1, 2, 0, 2, 1, 0]); weights = rng.uniform(.2, 1, n); weights /= weights.sum()
        theta = rng.normal(0, .2, parameter_count(d, h))
        args = (x, np.log(base), labels, weights, .01, h)
        _, analytic = objective(theta, *args)
        numerical = []
        for i in range(len(theta)):
            left, right = theta.copy(), theta.copy(); left[i] -= 1e-6; right[i] += 1e-6
            numerical.append((objective(right, *args)[0]-objective(left, *args)[0])/2e-6)
        np.testing.assert_allclose(analytic, numerical, rtol=1e-5, atol=2e-9)

    def test_zero_output_initialization_is_the_base_and_iteration_limit_is_explicit(self):
        x, base, y = self.data(); captured = {}
        def initial_only(fun, theta, args, **kwargs):
            value, gradient = fun(theta, *args); captured['theta'] = theta.copy()
            return SimpleNamespace(x=theta, fun=value, jac=gradient, status=1, success=False, nit=1, nfev=1)
        settings = {**SETTINGS, 'max_iter': 1}
        with patch('research.neural_residual.minimize', side_effect=initial_only):
            model = fit_model(x, base, y, settings=settings)
        self.assertFalse(model['fit']['converged']); self.assertEqual(model['fit']['stopReason'], 'iteration_limit')
        self.assertEqual(model['fit']['optimizerStatus'], 1)
        np.testing.assert_allclose(predict_model(model, x, base), base, atol=2e-16, rtol=0)
        self.assertTrue(np.all(captured['theta'][-(SETTINGS['hidden_units']*3+3):] == 0))

    def test_small_network_learns_a_nonlinear_synthetic_relation_and_exports_exactly(self):
        x, base, y = self.data()
        model = fit_model(x, base, y, settings={**SETTINGS, 'l2': .0001})
        p = np.array(predict_model(json.loads(json.dumps(model, allow_nan=False)), x, base))
        self.assertEqual(model['modelVersion'], MODEL_VERSION)
        self.assertLess(model['fit']['finalDataLogLoss'], model['fit']['initialDataLogLoss']-.2)
        self.assertGreater(np.mean(np.argmax(p, axis=1) == y), .95)
        np.testing.assert_allclose(p.sum(axis=1), 1, atol=2e-15, rtol=0)
        self.assertTrue(np.all(p > 0)); self.assertFalse(model['promotedToDefault'])
        self.assertEqual(parameter_count(56, 16), 963)
        self.assertLessEqual(model['fit']['parameterCount'], MAX_PARAMETERS)

    def test_training_scaler_is_weighted_training_only_and_prediction_does_not_mutate_it(self):
        x = np.array([[0., 7.], [2., 7.], [20., 7.], [99., 7.]])
        base = np.tile([.5, .25, .25], (4, 1)); labels = np.array([0, 1, 2, 0]); weights = np.array([1., 3., 2., 0.])
        saved = (x.copy(), base.copy(), labels.copy(), weights.copy())
        model = fit_model(x, base, labels, settings=SETTINGS, sample_weights=weights)
        self.assertAlmostEqual(model['featureMean'][0], 46/6)
        self.assertAlmostEqual(model['featureMean'][1], 7.)
        self.assertEqual(model['featureScale'][1], 1.)
        before = copy.deepcopy(model)
        extreme = [[1e9, 7.], [-1e9, 7.]]
        p = predict_model(model, extreme, [[.5, .25, .25]]*2)
        self.assertEqual(model, before); self.assertTrue(np.all(np.isfinite(p)))
        for actual, expected in zip((x, base, labels, weights), saved): np.testing.assert_array_equal(actual, expected)

    def test_same_seed_and_training_rows_reproduce_the_same_weights(self):
        x, base, y = self.data()
        first = fit_model(x, base, y, settings=SETTINGS)
        second = fit_model(x, base, y, settings=SETTINGS)
        self.assertEqual(first, second)
        self.assertEqual(predict_model(first, x, base), predict_model(second, x, base))

    def test_invalid_inputs_and_corrupt_models_are_rejected(self):
        x, base, y = self.data()
        for changed in ([True]*len(y), y.astype(float), np.full(len(y), 3)):
            with self.assertRaises(ValueError): fit_model(x, base, changed, settings=SETTINGS)
        for probabilities in (np.ones_like(base), np.full_like(base, np.nan), np.tile([1, 0, 0], (len(x), 1))):
            with self.assertRaises(ValueError): fit_model(x, probabilities, y, settings=SETTINGS)
        for weights in (np.zeros(len(y)), np.full(len(y), -1.), np.full(len(y), np.inf)):
            with self.assertRaises(ValueError): fit_model(x, base, y, settings=SETTINGS, sample_weights=weights)
        bad = x.copy(); bad[0, 0] = np.nan
        with self.assertRaises(ValueError): fit_model(bad, base, y, settings=SETTINGS)
        with self.assertRaises(ValueError): fit_model(np.zeros((len(y), 59)), base, y, settings=SETTINGS)
        model = fit_model(x, base, y, settings=SETTINGS)
        for mutation in ({'modelVersion':'other'}, {'promotedToDefault':True}, {'featureScale':[0., 1.]}):
            with self.assertRaises(ValueError): predict_model({**model, **mutation}, x, base)

    def test_nonfinite_or_abnormal_optimizer_termination_is_not_a_completed_fit(self):
        x, base, y = self.data()
        def broken(fun, theta, args, **kwargs):
            value, gradient = fun(theta, *args)
            return SimpleNamespace(x=theta, fun=value, jac=gradient, status=2, success=False, nit=1, nfev=1)
        with patch('research.neural_residual.minimize', side_effect=broken):
            with self.assertRaises(RuntimeError): fit_model(x, base, y, settings=SETTINGS)


if __name__ == '__main__': unittest.main()
