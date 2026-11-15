"""Auditable Dixon--Coles correction for already estimated goal rates.

This is a separate helper, not a replacement for the older in-sample strength
fit in ``research.calibration.DixonColes``.  A caller fitting rho to predictive
residuals must supply rates estimated before each observed match.  The cutoff
metadata below checks their declared dates; it cannot reconstruct their fit.

For fixed rates the independent Poisson likelihood is constant in rho.  The
four correction terms also conserve total mass exactly, including on a finite
score grid containing all four cells.  Consequently maximizing the weighted
sum of log(tau) estimates rho without re-estimating attack or defence strength.
"""
from __future__ import annotations

from datetime import date, timedelta
import math


MAX_RATE = 10.
MAX_GOALS = 30
POSITIVITY_MARGIN = 1e-12


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _rates(home_rate, away_rate):
    if any(not _number(value) or not 0 < value <= MAX_RATE for value in (home_rate, away_rate)):
        raise ValueError('Goal rates must be finite, positive and at most 10')


def _rho(rho):
    if not _number(rho):
        raise ValueError('The Dixon--Coles coefficient must be finite')


def _score(home_goals, away_goals):
    if any(type(value) is not int or not 0 <= value <= MAX_GOALS for value in (home_goals, away_goals)):
        raise ValueError('Scores must be nonnegative integers at most 30')


def _date(value):
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise ValueError('Expected an ISO calendar date')
    result = date.fromisoformat(value)
    if result.isoformat() != value:
        raise ValueError('Expected an ISO calendar date')
    return result


def _coefficient(home_goals, away_goals, home_rate, away_rate):
    if (home_goals, away_goals) == (0, 0):
        return -home_rate * away_rate
    if (home_goals, away_goals) == (0, 1):
        return home_rate
    if (home_goals, away_goals) == (1, 0):
        return away_rate
    if (home_goals, away_goals) == (1, 1):
        return -1.
    return 0.


def tau(home_goals, away_goals, home_rate, away_rate, rho):
    """Return the prescribed low-score factor; never clip an invalid factor."""
    _rates(home_rate, away_rate); _rho(rho); _score(home_goals, away_goals)
    result = 1 + _coefficient(home_goals, away_goals, home_rate, away_rate) * rho
    if result <= 0:
        raise ValueError('Dixon--Coles correction would make a score probability nonpositive')
    return result


def score_matrix(home_rate, away_rate, rho=0., limit=20):
    """Return a normalized matrix indexed by home then away regulation goals.

    The exact saved rho is used at every fixture.  An inadmissible value raises
    an error instead of silently projecting it to fixture-specific bounds.
    """
    _rates(home_rate, away_rate); _rho(rho)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('The finite score grid must contain 1 through 20 goals per side')
    factors = {(h, a): tau(h, a, home_rate, away_rate, rho) for h in (0, 1) for a in (0, 1)}
    home, away = [math.exp(-home_rate)], [math.exp(-away_rate)]
    for k in range(1, limit + 1):
        home.append(home[-1] * home_rate / k)
        away.append(away[-1] * away_rate / k)
    matrix = [[hp * ap * factors.get((h, a), 1.) for a, ap in enumerate(away)]
              for h, hp in enumerate(home)]
    mass = math.fsum(value for row in matrix for value in row)
    if not math.isfinite(mass) or mass <= 0:
        raise ValueError('Invalid finite score probability mass')
    return [[value / mass for value in row] for row in matrix]


def probabilities(home_rate, away_rate, rho=0., limit=20):
    """Return normalized P1 / draw / P2 using the corrected finite score grid."""
    matrix = score_matrix(home_rate, away_rate, rho, limit)
    home = math.fsum(value for h, row in enumerate(matrix) for a, value in enumerate(row) if h > a)
    draw = math.fsum(matrix[i][i] for i in range(limit + 1))
    away = math.fsum(value for h, row in enumerate(matrix) for a, value in enumerate(row) if h < a)
    mass = math.fsum((home, draw, away))
    return [home / mass, draw / mass, away / mass]


def fit_rho(rows, cutoff, *, half_life=365., window_days=1095, bounds=(-.15, .15), minimum_matches=100):
    """Fit one coefficient to past score/rate pairs by weighted likelihood.

    Each eligible row needs ``date``, ``rate_cutoff``, ``home_goals``,
    ``away_goals``, ``home_rate`` and ``away_rate``. ``rate_cutoff`` must precede
    its match date. Rows at/after the outer cutoff or outside its lookback are
    excluded before inspecting scores or rates. Weights decay by the supplied
    half-life. Bounds are intersected with positivity constraints for *all four*
    cells of every training fixture, not just the cell that happened to occur.

    The objective is concave; a monotone derivative and fixed 80-step bisection
    find its sole interior maximum, or the appropriate fixed boundary. There is
    no grid search or extra penalty parameter. If no low scores are observed,
    rho is unidentified and zero is returned when it is within the bounds.
    """
    cutoff = _date(cutoff)
    if (not _number(half_life) or half_life <= 0 or type(window_days) is not int or window_days <= 0
            or type(minimum_matches) is not int or minimum_matches <= 0):
        raise ValueError('Invalid time window, decay or minimum sample size')
    if (not isinstance(bounds, (tuple, list)) or len(bounds) != 2
            or any(not _number(value) for value in bounds) or not bounds[0] < bounds[1]):
        raise ValueError('Expected two ordered finite rho bounds')
    requested = list(bounds); lower, upper = bounds
    prior, terms = [], {}
    for row in rows:
        day = _date(row['date']); age = (cutoff-day).days
        if not 0 < age <= window_days:
            continue
        rate_cutoff = _date(row['rate_cutoff'])
        if rate_cutoff > day-timedelta(days=1):
            raise ValueError('Rate estimation must precede the observed match date')
        h, a = row['home_rate'], row['away_rate']
        yh, ya = row['home_goals'], row['away_goals']
        _rates(h, a); _score(yh, ya)
        lower = max(lower, (POSITIVITY_MARGIN-1)/h, (POSITIVITY_MARGIN-1)/a)
        upper = min(upper, (1-POSITIVITY_MARGIN)/(h*a), 1-POSITIVITY_MARGIN)
        weight = math.exp(-math.log(2)*age/half_life)
        coefficient = _coefficient(yh, ya, h, a)
        if coefficient and weight > 0:
            terms[coefficient] = terms.get(coefficient, 0.) + weight
        prior.append((day, rate_cutoff, weight, coefficient))
    if len(prior) < minimum_matches:
        raise ValueError('Insufficient past score/rate pairs to fit Dixon--Coles rho')
    if not lower < upper:
        raise ValueError('No common positive Dixon--Coles coefficient exists within the requested bounds')
    total_weight = math.fsum(row[2] for row in prior)
    if total_weight <= 0:
        raise ValueError('Past observations have no finite positive weight')

    def derivative(value):
        return math.fsum((weight/total_weight)*coefficient/(1+value*coefficient)
                         for coefficient, weight in terms.items())

    iterations = 0
    if not terms:
        rho = max(lower, min(upper, 0.))
    elif derivative(lower) <= 0:
        rho = lower
    elif derivative(upper) >= 0:
        rho = upper
    else:
        left, right = lower, upper
        for iterations in range(1, 81):
            middle = (left+right)/2
            if derivative(middle) > 0:
                left = middle
            else:
                right = middle
        rho = (left+right)/2
    gain = math.fsum(weight*math.log1p(rho*coefficient) for coefficient, weight in terms.items())/total_weight
    largest_weight = max(row[2] for row in prior)
    return {'rho': rho, 'fit': {
        'training_games': len(prior), 'informative_games': sum(bool(row[3]) for row in prior),
        'latest_training_date': max(row[0] for row in prior).isoformat(),
        'latest_rate_cutoff': max(row[1] for row in prior).isoformat(),
        'cutoff_exclusive': cutoff.isoformat(), 'half_life': half_life, 'window_days': window_days,
        'minimum_matches': minimum_matches, 'requested_bounds': requested,
        'feasible_bounds': [lower, upper], 'effective_weight': total_weight,
        'effective_sample_size': (total_weight/largest_weight)**2/math.fsum((row[2]/largest_weight)**2 for row in prior),
        'informative_weight': math.fsum(row[2] for row in prior if row[3]),
        'identified': bool(terms), 'iterations': iterations, 'converged': True,
        'at_boundary': rho in (lower, upper),
        'log_likelihood_gain_over_independence': gain,
        'objective': 'past-weighted score log likelihood; fixed independent Poisson components cancel',
        'rate_provenance': 'caller-supplied prior forecasts; declared rate_cutoff precedes each score date'}}
