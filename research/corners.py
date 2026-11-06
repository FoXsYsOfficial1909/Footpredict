"""Past-only EPL corner counts and portable total-corner distributions."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
import hashlib
import io
import math
from pathlib import Path

import numpy as np
from scipy.optimize import minimize, minimize_scalar
from scipy.stats import nbinom, poisson

from research.data import ALIASES, Match, canonical, parse_csv, parse_day
from research.models import LearnedPoisson

LINES = (8.5, 9.5, 10.5)
SIDE_MEAN_BOUNDS = (.1, 20.)


@dataclass(frozen=True)
class CornerObservation:
    match: Match
    home_corners: int | None
    away_corners: int | None
    issues: tuple = ()

    @property
    def total(self):
        if self.home_corners is None or self.away_corners is None:
            return None
        return self.home_corners + self.away_corners


def corner_count(value):
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, bool):
        raise ValueError('Corner counts must be integers from zero to forty')
    try:
        number = float(value)
    except (ValueError, TypeError) as error:
        raise ValueError('Corner counts must be integers from zero to forty') from error
    if not math.isfinite(number) or number != int(number) or not 0 <= number <= 40:
        raise ValueError('Corner counts must be integers from zero to forty')
    return int(number)


def parse_corners(body, season):
    """Keep verified goal/result labels; invalid/missing corner counts stay None."""
    verified = {match.key: match for match in parse_csv(body, season)}
    result = []
    for row in csv.DictReader(io.StringIO(body.decode('utf-8-sig'))):
        if not (row.get('Date') or '').strip():
            continue
        key = (parse_day(row['Date']).isoformat(), canonical(row['HomeTeam']), canonical(row['AwayTeam']))
        issues = []
        def count(column):
            try:
                value = corner_count(row.get(column))
            except ValueError:
                issues.append(column + ':invalid')
                return None
            if value is None:
                issues.append(column + ':missing')
            return value
        home, away = count('HC'), count('AC')
        result.append(CornerObservation(verified[key], home, away, tuple(issues)))
    return sorted(result, key=lambda row: row.match.key)


def load_corners(directory, seasons):
    observations, manifest = [], []
    for season in seasons:
        path = Path(directory) / (str(season) + '-E0.csv')
        body = path.read_bytes(); rows = parse_corners(body, season)
        observations.extend(rows)
        manifest.append({'season': season, 'rows': len(rows),
            'cornerRows': sum(row.total is not None for row in rows),
            'missingCornerRows': sum(row.total is None for row in rows),
            'invalidCornerRows': sum(any(issue.endswith(':invalid') for issue in row.issues) for row in rows),
            'sha256': hashlib.sha256(body).hexdigest(),
            'url': f'https://www.football-data.co.uk/mmz4281/{season % 100:02d}{(season + 1) % 100:02d}/E0.csv',
            'used_columns': ['Date', 'HomeTeam', 'AwayTeam', 'FTHG', 'FTAG', 'FTR', 'HC', 'AC'],
            'not_used': 'Target-match statistics and future corner counts never enter their own prediction.'})
    observations.sort(key=lambda row: row.match.key)
    if len({row.match.key for row in observations}) != len(observations):
        raise ValueError('Duplicate corner fixture')
    return observations, manifest


def count_distribution(mean, family='poisson', alpha=0.):
    if not math.isfinite(mean) or mean <= 0:
        raise ValueError('Total-corner mean must be positive and finite')
    if family not in ('poisson', 'negative_binomial'):
        raise ValueError('Unknown corner distribution family')
    if not math.isfinite(alpha) or alpha < 0 or alpha > 1:
        raise ValueError('Corner dispersion must be between zero and one')
    if family == 'poisson' or alpha <= 1e-8:
        return poisson(mu=mean)
    size = 1. / alpha
    return nbinom(n=size, p=size / (size + mean))


def distribution_prediction(home_mean, away_mean, family, alpha):
    mean = home_mean + away_mean
    distribution = count_distribution(mean, family, alpha)
    totals = {}
    for line in LINES:
        under = float(distribution.cdf(math.floor(line)))
        # Complementary probabilities share the identical finite CDF value.
        totals[str(line)] = {'line': line, 'under': under, 'over': 1. - under}
    return {'expectedCorners': {'home': float(home_mean), 'away': float(away_mean), 'total': float(mean)},
        'totals': totals, 'range80': {'lower': int(distribution.ppf(.1)),
            'upper': int(distribution.ppf(.9)), 'coverage': .8},
        'distribution': {'family': family, 'alpha': float(alpha), 'mean': float(mean)}}


class CornerModel:
    """Shrunk team strengths for side means, with a separate total dispersion.

    Negative-binomial dispersion describes TOTAL corners only. It is estimated
    from earlier training totals and fitted total means, never target outcomes.
    No parameters, count caps or probabilities from the goal model are reused.
    """
    def __init__(self, half_life=365, ridge=.03, window_days=1095, family='poisson'):
        if any(not math.isfinite(value) or value <= 0 for value in (half_life, ridge, window_days)):
            raise ValueError('Corner model settings must be positive and finite')
        if family not in ('poisson', 'negative_binomial'):
            raise ValueError('Unknown corner distribution family')
        self.half_life, self.ridge, self.window_days = half_life, ridge, window_days
        self.family, self.alpha = family, 0.
        self.teams, self.theta, self.as_of, self.fit_info = [], None, None, None

    def fit(self, observations, day):
        prior = [row for row in observations if 0 < (day - row.match.day).days <= self.window_days]
        covered = []
        for row in prior:
            h, a = corner_count(row.home_corners), corner_count(row.away_corners)
            if h is not None and a is not None:
                covered.append((row, h, a))
        if len(covered) < 100:
            raise ValueError('At least 100 earlier paired corner observations are required')
        if len({row.match.key for row, _, _ in covered}) != len(covered):
            raise ValueError('Duplicate historical corner fixture')
        teams = sorted({name for row, _, _ in covered for name in (row.match.home, row.match.away)})
        index = {name: i for i, name in enumerate(teams)}; n = len(teams)
        hi = np.array([index[row.match.home] for row, _, _ in covered])
        ai = np.array([index[row.match.away] for row, _, _ in covered])
        yh = np.array([h for _, h, _ in covered], dtype=float)
        ya = np.array([a for _, _, a in covered], dtype=float)
        weights = np.exp(-math.log(2) * np.array([(day - row.match.day).days for row, _, _ in covered]) / self.half_life)
        theta = np.zeros(2 + 2 * n)
        mean_a = max(.1, float(np.average(ya, weights=weights)))
        mean_h = max(.1, float(np.average(yh, weights=weights)))
        theta[0], theta[1] = math.log(mean_a), math.log(mean_h / mean_a)
        if self.theta is not None:
            old = {name: i for i, name in enumerate(self.teams)}; old_n = len(old)
            theta[:2] = self.theta[:2]
            for name, i in index.items():
                if name in old:
                    j = old[name]
                    theta[2 + i] = self.theta[2 + j]
                    theta[2 + n + i] = self.theta[2 + old_n + j]
        result = minimize(LearnedPoisson.objective, theta,
            args=(hi, ai, yh, ya, weights, self.ridge, n), jac=True, method='L-BFGS-B',
            bounds=[(math.log(.1), math.log(20)), (-1., 1.)] + [(-2.5, 2.5)] * (2 * n),
            options={'maxiter': 220, 'ftol': 1e-10, 'gtol': 1e-6})
        if not result.success or not np.all(np.isfinite(result.x)):
            raise RuntimeError('Corner mean optimizer did not converge: ' + str(result.message))
        alpha = 0.
        if self.family == 'negative_binomial':
            t = result.x
            h = np.clip(np.exp(t[0] + t[1] + t[2 + hi] - t[2 + n + ai]), *SIDE_MEAN_BOUNDS)
            a = np.clip(np.exp(t[0] + t[2 + ai] - t[2 + n + hi]), *SIDE_MEAN_BOUNDS)
            total_mean, total_count = h + a, yh + ya
            def objective(log_alpha):
                dispersion = math.exp(log_alpha); size = 1. / dispersion
                return -float(np.average(nbinom.logpmf(total_count, size, size / (size + total_mean)), weights=weights))
            fitted = minimize_scalar(objective, bounds=(math.log(1e-4), math.log(1.)), method='bounded',
                                     options={'xatol': 1e-7})
            if not fitted.success or not math.isfinite(fitted.fun):
                raise RuntimeError('Corner dispersion optimizer did not converge')
            alpha = math.exp(float(fitted.x))
        self.teams, self.theta, self.as_of, self.alpha = teams, result.x, day, alpha
        self.fit_info = {'training_games': len(covered), 'paired_corners_games': len(covered),
            'missing_corner_games': len(prior) - len(covered),
            'latest_training_date': max(row.match.day for row, _, _ in covered).isoformat(),
            'iterations': int(result.nit), 'loss': float(result.fun), 'converged': True,
            'dispersion_estimation': 'past_training_total_counts' if self.family == 'negative_binomial' else 'not_applicable'}
        return self

    def predict(self, home, away, day=None):
        if self.theta is None:
            raise ValueError('Corner model has not been fitted')
        if day is not None and day < self.as_of:
            raise ValueError('A future-fitted corner model cannot predict an earlier game')
        home, away = canonical(home), canonical(away)
        if not home or not away or home == away:
            raise ValueError('Invalid corner fixture teams')
        index = {name: i for i, name in enumerate(self.teams)}; n = len(self.teams)
        def strength(name, offset):
            return self.theta[offset + index[name]] if name in index else 0.
        h = math.exp(self.theta[0] + self.theta[1] + strength(home, 2) - strength(away, 2 + n))
        a = math.exp(self.theta[0] + strength(away, 2) - strength(home, 2 + n))
        h, a = [max(SIDE_MEAN_BOUNDS[0], min(SIDE_MEAN_BOUNDS[1], value)) for value in (h, a)]
        result = distribution_prediction(h, a, self.family, self.alpha)
        result['unseenTeams'] = [name for name in (home, away) if name not in index]
        return result

    def export(self):
        if self.theta is None:
            raise ValueError('Corner model has not been fitted')
        n = len(self.teams)
        return {'schemaVersion': 1, 'model_version': 'corners-glm-0.1', 'status': 'experimental',
            'target': 'corners_total_regulation', 'as_of': self.as_of.isoformat(),
            'family': self.family, 'alpha': self.alpha, 'lines': list(LINES),
            'parameters': {'half_life': self.half_life, 'ridge': self.ridge, 'window_days': self.window_days},
            'intercept': float(self.theta[0]), 'home_advantage': float(self.theta[1]),
            'teams': {name: {'attack': float(self.theta[2 + i]), 'defence': float(self.theta[2 + n + i])}
                      for i, name in enumerate(self.teams)},
            'side_mean_bounds': list(SIDE_MEAN_BOUNDS), 'aliases': ALIASES,
            'fit': dict(self.fit_info), 'promoted_to_default': False}


def empiricalLeague(observations, cutoff, half_life=365, window_days=1095):
    """Weighted empirical event frequencies, with fixed Beta(.5,.5) smoothing."""
    if any(not math.isfinite(value) or value <= 0 for value in (half_life, window_days)):
        raise ValueError('Corner benchmark settings must be positive')
    covered = [(row, corner_count(row.home_corners), corner_count(row.away_corners))
               for row in observations if 0 < (cutoff - row.match.day).days <= window_days]
    covered = [(row, h, a) for row, h, a in covered if h is not None and a is not None]
    if len(covered) < 100:
        raise ValueError('At least 100 earlier paired corner observations are required')
    weights = np.exp(-math.log(2) * np.array([(cutoff - row.match.day).days for row, _, _ in covered]) / half_life)
    totals = np.array([h + a for _, h, a in covered]); mass = weights.sum()
    hm = float(np.average([h for _, h, _ in covered], weights=weights))
    am = float(np.average([a for _, _, a in covered], weights=weights))
    events = {}
    for line in LINES:
        over = float((weights[totals > line].sum() + .5) / (mass + 1.))
        events[str(line)] = {'line': line, 'over': over, 'under': 1. - over}
    order = np.argsort(totals, kind='stable'); cumulative = np.cumsum(weights[order]) / mass
    quantile = lambda q: int(totals[order[min(int(np.searchsorted(cumulative, q)), len(totals) - 1)]])
    return {'expectedCorners': {'home': hm, 'away': am, 'total': hm + am},
        'as_of': cutoff.isoformat(), 'training_cutoff_exclusive': cutoff.isoformat(),
        'totals': events, 'range80': {'lower': quantile(.1), 'upper': quantile(.9), 'coverage': .8},
        'distribution': {'family': 'empirical', 'alpha': None, 'mean': hm + am},
        'unseenTeams': [], 'fit': {'training_games': len(covered),
            'latest_training_date': max(row.match.day for row, _, _ in covered).isoformat()}}


empirical_league = empiricalLeague
