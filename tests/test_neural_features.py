import copy
from dataclasses import replace
from datetime import date, timedelta
import math
import unittest

from research.data import Match
from research.division_model import DivisionGame
from research.evaluate import base_distribution
from research.models import v1_statistics
from research.neural_features import build_rows, FEATURE_NAMES, DIVISION_ORDER
from research.performance import Performance


class NeuralFeatureTests(unittest.TestCase):
    @staticmethod
    def game(day, home='A', away='B', *, division='E0', hg=1, ag=0,
             hs=12., aws=9., hst=4., ast=3.):
        return DivisionGame(division, Performance(Match(day, day.year if day.month >= 7 else day.year-1,
            home, away, hg, ag), hs, aws, hst, ast))

    def sample(self, *, division='E0'):
        teams=list('ABCD'); start=date(2023,8,1); games=[]
        for i in range(24):
            home=teams[i%4]; away=teams[(i%4+1+i//4)%4]
            if home==away:away=teams[(i%4+1)%4]
            games.append(self.game(start+timedelta(days=i*3),home,away,division=division,
                                   hg=i%3,ag=(i+1)%2))
        return games

    @staticmethod
    def find(rows, game):
        m=game.observation.match
        return next(r for r in rows if (r['division'],r['date'],r['home'],r['away'])==
                    (game.division,m.day.isoformat(),m.home,m.away))

    def test_target_previous_day_and_future_values_cannot_enter_its_features_or_base(self):
        games=self.sample(); target=games[-1]; day=target.observation.match.day
        before=self.find(build_rows(games),target)
        poisoned=replace(target,observation=replace(target.observation,
            match=replace(target.observation.match,home_goals=0,away_goals=20),
            home_shots=99.,home_sot=98.,away_shots=100.,away_sot=99.))
        changed=games[:-1]+[poisoned,self.game(day-timedelta(days=1),'C','D',hg=25,ag=0),
                           self.game(day+timedelta(days=1),'A','B',hg=0,ag=30)]
        after=self.find(build_rows(changed),target)
        for key in ('features','base','v1','frequency','v1_available','history_counts',
                    'recent_history_counts','latest_input_match','unknown_teams'):
            self.assertEqual(before[key],after[key],key)
        self.assertNotEqual(before['actual'],after['actual'])
        self.assertLess(after['latest_input_match'],after['cutoff_date'])

    def test_division_histories_and_elo_do_not_mix(self):
        games=self.sample(); target=games[-1]
        before=self.find(build_rows(games),target)
        elsewhere=[replace(g,division='E2') for g in games]
        # These are separate synthetic league histories; the pinned corpus loader
        # independently verifies real clubs' one-division-per-season membership.
        after=self.find(build_rows(games+elsewhere),target)
        self.assertEqual(before,after)
        rows=build_rows(elsewhere)
        features=dict(zip(FEATURE_NAMES,rows[-1]['features']))
        self.assertEqual([features['division_'+d] for d in DIVISION_ORDER],[0.,0.,1.])

    def test_missing_shots_and_observed_zero_are_distinguished_without_invented_values(self):
        start=date(2023,8,1)
        zero=self.game(start,hs=0.,aws=0.,hst=0.,ast=0.,hg=0,ag=0)
        missing=replace(zero,observation=replace(zero.observation,
            home_shots=None,away_shots=None,home_sot=None,away_sot=None))
        target=self.game(start+timedelta(days=4))
        observed=self.find(build_rows([zero,target]),target)
        absent=self.find(build_rows([missing,target]),target)
        a=dict(zip(FEATURE_NAMES,observed['features']));b=dict(zip(FEATURE_NAMES,absent['features']))
        for window in (5,15):
            for side in ('home','away'):
                for stat in ('shots_for','shots_against','sot_for','sot_against'):
                    key=f'{side}_{stat}_coverage_{window}'
                    self.assertEqual(a[key],1.);self.assertEqual(b[key],0.)
        self.assertEqual(a['home_sot_for_5'],3*4.5/4)
        self.assertEqual(b['home_sot_for_5'],4.5)
        self.assertEqual(observed['actual'],absent['actual'])
        partial=replace(zero,observation=replace(zero.observation,away_shots=None,away_sot=None))
        row=self.find(build_rows([partial,target]),target)
        coverage=dict(zip(FEATURE_NAMES,row['features']))
        self.assertEqual(coverage['home_sot_for_5'],3*4.5/4)
        self.assertEqual(coverage['home_sot_for_coverage_5'],1.)
        self.assertEqual(coverage['home_sot_against_coverage_5'],0.)
        self.assertEqual(coverage['away_sot_for_coverage_5'],0.)
        self.assertEqual(coverage['away_sot_against_coverage_5'],1.)

    def test_full_coverage_unknown_teams_and_v1_fallback_are_explicit(self):
        games=self.sample(); before=copy.deepcopy(games);rows=build_rows(games)
        self.assertEqual(len(rows),len(games));self.assertEqual(games,before)
        self.assertEqual(len(FEATURE_NAMES),56);self.assertEqual(len(set(FEATURE_NAMES)),56)
        first=rows[0];features=dict(zip(FEATURE_NAMES,first['features']))
        self.assertIsNone(first['v1']);self.assertFalse(first['v1_available'])
        self.assertEqual(first['base'],[1/3]*3);self.assertEqual(first['frequency'],first['base'])
        self.assertEqual(first['unknown_teams'],['A','B'])
        self.assertEqual(first['history_counts'],{'home':0,'away':0})
        self.assertIsNone(first['latest_input_match'])
        self.assertEqual(features['v1_available'],0.)
        for r in rows:
            self.assertEqual(len(r['features']),56)
            self.assertTrue(all(math.isfinite(v) for v in r['features']))
            self.assertAlmostEqual(sum(r['base']),1.)
        known=next(r for r in reversed(rows) if r['v1_available'])
        self.assertEqual(known['base'],known['v1'])
        self.assertEqual(dict(zip(FEATURE_NAMES,known['features']))['v1_available'],1.)

    def test_base_probabilities_match_fixed_historical_implementations(self):
        games=self.sample();rows=build_rows(games);matches=[g.observation.match for g in games]
        for r in rows:
            cutoff=date.fromisoformat(r['cutoff_date'])
            old=v1_statistics(matches,cutoff,r['season'],r['home'],r['away'])
            frequencies=base_distribution(matches,cutoff,r['season'])
            self.assertEqual(r['frequency'],frequencies)
            self.assertEqual(r['v1'],old['probabilities'].tolist() if old else None)
            self.assertEqual(r['base'],r['v1'] if old else frequencies)
            for i,name in enumerate(('log_base_home','log_base_draw','log_base_away')):
                self.assertEqual(r['features'][FEATURE_NAMES.index(name)],math.log(r['base'][i]))

    def test_calendar_rest_is_bounded_and_inputs_are_sorted_before_state_building(self):
        earlier=self.game(date(2023,8,1));later=self.game(date(2024,1,1),'A','C')
        rows=build_rows([later,earlier]);r=self.find(rows,later);features=dict(zip(FEATURE_NAMES,r['features']))
        self.assertEqual(features['home_league_interval_days'],60.)
        self.assertEqual(features['away_league_interval_days'],60.)
        self.assertEqual(r['history_counts'],{'home':1,'away':0})
        self.assertEqual(r['unknown_teams'],['C'])
        self.assertEqual(rows,build_rows([earlier,later]))

    def test_duplicate_fixtures_double_play_and_invalid_divisions_are_rejected(self):
        game=self.game(date(2023,8,1))
        with self.assertRaises(ValueError):build_rows([game,game])
        with self.assertRaises(ValueError):
            build_rows([game,self.game(date(2023,8,1),'A','C')])
        with self.assertRaises(ValueError):build_rows([replace(game,division='E3')])
        invalid=replace(game,observation=replace(game.observation,home_sot=99.))
        with self.assertRaises(ValueError):build_rows([invalid])
        self.assertEqual(build_rows([]),[])


if __name__=='__main__':unittest.main()
