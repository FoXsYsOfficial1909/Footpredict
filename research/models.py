"""Past-only baseline, exact Poisson v1 and learned attack/defence Poisson v2."""
from __future__ import annotations

from datetime import date
import math
import numpy as np
from scipy.optimize import minimize


def score_probabilities(home_goals, away_goals, limit=20):
    if not all(math.isfinite(value) and 0 < value <= 10 for value in (home_goals, away_goals)):
        raise ValueError('Invalid goal expectation')
    home, away = [math.exp(-home_goals)], [math.exp(-away_goals)]
    for k in range(1, limit + 1):
        home.append(home[-1] * home_goals / k)
        away.append(away[-1] * away_goals / k)
    matrix = np.outer(home, away)
    result = np.array([np.tril(matrix, -1).sum(), np.trace(matrix), np.triu(matrix, 1).sum()])
    return result / result.sum()


def v1_statistics(matches, day, season, home, away):
    prior = [m for m in matches if m.day < day and m.season in (season, season - 1)]
    home_games = [m for m in prior if m.home == home]
    away_games = [m for m in prior if m.away == away]
    if len(prior) < 15 or len(home_games) < 2 or len(away_games) < 2:
        return None
    weights = {m.key: math.exp(-math.log(2) * (day - m.day).days / 120) * (1 if m.season == season else .35) for m in prior}

    def totals(games, side):
        return sum(weights[m.key] for m in games), sum(weights[m.key] * getattr(m, side + '_goals') for m in games)

    mass, goals_h = totals(prior, 'home')
    _, goals_a = totals(prior, 'away')
    lh, la = goals_h / mass, goals_a / mass
    if lh < .25 or la < .25:
        return None

    def smooth(games, side, base):
        weight, goals = totals(games, side)
        return (goals + 5 * base) / (weight + 5)

    counts = np.ones(3)
    for m in prior:
        counts[m.outcome] += weights[m.key]
    h = max(.05, min(5, smooth(home_games, 'home', lh) * smooth(away_games, 'home', lh) / lh))
    a = max(.05, min(5, smooth(away_games, 'away', la) * smooth(home_games, 'away', la) / la))
    return {'probabilities': score_probabilities(h, a), 'base': counts / counts.sum(),
            'home_goals': h, 'away_goals': a, 'prior_games': len(prior)}


class LearnedPoisson:
    """Weighted Poisson likelihood with ridge shrinkage of team strength parameters."""
    def __init__(self, half_life=365, ridge=.1, window_days=1095):
        if half_life <= 0 or ridge <= 0 or window_days <= 0:
            raise ValueError('Model settings must be positive')
        self.half_life, self.ridge, self.window_days = half_life, ridge, window_days
        self.teams, self.theta = [], None
        self.as_of = None
        self.fit_info = None

    @staticmethod
    def objective(theta, hi, ai, yh, ya, weights, ridge, n):
        attack, defence = theta[2:2+n], theta[2+n:]
        eta_h = theta[0] + theta[1] + attack[hi] - defence[ai]
        eta_a = theta[0] + attack[ai] - defence[hi]
        lh, la = np.exp(eta_h), np.exp(eta_a)
        normalizer = 2 * weights.sum()
        residual_h, residual_a = weights * (lh - yh) / normalizer, weights * (la - ya) / normalizer
        loss = np.sum(weights * (lh - yh * eta_h + la - ya * eta_a)) / normalizer
        loss += ridge * np.sum(theta[2:] ** 2) / (2 * n)
        gradient = np.zeros_like(theta)
        gradient[0] = (residual_h + residual_a).sum()
        gradient[1] = residual_h.sum()
        np.add.at(gradient[2:2+n], hi, residual_h)
        np.add.at(gradient[2:2+n], ai, residual_a)
        np.add.at(gradient[2+n:], ai, -residual_h)
        np.add.at(gradient[2+n:], hi, -residual_a)
        gradient[2:] += ridge * theta[2:] / n
        return float(loss), gradient

    def fit(self, matches, day):
        prior = [m for m in matches if 0 < (day - m.day).days <= self.window_days]
        if len(prior) < 100:
            raise ValueError('At least 100 earlier games are needed')
        teams = sorted({name for m in prior for name in (m.home, m.away)})
        index = {name: i for i, name in enumerate(teams)}
        hi, ai = np.array([index[m.home] for m in prior]), np.array([index[m.away] for m in prior])
        yh, ya = np.array([m.home_goals for m in prior]), np.array([m.away_goals for m in prior])
        weights = np.exp(-math.log(2) * np.array([(day - m.day).days for m in prior]) / self.half_life)
        n = len(teams)
        theta = np.zeros(2 + 2 * n)
        mean_a = max(.25, np.average(ya, weights=weights))
        mean_h = max(.25, np.average(yh, weights=weights))
        theta[0], theta[1] = math.log(mean_a), math.log(mean_h / mean_a)
        if self.theta is not None:
            old_index = {name: i for i, name in enumerate(self.teams)}
            old_n = len(self.teams)
            theta[:2] = self.theta[:2]
            for name, i in index.items():
                if name in old_index:
                    j = old_index[name]
                    theta[2+i], theta[2+n+i] = self.theta[2+j], self.theta[2+old_n+j]
        result = minimize(self.objective, theta, args=(hi, ai, yh, ya, weights, self.ridge, n),
                          jac=True, method='L-BFGS-B',
                          bounds=[(-1.5, 1.5), (-1., 1.)] + [(-2., 2.)] * (2*n),
                          options={'maxiter': 180, 'ftol': 1e-10, 'gtol': 1e-6})
        if not result.success or not np.all(np.isfinite(result.x)):
            raise RuntimeError('Poisson optimizer did not converge: ' + str(result.message))
        self.teams, self.theta, self.as_of = teams, result.x, day
        self.fit_info = {'training_games': len(prior), 'latest_training_date': max(m.day for m in prior).isoformat(),
                         'iterations': int(result.nit), 'loss': float(result.fun), 'converged': bool(result.success)}
        return self

    def predict(self, home, away, day=None):
        if self.theta is None:
            raise ValueError('Model has not been fitted')
        if day is not None and day < self.as_of:
            raise ValueError('A future-fitted model cannot predict an earlier game')
        index = {name: i for i, name in enumerate(self.teams)}
        n = len(self.teams)

        def strength(team, offset):
            return self.theta[offset+index[team]] if team in index else 0.

        h = math.exp(self.theta[0] + self.theta[1] + strength(home, 2) - strength(away, 2+n))
        a = math.exp(self.theta[0] + strength(away, 2) - strength(home, 2+n))
        h, a = max(.05, min(5, h)), max(.05, min(5, a))
        return {'probabilities': score_probabilities(h, a), 'home_goals': h, 'away_goals': a,
                'unseen_teams': [team for team in (home, away) if team not in index]}

    def export(self):
        if self.theta is None:
            raise ValueError('Model has not been fitted')
        n = len(self.teams)
        return {'model_version': 'poisson-v2-experimental-0.1', 'status': 'experimental',
                'as_of': self.as_of.isoformat(), 'parameters': {'half_life': self.half_life, 'ridge': self.ridge, 'window_days': self.window_days},
                'intercept': float(self.theta[0]), 'home_advantage': float(self.theta[1]),
                'teams': {team: {'attack': float(self.theta[2+i]), 'defence': float(self.theta[2+n+i])} for i, team in enumerate(self.teams)},
                'fit': self.fit_info, 'goal_cap': 5, 'poisson_max': 20}


def loss(probabilities, actual):
    p = np.asarray(probabilities, dtype=float)
    if p.shape != (3,) or np.any(~np.isfinite(p)) or np.any(p < 0) or abs(p.sum() - 1) > 1e-8:
        raise ValueError('Invalid probability distribution')
    return {'brier': float(np.sum((p - np.eye(3)[actual]) ** 2)),
            'logloss': -math.log(max(1e-12, float(p[actual]))),
            'correct': int(np.argmax(p) == actual)}
