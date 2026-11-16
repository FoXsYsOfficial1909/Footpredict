"""A small, deterministic neural correction to frozen pre-match probabilities.

The caller owns chronological feature generation and training splits.  This
module fits a scaler only to supplied training rows and never receives future
labels or a validation set.  Starting output weights are zero, so the initial
network prediction equals the supplied positive base distribution.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.optimize import minimize
from scipy.special import logsumexp


MODEL_VERSION = 'neural-residual-1'
SETTING_KEYS = {'hidden_units', 'l2', 'max_iter', 'seed', 'feature_clip'}
HIDDEN_BIAS_STD = .05
PARAMETER_BOUND = 3.
MAX_PARAMETERS = 1000
FTOL = 1e-10
GTOL = 1e-6
MAXLS = 30


def _settings(settings):
    if not isinstance(settings, dict) or set(settings) not in (SETTING_KEYS, SETTING_KEYS|{'hidden_bias_std'}):
        raise ValueError('Unexpected neural settings')
    h, budget, seed = settings['hidden_units'], settings['max_iter'], settings['seed']
    if (type(h) is not int or not 1 <= h <= 32 or type(budget) is not int or not 1 <= budget <= 500
            or type(seed) is not int or not 0 <= seed < 2**32
            or type(settings['l2']) not in (int, float) or not math.isfinite(settings['l2']) or settings['l2'] <= 0
            or type(settings['feature_clip']) not in (int, float)
            or not math.isfinite(settings['feature_clip']) or not 0 < settings['feature_clip'] <= 10):
        raise ValueError('Invalid bounded neural settings')
    if settings.get('hidden_bias_std', HIDDEN_BIAS_STD) != HIDDEN_BIAS_STD:
        raise ValueError('The fixed hidden-bias initialization may not be tuned')
    return {**settings, 'hidden_bias_std': HIDDEN_BIAS_STD}


def parameter_count(features, hidden_units):
    return (features+1)*hidden_units+(hidden_units+1)*3


def _matrix(values, columns=None, name='features'):
    raw = np.asarray(values)
    if raw.dtype.kind not in 'fiu' or raw.ndim != 2 or raw.shape[0] < 1 or raw.shape[1] < 1:
        raise ValueError('Expected a nonempty numeric '+name+' matrix')
    result = np.array(raw, dtype=np.float64, copy=True)
    if not np.all(np.isfinite(result)) or (columns is not None and result.shape[1] != columns):
        raise ValueError('Invalid '+name+' matrix')
    return result


def _probabilities(values, rows):
    result = _matrix(values, 3, 'base probability')
    if (len(result) != rows or np.any(result <= 0) or np.any(result > 1)
            or np.any(np.abs(result.sum(axis=1)-1) > 1e-8)):
        raise ValueError('Base probabilities must be three positive normalized classes')
    return result/result.sum(axis=1, keepdims=True)


def _weights(values, rows):
    if values is None:
        result = np.ones(rows, dtype=np.float64)
    else:
        raw = np.asarray(values)
        if raw.dtype.kind not in 'fiu' or raw.shape != (rows,):
            raise ValueError('Sample weights must match the training rows')
        result = np.array(raw, dtype=np.float64, copy=True)
    if not np.all(np.isfinite(result)) or np.any(result < 0):
        raise ValueError('Sample weights must be finite and nonnegative')
    mass = float(result.sum())
    if not math.isfinite(mass) or mass <= 0:
        raise ValueError('Training rows need positive total weight')
    return result/mass, mass


def _labels(values, rows):
    result = np.asarray(values)
    if result.dtype.kind not in 'iu' or result.shape != (rows,) or np.any(result < 0) or np.any(result > 2):
        raise ValueError('Labels must be integer P1/draw/P2 indices')
    return np.array(result, dtype=np.int64, copy=True)


def unpack(theta, features, hidden_units):
    """Unpack the one hidden layer; order is also used by the saved model."""
    a = features*hidden_units
    b = a+hidden_units
    c = b+hidden_units*3
    return theta[:a].reshape(features, hidden_units), theta[a:b], theta[b:c].reshape(hidden_units, 3), theta[c:]


def objective(theta, x, log_base, labels, weights, l2, hidden_units):
    """Weighted mean cross entropy plus 0.5*l2*sum(theta**2), with its gradient.

    Inputs are already validated finite arrays; weights are normalized by fit.
    The regularizer covers every neural weight and bias, not the base model.
    """
    w1, b1, w2, b2 = unpack(theta, x.shape[1], hidden_units)
    hidden = np.tanh(x@w1+b1)
    logits = log_base+hidden@w2+b2
    normalizer = logsumexp(logits, axis=1, keepdims=True)
    p = np.exp(logits-normalizer)
    data_loss = -float(np.sum(weights*(logits[np.arange(len(x)), labels]-normalizer[:, 0])))
    value = data_loss+float(.5*l2*np.dot(theta, theta))
    residual = p.copy()
    residual[np.arange(len(x)), labels] -= 1
    residual *= weights[:, None]
    dh = (residual@w2.T)*(1-hidden**2)
    gradient = np.concatenate(((x.T@dh).ravel(), dh.sum(axis=0), (hidden.T@residual).ravel(), residual.sum(axis=0)))
    gradient += l2*theta
    return value, gradient


def _log_loss(theta, x, log_base, labels, weights, hidden_units):
    return objective(theta, x, log_base, labels, weights, 0., hidden_units)[0]


def fit_model(x, base_probabilities, labels, *, settings, sample_weights=None):
    """Fit one bounded residual MLP; a finite optimizer budget is reported honestly.

    All rows, scaler statistics, and sample weights supplied here must belong to
    the caller's earlier training period.  No validation labels select weights,
    scaling, stopping time, or an alternative seed in this module.
    """
    settings = _settings(settings)
    x = _matrix(x)
    n, d = x.shape
    h = settings['hidden_units']
    count = parameter_count(d, h)
    if count > MAX_PARAMETERS:
        raise ValueError('The preregistered small network exceeds 1000 parameters')
    base = _probabilities(base_probabilities, n)
    y = _labels(labels, n)
    weights, original_mass = _weights(sample_weights, n)
    mean = np.sum(weights[:, None]*x, axis=0)
    variance = np.sum(weights[:, None]*(x-mean)**2, axis=0)
    scale = np.sqrt(variance)
    scale[scale < 1e-12] = 1.
    z = np.clip((x-mean)/scale, -settings['feature_clip'], settings['feature_clip'])
    if not np.all(np.isfinite(z)):
        raise ValueError('Feature standardization produced nonfinite values')
    log_base = np.log(base)
    rng = np.random.default_rng(settings['seed'])
    initial = np.zeros(count, dtype=np.float64)
    initial[:d*h] = np.clip(rng.normal(0., math.sqrt(2/(d+h)), size=d*h), -PARAMETER_BOUND, PARAMETER_BOUND)
    initial[d*h:d*h+h] = rng.normal(0., settings['hidden_bias_std'], size=h)
    initial_objective = objective(initial, z, log_base, y, weights, settings['l2'], h)[0]
    initial_loss = _log_loss(initial, z, log_base, y, weights, h)
    result = minimize(objective, initial, args=(z, log_base, y, weights, settings['l2'], h),
        jac=True, method='L-BFGS-B', bounds=[(-PARAMETER_BOUND, PARAMETER_BOUND)]*count,
        options={'maxiter': settings['max_iter'], 'ftol': FTOL, 'gtol': GTOL, 'maxls': MAXLS})
    if (not np.all(np.isfinite(result.x)) or not math.isfinite(float(result.fun))
            or not np.all(np.isfinite(result.jac)) or int(result.status) not in (0, 1)
            or np.any(np.abs(result.x) > PARAMETER_BOUND+1e-9)
            or float(result.fun) > initial_objective+1e-8):
        raise RuntimeError('Residual network optimizer failed to produce a finite bounded fit')
    if result.success:
        reason = 'converged'
    elif int(result.nit) >= settings['max_iter']:
        reason = 'iteration_limit'
    else:
        reason = 'evaluation_limit'
    w1, b1, w2, b2 = unpack(result.x, d, h)
    return {'schemaVersion': 1, 'modelVersion': MODEL_VERSION,
        'architecture': {'inputFeatures': d, 'hiddenUnits': h, 'outputs': 3,
                         'activation': 'tanh', 'residualBase': 'log_probability'},
        'settings': settings, 'featureMean': mean.tolist(), 'featureScale': scale.tolist(),
        'weights': {'input': w1.tolist(), 'hiddenBias': b1.tolist(), 'output': w2.tolist(), 'outputBias': b2.tolist()},
        'fit': {'trainingRows': n, 'parameterCount': count,
            'positiveWeightRows': int(np.count_nonzero(weights)), 'sampleWeightSum': original_mass,
            'effectiveSampleSize': float(1/np.sum(weights**2)),
            'classCounts': [int(np.count_nonzero(y == i)) for i in range(3)],
            'initialDataLogLoss': initial_loss,
            'finalDataLogLoss': _log_loss(result.x, z, log_base, y, weights, h),
            'initialObjective': initial_objective, 'finalObjective': float(result.fun),
            'optimizer': 'L-BFGS-B', 'optimizerStatus': int(result.status),
            'converged': bool(result.success), 'stopReason': reason,
            'iterations': int(result.nit), 'functionEvaluations': int(result.nfev),
            'gradientMaxAbs': float(np.max(np.abs(result.jac))),
            'parameterBound': PARAMETER_BOUND, 'ftol': FTOL, 'gtol': GTOL, 'maxls': MAXLS,
            'scalerFit': 'weighted_training_rows_only', 'initialOutputCorrection': 'zero',
            'initialization': {'inputWeights': 'xavier_normal', 'hiddenBiasStd': settings['hidden_bias_std'],
                               'outputWeights': 'zero', 'outputBias': 'zero', 'seed': settings['seed']},
            'objective': 'weighted mean cross entropy + 0.5*l2*sum(all neural parameters squared)'},
        'promotedToDefault': False}


def predict_model(model, x, base_probabilities):
    """Apply saved weights/scaler without refitting or inspecting any labels."""
    if not isinstance(model, dict) or model.get('schemaVersion') != 1 or model.get('modelVersion') != MODEL_VERSION:
        raise ValueError('Unknown residual network model')
    settings = _settings(model['settings'])
    architecture = model['architecture']
    d, h = architecture.get('inputFeatures'), architecture.get('hiddenUnits')
    if (type(d) is not int or d < 1 or h != settings['hidden_units'] or architecture.get('outputs') != 3
            or architecture.get('activation') != 'tanh' or architecture.get('residualBase') != 'log_probability'
            or parameter_count(d, h) > MAX_PARAMETERS or model.get('promotedToDefault') is not False):
        raise ValueError('Invalid residual network architecture')
    x = _matrix(x, d)
    base = _probabilities(base_probabilities, len(x))
    mean, scale = np.asarray(model['featureMean'], dtype=float), np.asarray(model['featureScale'], dtype=float)
    saved = model['weights']
    w1, b1 = np.asarray(saved['input'], dtype=float), np.asarray(saved['hiddenBias'], dtype=float)
    w2, b2 = np.asarray(saved['output'], dtype=float), np.asarray(saved['outputBias'], dtype=float)
    if (mean.shape != (d,) or scale.shape != (d,) or w1.shape != (d, h) or b1.shape != (h,)
            or w2.shape != (h, 3) or b2.shape != (3,) or np.any(scale <= 0)
            or any(not np.all(np.isfinite(v)) for v in (mean, scale, w1, b1, w2, b2))
            or any(np.any(np.abs(v) > PARAMETER_BOUND+1e-9) for v in (w1, b1, w2, b2))):
        raise ValueError('Invalid saved neural scaler or parameters')
    z = np.clip((x-mean)/scale, -settings['feature_clip'], settings['feature_clip'])
    logits = np.log(base)+np.tanh(z@w1+b1)@w2+b2
    values = np.exp(logits-logsumexp(logits, axis=1, keepdims=True))
    if not np.all(np.isfinite(values)) or np.any(values < 0):
        raise ValueError('Residual network produced invalid probabilities')
    return (values/values.sum(axis=1, keepdims=True)).tolist()
