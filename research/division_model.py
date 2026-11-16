"""Shared club strength with separate scoring environments for each division."""
from dataclasses import dataclass
import math

import numpy as np
from scipy.optimize import minimize

from research.data import DIVISIONS
from research.models import score_probabilities
from research.performance import Performance


@dataclass(frozen=True)
class DivisionGame:
    division: str
    observation: Performance


class DivisionPoisson:
    """Weighted quasi-Poisson fit; only past clubs connect division histories."""
    def __init__(self, half_life=730, ridge=.03, window_days=1095, goal_weight=.5):
        if min(half_life, ridge, window_days) <= 0 or not 0 <= goal_weight <= 1:
            raise ValueError('Invalid division-model settings')
        self.half_life, self.ridge, self.window_days, self.goal_weight = half_life, ridge, window_days, goal_weight
        self.teams, self.divisions, self.theta = [], [], None
        self.as_of, self.fit_info, self.shot_scales = None, None, {}

    @staticmethod
    def objective(theta, li, hi, ai, yh, ya, weights, ridge, n, levels):
        attack, defence = theta[2*levels:2*levels+n], theta[2*levels+n:]
        eta_h = theta[li] + theta[levels+li] + attack[hi] - defence[ai]
        eta_a = theta[li] + attack[ai] - defence[hi]
        lh, la = np.exp(eta_h), np.exp(eta_a)
        normalizer = 2*weights.sum()
        rh, ra = weights*(lh-yh)/normalizer, weights*(la-ya)/normalizer
        value = np.sum(weights*(lh-yh*eta_h+la-ya*eta_a))/normalizer
        value += ridge*np.sum(theta[2*levels:]**2)/(2*n)
        gradient = np.zeros_like(theta)
        np.add.at(gradient, li, rh+ra)
        np.add.at(gradient, levels+li, rh)
        np.add.at(gradient[2*levels:2*levels+n], hi, rh)
        np.add.at(gradient[2*levels:2*levels+n], ai, ra)
        np.add.at(gradient[2*levels+n:], ai, -rh)
        np.add.at(gradient[2*levels+n:], hi, -ra)
        gradient[2*levels:] += ridge*theta[2*levels:]/n
        return float(value), gradient

    def fit(self, games, cutoff):
        prior = [g for g in games if 0 < (cutoff-g.observation.match.day).days <= self.window_days]
        if len(prior) < 100 or any(g.division not in DIVISIONS for g in prior):
            raise ValueError('At least 100 verified E0/E1/E2 past games are required')
        divisions = sorted({g.division for g in prior})
        teams = sorted({t for g in prior for t in (g.observation.match.home, g.observation.match.away)})
        tid, did = {t: i for i, t in enumerate(teams)}, {d: i for i, d in enumerate(divisions)}
        li = np.array([did[g.division] for g in prior])
        hi = np.array([tid[g.observation.match.home] for g in prior])
        ai = np.array([tid[g.observation.match.away] for g in prior])
        weights = np.exp(-math.log(2)*np.array([(cutoff-g.observation.match.day).days for g in prior])/self.half_life)
        scales, stats, histories = {}, {}, {}
        for division in divisions:
            indices = [i for i, g in enumerate(prior) if g.division == division]
            covered = [i for i in indices if prior[i].observation.home_sot is not None and prior[i].observation.away_sot is not None]
            if len(indices) < 100 or (self.goal_weight < 1 and len(covered) < 100):
                raise ValueError('Each division requires 100 prior matches with sufficient shot coverage')
            if self.goal_weight < 1:
                shots = sum(weights[i]*(prior[i].observation.home_sot+prior[i].observation.away_sot) for i in covered)
                if shots <= 0:raise ValueError('No usable prior shots in division')
                goals = sum(weights[i]*(prior[i].observation.match.home_goals+prior[i].observation.match.away_goals) for i in covered)
                scales[division] = float(goals/shots)
            else:scales[division] = None
            stats[division] = {'trainingGames': len(indices), 'shotGames': len(covered),
                'goalFallbackGames': len(indices)-len(covered), 'goalsPerShotOnTarget': scales[division]}
        yh, ya = [], []
        for g in prior:
            o = g.observation; m = o.match
            for team in (m.home, m.away):histories.setdefault(team, set()).add(g.division)
            if self.goal_weight < 1 and o.home_sot is not None and o.away_sot is not None:
                yh.append(self.goal_weight*m.home_goals+(1-self.goal_weight)*scales[g.division]*o.home_sot)
                ya.append(self.goal_weight*m.away_goals+(1-self.goal_weight)*scales[g.division]*o.away_sot)
            else:yh.append(m.home_goals); ya.append(m.away_goals)
        yh, ya = np.array(yh), np.array(ya)
        n, levels = len(teams), len(divisions); theta = np.zeros(2*levels+2*n)
        for division, i in did.items():
            active = li == i
            away = max(.25, np.average(ya[active], weights=weights[active]))
            home = max(.25, np.average(yh[active], weights=weights[active]))
            theta[i], theta[levels+i] = math.log(away), math.log(home/away)
        if self.theta is not None:
            old_n, old_l = len(self.teams), len(self.divisions)
            for division, i in did.items():
                if division in self.divisions:
                    j = self.divisions.index(division); theta[i] = self.theta[j]; theta[levels+i] = self.theta[old_l+j]
            for team, i in tid.items():
                if team in self.teams:
                    j = self.teams.index(team)
                    theta[2*levels+i] = self.theta[2*old_l+j]
                    theta[2*levels+n+i] = self.theta[2*old_l+old_n+j]
        result = minimize(self.objective, theta, args=(li, hi, ai, yh, ya, weights, self.ridge, n, levels),
            jac=True, method='L-BFGS-B', bounds=[(-1.5, 1.5)]*levels+[(-1., 1.)]*levels+[(-2., 2.)]*(2*n),
            options={'maxiter': 180, 'ftol': 1e-10, 'gtol': 1e-6})
        if not result.success or not np.all(np.isfinite(result.x)):
            raise RuntimeError('Joint division optimizer did not converge: '+str(result.message))
        self.teams, self.divisions, self.theta, self.as_of = teams, divisions, result.x, cutoff
        self.shot_scales = scales
        self.fit_info = {'trainingGames': len(prior), 'latestTrainingDate': max(g.observation.match.day for g in prior).isoformat(),
            'byDivision': stats, 'bridgeTeams': sorted(t for t, seen in histories.items() if len(seen) > 1),
            'iterations': int(result.nit), 'loss': float(result.fun), 'converged': bool(result.success)}
        return self

    def predict(self, division, home, away, day=None):
        if self.theta is None or division not in self.divisions or any(t not in self.teams for t in (home, away)):
            raise ValueError('Unfitted model, unknown division or unknown club')
        if day is not None and day < self.as_of:raise ValueError('Cannot predict before the training cutoff')
        li, hi, ai = self.divisions.index(division), self.teams.index(home), self.teams.index(away)
        n, levels = len(self.teams), len(self.divisions)
        h = math.exp(self.theta[li]+self.theta[levels+li]+self.theta[2*levels+hi]-self.theta[2*levels+n+ai])
        a = math.exp(self.theta[li]+self.theta[2*levels+ai]-self.theta[2*levels+n+hi])
        h, a = max(.05, min(5, h)), max(.05, min(5, a))
        return {'probabilities': score_probabilities(h, a), 'home_goals': h, 'away_goals': a}

    def export(self):
        if self.theta is None:raise ValueError('Model is not fitted')
        n, levels = len(self.teams), len(self.divisions)
        return {'model_version': 'division-poisson-experimental-1', 'status': 'experimental', 'as_of': self.as_of.isoformat(),
            'parameters': {'half_life': self.half_life, 'ridge': self.ridge, 'window_days': self.window_days, 'goal_weight': self.goal_weight},
            'divisions': {d: {'intercept': float(self.theta[i]), 'home_advantage': float(self.theta[levels+i]),
                             'goals_per_shot_on_target': self.shot_scales[d]} for i, d in enumerate(self.divisions)},
            'teams': {t: {'attack': float(self.theta[2*levels+i]), 'defence': float(self.theta[2*levels+n+i])}
                      for i, t in enumerate(self.teams)},
            'fit': self.fit_info, 'goal_cap': 5, 'poisson_max': 20, 'is_xg': False, 'promoted_to_default': False}
