"""Small past-only workload correction over fixed out-of-sample probabilities.

The base xG model and its ensemble weight are inputs, never selected here.
The three supplied workload coordinates are rest difference / 7, negative
seven-day match-count difference / 3, and negative fourteen-day difference / 5.
"""
from __future__ import annotations

from datetime import date, datetime
import math

import numpy as np
from scipy.optimize import minimize
from scipy.special import logsumexp

from research.models import loss

MIN_TRAINING_ROWS = 300


def calendar_day(value):
    if isinstance(value, datetime):
        raise ValueError('Workload fitting requires a calendar-date cutoff')
    return value if isinstance(value, date) else date.fromisoformat(value)


def probabilities(value):
    loss(value, 0)
    return np.asarray(value, dtype=float)


def workload_vector(value):
    vector = np.asarray(value, dtype=float)
    if vector.shape != (3,) or not np.all(np.isfinite(vector)):
        raise ValueError('A workload vector must have three finite coordinates')
    return vector


def softmax(logits):
    values = np.exp(logits - np.max(logits, axis=-1, keepdims=True))
    return values / np.sum(values, axis=-1, keepdims=True)


def adjust(base_probabilities, vector, model):
    """Apply a frozen correction, with an exact fallback for missing coverage."""
    base = probabilities(base_probabilities)
    if vector is None:
        return list(base_probabilities)
    features = workload_vector(vector)
    beta = workload_vector(model['beta'])
    if np.any(np.abs(beta) > .75 + 1e-12):
        raise ValueError('Workload coefficients exceed the registered bounds')
    if not np.any(beta):
        return list(base_probabilities)
    delta = float(beta @ features)
    logits = np.log(np.maximum(1e-12, base)) + np.array([delta, 0., -delta])
    return softmax(logits).tolist()


def fit_workload(examples, cutoff, *, ridge=.2, half_life=730, window_days=1460):
    """Fit three bounded coefficients using only earlier covered OOF examples.

    Each example contains date, actual (0/1/2), baseline (H/D/A probabilities),
    and workload (three coordinates or None when coverage is incomplete).
    No missing workload is imputed and no future label is inspected.
    """
    cutoff = calendar_day(cutoff)
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool)
               and math.isfinite(value) and value > 0 for value in (ridge, half_life, window_days)):
        raise ValueError('Workload fitting settings must be positive and finite')
    prior = [row for row in examples
             if 0 < (cutoff - calendar_day(row['date'])).days <= window_days]
    covered = [row for row in prior if row.get('workload') is not None]
    # Validate every used observation, including small histories that fall back.
    for row in covered:
        workload_vector(row['workload'])
        probabilities(row['baseline'])
        actual = row['actual']
        if isinstance(actual, bool) or not isinstance(actual, (int, np.integer)) or actual not in (0, 1, 2):
            raise ValueError('Workload examples require a valid observed outcome')
    model = {
        'model_version': 'workload-residual-0.1', 'beta': [0., 0., 0.],
        'status': 'insufficient_history', 'requiredTrainingRows': MIN_TRAINING_ROWS,
        'trainingRows': len(covered), 'eligibleRows': len(prior),
        'missingWorkloadRows': len(prior) - len(covered),
        'latestTrainingMatch': max((calendar_day(row['date']).isoformat() for row in covered), default=None),
        'cutoffExclusive': cutoff.isoformat(),
        'settings': {'ridge': ridge, 'half_life': half_life, 'window_days': window_days,
                     'beta_bound': .75},
    }
    if len(covered) < MIN_TRAINING_ROWS:
        model['reason'] = 'Fewer than 300 earlier examples have complete workload coverage; use the fixed base forecast.'
        return model
    features = np.array([row['workload'] for row in covered], dtype=float)
    logs = np.log(np.maximum(1e-12, np.array([row['baseline'] for row in covered], dtype=float)))
    labels = np.array([row['actual'] for row in covered], dtype=int)
    target = np.eye(3)[labels]
    weights = np.exp(-math.log(2) * np.array([(cutoff - calendar_day(row['date'])).days for row in covered]) / half_life)
    weights /= weights.sum()

    def objective(beta):
        delta = features @ beta
        logits = logs + delta[:, None] * np.array([1., 0., -1.])
        p = softmax(logits)
        cross_entropy = np.sum(weights * (logsumexp(logits, axis=1) - logits[np.arange(len(labels)), labels]))
        residual = weights * ((p[:, 0] - target[:, 0]) - (p[:, 2] - target[:, 2]))
        gradient = features.T @ residual + ridge * beta
        return cross_entropy + ridge * float(beta @ beta) / 2, gradient

    fitted = minimize(objective, np.zeros(3), jac=True, method='L-BFGS-B',
                      bounds=[(-.75, .75)] * 3,
                      options={'maxiter': 200, 'ftol': 1e-12, 'gtol': 1e-8})
    if not fitted.success or not np.all(np.isfinite(fitted.x)):
        raise RuntimeError('Workload correction did not converge: ' + str(fitted.message))
    model.update(beta=fitted.x.tolist(), status='fitted', optimizerIterations=int(fitted.nit))
    return model
