"""Past-only Poisson strength estimates with historical expected-goal targets."""
from __future__ import annotations

from dataclasses import replace
import math

from research.models import LearnedPoisson


class ExpectedGoalsPoisson(LearnedPoisson):
    """Blend verified goals with scaled historical xG, preserving actual results.

    Poisson quasi-likelihood permits non-negative continuous training targets.
    The scale and every blended observation use only matches before the cutoff.
    A match missing either side's xG retains both original goal targets.
    """

    def __init__(self, goal_weight=.5, **settings):
        super().__init__(**settings)
        try:
            goal_weight = float(goal_weight)
        except (TypeError, ValueError) as error:
            raise ValueError('Goal weight must be between zero and one') from error
        if not math.isfinite(goal_weight) or not 0 <= goal_weight <= 1:
            raise ValueError('Goal weight must be between zero and one')
        self.goal_weight = goal_weight
        self.xg_scale = None
        self.xg_info = None

    def fit(self, observations, day):
        prior = [row for row in observations
                 if 0 < (day - row.match.day).days <= self.window_days]
        if len(prior) < 100:
            raise ValueError('At least 100 earlier games are needed')

        covered = []
        for row in prior:
            for value in (row.home_xg, row.away_xg):
                if value is not None:
                    try:
                        valid = math.isfinite(value) and 0 <= value <= 15
                    except TypeError:
                        valid = False
                    if not valid:
                        raise ValueError('Historical xG must be finite and between zero and 15')
            if row.home_xg is not None and row.away_xg is not None:
                covered.append(row)
        if len(covered) < 100:
            raise ValueError('At least 100 earlier games with paired xG are needed')

        def weight(row):
            return math.exp(-math.log(2) * (day - row.match.day).days / self.half_life)

        weights = [weight(row) for row in covered]
        goals = sum(w * (row.match.home_goals + row.match.away_goals)
                    for w, row in zip(weights, covered))
        xg = sum(w * (row.home_xg + row.away_xg)
                 for w, row in zip(weights, covered))
        if xg <= 0 or not math.isfinite(xg):
            raise ValueError('Earlier paired xG must have a positive finite total')
        scale = goals / xg
        if not math.isfinite(scale) or scale < 0:
            raise ValueError('Invalid historical goals-to-xG scale')

        modified = []
        for row in prior:
            hg, ag = row.match.home_goals, row.match.away_goals
            if row.home_xg is not None and row.away_xg is not None:
                hg = self.goal_weight * hg + (1 - self.goal_weight) * scale * row.home_xg
                ag = self.goal_weight * ag + (1 - self.goal_weight) * scale * row.away_xg
            modified.append(replace(row.match, home_goals=hg, away_goals=ag))

        # Update diagnostics only after a successful fit, as for the base model.
        super().fit(modified, day)
        self.xg_scale = scale
        self.xg_info = {
            'goal_weight': self.goal_weight,
            'goals_per_xg': scale,
            'training_games': len(prior),
            'xg_games': len(covered),
            'missing_xg_games': len(prior) - len(covered),
            'weighted_coverage': sum(weights) / sum(weight(row) for row in prior),
            'earliest_xg_date': min(row.match.day for row in covered).isoformat(),
            'latest_xg_date': max(row.match.day for row in covered).isoformat(),
            'cutoff_exclusive': day.isoformat(),
        }
        self.fit_info.update({
            'xg_games': len(covered),
            'xg_weighted_coverage': self.xg_info['weighted_coverage'],
            'latest_xg_date': self.xg_info['latest_xg_date'],
        })
        return self

    def export(self):
        result = super().export()
        result['model_version'] = 'expected-goals-poisson-0.1'
        result['xg_target'] = dict(self.xg_info)
        return result
