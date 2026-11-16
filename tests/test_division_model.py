from dataclasses import replace
from datetime import date, timedelta
import json
import unittest
from unittest.mock import patch

import numpy as np
from scipy.optimize import minimize

from research.championship_shots import normalized as elc_normalized
from research.cross_division import PLAN, forecast, validate
from research.data import Match
from research.division_model import DivisionGame, DivisionPoisson
from research.models import LearnedPoisson
from research.nightly import sha
from research.performance import Performance


def sample():
    rng = np.random.default_rng(14); result = []
    for division, start, season, prefix, hmean, amean in (
        ('E0', date(2023, 1, 1), 2022, 'PL', 1.8, .9),
        ('E1', date(2023, 8, 1), 2023, 'ELC', 1.4, 1.1)):
        teams = ['Bridge']+[prefix+str(i) for i in range(1, 6)]
        for i in range(180):
            home, away = teams[i%6], teams[(i%6+1+(i//6)%5)%6]
            m = Match(start+timedelta(days=i), season, home, away, int(rng.poisson(hmean)), int(rng.poisson(amean)))
            result.append(DivisionGame(division, Performance(m, 14., 12., 4., 3.)))
    return result


def three_division_sample():
    # Separate club moves connect E0/E1 and E1/E2; a club belongs to one
    # division in each season. The synthetic scores do not encode a result
    # for the fixture whose probability is subsequently requested.
    result=[]
    for game in sample():
        match=game.observation.match
        match=replace(match,home='L1Bridge' if match.home=='ELC5' else match.home,
                      away='L1Bridge' if match.away=='ELC5' else match.away)
        result.append(replace(game,observation=replace(game.observation,match=match)))
    rng=np.random.default_rng(87);teams=['L1Bridge']+[f'L1{i}' for i in range(1,6)]
    for i in range(180):
        home,away=teams[i%6],teams[(i%6+1+(i//6)%5)%6]
        match=Match(date(2022,1,1)+timedelta(days=i),2021,home,away,
                    int(rng.poisson(1.35)),int(rng.poisson(1.3)))
        result.append(DivisionGame('E2',Performance(match,10.,11.,3.,4.)))
    return result


class DivisionModelTests(unittest.TestCase):
    def test_joint_objective_gradient_and_one_division_reduction(self):
        rng = np.random.default_rng(19); n = 4
        hi, ai = np.array([0,1,2,3]), np.array([1,2,3,0])
        yh, ya, weights = np.array([2.,0,1,3]), np.array([0.,1,1,2]), np.array([1.,.8,.5,.3])
        theta = rng.normal(0,.1,size=4+2*n)
        args = (np.array([0,0,1,1]), hi, ai, yh, ya, weights, .03, n, 2)
        _, gradient = DivisionPoisson.objective(theta,*args)
        numerical = []
        for index in range(len(theta)):
            a,b = theta.copy(),theta.copy();a[index]-=1e-6;b[index]+=1e-6
            numerical.append((DivisionPoisson.objective(b,*args)[0]-DivisionPoisson.objective(a,*args)[0])/2e-6)
        np.testing.assert_allclose(gradient,numerical,rtol=1e-5,atol=1e-7)
        theta = rng.normal(0,.1,size=2+2*n)
        original = LearnedPoisson.objective(theta,hi,ai,yh,ya,weights,.03,n)
        joint = DivisionPoisson.objective(theta,np.zeros(4,dtype=int),hi,ai,yh,ya,weights,.03,n,1)
        self.assertAlmostEqual(original[0],joint[0],places=14)
        np.testing.assert_allclose(original[1],joint[1],atol=1e-14,rtol=0)

    def test_future_results_shots_and_division_history_cannot_change_the_fit(self):
        games=sample();cutoff=date(2024,2,15)
        model=DivisionPoisson().fit(games,cutoff)
        future=DivisionGame('E0',Performance(Match(cutoff,2023,'New','Other',20,0),99,1,90,1))
        other=DivisionPoisson().fit(games+[future],cutoff)
        np.testing.assert_array_equal(model.theta,other.theta)
        self.assertEqual(model.shot_scales,other.shot_scales)
        self.assertEqual(model.fit_info,other.fit_info)
        self.assertLess(model.fit_info['latestTrainingDate'],cutoff.isoformat())
        self.assertEqual(model.fit_info['bridgeTeams'],['Bridge'])

    def test_league_scales_remain_separate_and_missing_statistics_keep_results(self):
        games=sample();cutoff=date(2024,2,15)
        model=DivisionPoisson().fit(games,cutoff)
        self.assertNotAlmostEqual(model.shot_scales['E0'],model.shot_scales['E1'],places=4)
        changed=list(games);changed[-1]=replace(games[-1],observation=replace(games[-1].observation,home_sot=None))
        other=DivisionPoisson().fit(changed,cutoff)
        self.assertEqual(other.fit_info['trainingGames'],360)
        self.assertEqual(other.fit_info['byDivision']['E1']['trainingGames'],180)
        self.assertEqual(other.fit_info['byDivision']['E1']['goalFallbackGames'],1)
        self.assertEqual(other.fit_info['byDivision']['E0']['goalFallbackGames'],0)
        self.assertEqual(set(other.export()['divisions']),{'E0','E1'})
        prediction=other.predict('E1','Bridge','ELC1',cutoff)
        self.assertAlmostEqual(float(prediction['probabilities'].sum()),1.)
        self.assertFalse(other.export()['promoted_to_default']);self.assertFalse(other.export()['is_xg'])

    def test_unknown_clubs_divisions_and_backward_predictions_are_rejected(self):
        cutoff=date(2024,2,15);model=DivisionPoisson().fit(sample(),cutoff)
        for args in [('E2','Bridge','ELC1',cutoff),('E1','Missing','ELC1',cutoff),
                     ('E1','Bridge','ELC1',cutoff-timedelta(days=1))]:
            with self.assertRaises(ValueError):model.predict(*args)

    def test_cross_division_history_diagnostics_use_only_information_before_forecast(self):
        games=sample();plan=json.loads(PLAN.read_text());cutoff=date(2024,2,19)
        row={'date':'2024-02-20','cutoff_date':cutoff.isoformat(),'home':'PL1','away':'ELC1','actual':0}
        before=forecast(games,[row],plan)
        future=[DivisionGame(div,Performance(Match(cutoff,2023,'PL1','New',20,0),99,1,90,1)) for div in ('E0','E1')]
        after=forecast(games+future,[row],plan)
        self.assertEqual(before,after)
        self.assertTrue(before[0][0]['lowELCHistoryWithPL'])
        self.assertEqual(before[0][0]['pastClubGames']['PL1']['E1'],0)
        self.assertGreaterEqual(before[0][0]['pastClubGames']['PL1']['E0'],20)

    def test_two_division_source_conflicts_and_inconsistent_shots_are_rejected(self):
        games=sample();plan=json.loads(PLAN.read_text())
        plan.update(asOf='2024-02-15',sourceMatches={'E0':180,'E1':180},
                    sourceELCDataSha256=sha(elc_normalized([g.observation for g in games if g.division=='E1'])))
        validate(games,plan)
        changed=list(games);g=changed[0]
        changed[0]=replace(g,observation=replace(g.observation,match=replace(g.observation.match,day=date(2023,8,1),season=2023)))
        with self.assertRaisesRegex(ValueError,'conflicting divisions'):validate(changed,plan)
        changed=list(games);changed[0]=replace(g,observation=replace(g.observation,home_sot=80))
        with self.assertRaisesRegex(ValueError,'shot statistics'):validate(changed,plan)

    def test_three_division_objective_gradient(self):
        rng=np.random.default_rng(92);n=5;levels=3;count=15
        theta=rng.normal(0,.1,size=2*levels+2*n)
        hi=np.arange(count)%n;ai=(hi+1)%n;li=np.arange(count)%levels
        args=(li,hi,ai,rng.poisson(1.5,count),rng.poisson(1.1,count),
              rng.uniform(.1,1,count),.03,n,levels)
        _,gradient=DivisionPoisson.objective(theta,*args)
        numerical=[]
        for i in range(len(theta)):
            low,high=theta.copy(),theta.copy();low[i]-=1e-6;high[i]+=1e-6
            numerical.append((DivisionPoisson.objective(high,*args)[0]-
                              DivisionPoisson.objective(low,*args)[0])/2e-6)
        np.testing.assert_allclose(gradient,numerical,rtol=1e-5,atol=1e-7)

    def test_three_divisions_keep_scales_and_home_advantage_separate(self):
        cutoff=date(2024,2,15);games=three_division_sample()
        model=DivisionPoisson().fit(games,cutoff);export=model.export()
        self.assertEqual(set(export['divisions']),{'E0','E1','E2'})
        self.assertEqual(model.fit_info['trainingGames'],540)
        self.assertEqual(model.fit_info['bridgeTeams'],['Bridge','L1Bridge'])
        self.assertEqual({d:s['trainingGames'] for d,s in model.fit_info['byDivision'].items()},
                         {'E0':180,'E1':180,'E2':180})
        self.assertEqual(len(set(model.shot_scales.values())),3)
        self.assertNotAlmostEqual(export['divisions']['E0']['home_advantage'],
                                  export['divisions']['E2']['home_advantage'],places=3)
        for division,opponent in [('E0','PL1'),('E1','ELC1'),('E2','L11')]:
            p=model.predict(division,'L1Bridge',opponent,cutoff)['probabilities']
            self.assertAlmostEqual(float(p.sum()),1.,places=14)
            self.assertTrue(np.all(p>0))
        self.assertFalse(export['promoted_to_default']);self.assertFalse(export['is_xg'])

    def test_new_league_future_scores_and_missing_shots_preserve_the_information_fence(self):
        cutoff=date(2024,2,15);games=three_division_sample()
        before=DivisionPoisson().fit(games,cutoff)
        future=[DivisionGame(division,Performance(Match(cutoff+timedelta(days=i),2023,
                    'L1Bridge','L11',30,0),99.,1.,98.,1.))
                for i,division in enumerate(('E0','E1','E2'))]
        after=DivisionPoisson().fit(games+future,cutoff)
        np.testing.assert_array_equal(before.theta,after.theta)
        self.assertEqual(before.fit_info,after.fit_info)
        self.assertEqual(before.shot_scales,after.shot_scales)
        missing=list(games);missing[-1]=replace(missing[-1],observation=
            replace(missing[-1].observation,away_sot=None))
        changed=DivisionPoisson().fit(missing,cutoff)
        self.assertEqual(changed.fit_info['trainingGames'],540)
        self.assertEqual(changed.fit_info['byDivision']['E2']['goalFallbackGames'],1)
        for division in ('E0','E1'):
            self.assertEqual(changed.shot_scales[division],before.shot_scales[division])
        with self.assertRaises(ValueError):
            DivisionPoisson().fit(games+[DivisionGame('E3',games[-1].observation)],cutoff)

    def test_two_to_three_division_warm_start_maps_every_old_parameter_by_identity(self):
        cutoff=date(2024,2,15);games=three_division_sample()
        model=DivisionPoisson().fit([g for g in games if g.division!='E2'],cutoff)
        previous=model.export();old_teams=set(model.teams)
        with patch('research.division_model.minimize',wraps=minimize) as solver:
            model.fit(games,cutoff)
        initial=solver.call_args.args[1];n=len(model.teams);levels=len(model.divisions)
        for division,values in previous['divisions'].items():
            li=model.divisions.index(division)
            self.assertEqual(initial[li],values['intercept'])
            self.assertEqual(initial[levels+li],values['home_advantage'])
        for team,i in ((t,model.teams.index(t)) for t in model.teams):
            if team in old_teams:
                self.assertEqual(initial[2*levels+i],previous['teams'][team]['attack'])
                self.assertEqual(initial[2*levels+n+i],previous['teams'][team]['defence'])
            else:
                self.assertEqual(initial[2*levels+i],0)
                self.assertEqual(initial[2*levels+n+i],0)
        cold=DivisionPoisson().fit(games,cutoff)
        self.assertTrue(model.fit_info['converged'])
        np.testing.assert_allclose(model.predict('E1','L1Bridge','ELC1',cutoff)['probabilities'],
                                   cold.predict('E1','L1Bridge','ELC1',cutoff)['probabilities'],atol=1e-4,rtol=0)


if __name__=='__main__':unittest.main()
