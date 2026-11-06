from dataclasses import replace
from datetime import date,timedelta
import unittest

import numpy as np

from research.data import Match
from research.models import LearnedPoisson,loss
from research.performance import Performance,parse_performance,ShotPoisson,feature_state,vector,FEATURES
from research.calibration import fit_calibration,calibrated,DixonColes


def sample():
    rng=np.random.default_rng(71);teams=['A','B','C','D','E','F'];rows=[]
    for i in range(220):
        home=teams[i%6];away=teams[(i%6+1+(i//6)%5)%6]
        m=Match(date(2024,1,1)+timedelta(days=i),2023,home,away,int(rng.poisson(1.8)),int(rng.poisson(1.1)))
        rows.append(Performance(m,12+i%5,10+i%4,4+i%3,3+i%2))
    return rows


class PerformanceTests(unittest.TestCase):
    def test_bad_shots_become_missing_without_dropping_the_verified_result(self):
        body=b'Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HS,AS,HST,AST\n15/08/2021,Newcastle,West Ham,2,4,A,10,8,4,9\n'
        row=parse_performance(body,2021)[0]
        self.assertEqual(row.match.away_goals,4)
        self.assertIsNone(row.away_shots);self.assertIsNone(row.away_sot)
        self.assertEqual(row.home_sot,4);self.assertTrue(row.issues)

    def test_target_and_future_stats_cannot_enter_their_own_feature_vector(self):
        rows=sample();target=rows[150].match;cutoff=target.day-timedelta(days=1)
        original=feature_state(rows,cutoff)
        changed=[replace(r,match=replace(r.match,home_goals=20,away_goals=0),home_shots=90,home_sot=80) if r.match.day>=cutoff else r for r in rows]
        after=feature_state(changed,cutoff)
        self.assertEqual(vector(original,target.home,target.away,target.day),vector(after,target.home,target.away,target.day))
        self.assertLess(original['latestMatch'],cutoff.isoformat())
        self.assertEqual(len(vector(original,target.home,target.away,target.day)),len(FEATURES))

    def test_future_outcomes_and_shots_cannot_change_a_trained_shot_model(self):
        rows=sample();cutoff=date(2024,9,1)
        first=ShotPoisson().fit(rows,cutoff)
        future=Performance(Match(cutoff,2024,'A','B',20,0),99,1,90,1)
        after=ShotPoisson().fit(rows+[future],cutoff)
        np.testing.assert_array_equal(first.theta,after.theta)
        self.assertEqual(first.shot_scale,after.shot_scale)

    def test_historical_shots_have_an_effect_and_goal_only_limit_matches_the_old_model(self):
        rows=sample();cutoff=date(2024,9,1)
        original=ShotPoisson().fit(rows,cutoff)
        changed=[replace(r,home_shots=50,home_sot=30) if r.match.home=='A' else r for r in rows]
        after=ShotPoisson().fit(changed,cutoff)
        self.assertGreater(np.max(np.abs(original.theta-after.theta)),1e-4)
        goals=ShotPoisson(goal_weight=1).fit(rows,cutoff)
        baseline=LearnedPoisson().fit([r.match for r in rows],cutoff)
        np.testing.assert_allclose(goals.theta,baseline.theta,atol=1e-12,rtol=0)

    def test_missing_statistics_are_not_zeroes_or_excluded_fixture_results(self):
        rows=sample();rows[0]=replace(rows[0],home_sot=None)
        model=ShotPoisson().fit(rows,date(2024,9,1))
        self.assertEqual(model.fit_info['training_games'],220)
        self.assertEqual(model.fit_info['shot_games'],219)


class CalibrationTests(unittest.TestCase):
    def test_calibration_uses_only_earlier_out_of_sample_labels(self):
        examples=[{'date':(date(2023,1,1)+timedelta(days=i)).isoformat(),'candidate':[.6,.25,.15],'actual':i%3} for i in range(400)]
        cutoff=date(2024,5,1);settings={'source':'candidate','mode':'temperature_bias','halfLife':730,'windowDays':1460,'ridge':.05}
        first=fit_calibration(examples,cutoff,settings)
        after=fit_calibration(examples+[{'date':cutoff.isoformat(),'candidate':[1.,0.,0.],'actual':2}],cutoff,settings)
        self.assertEqual(first,after)
        self.assertLess(first['latestTrainingMatch'],cutoff.isoformat())
        p=calibrated([.6,.25,.15],first)
        self.assertAlmostEqual(sum(p),1.)
        self.assertLess(np.mean([loss(p,i%3)['logloss'] for i in range(400)]),np.mean([loss([.6,.25,.15],i%3)['logloss'] for i in range(400)]))

    def test_identity_calibration_preserves_the_full_distribution(self):
        for model in [{'mode':'temperature','parameters':[1.]},{'mode':'temperature_bias','parameters':[1.,0.,0.]},
            {'mode':'matrix','parameters':np.r_[np.eye(3).ravel(),np.zeros(3)].tolist()}]:
            np.testing.assert_allclose(calibrated([.6,.25,.15],model),[.6,.25,.15],atol=1e-15,rtol=0)

    def test_dixon_coles_is_past_only_and_zero_correlation_recovers_independent_poisson(self):
        rows=[r.match for r in sample()];cutoff=date(2024,9,1)
        first=DixonColes().fit(rows,cutoff);after=DixonColes().fit(rows+[Match(cutoff,2024,'A','B',20,0)],cutoff)
        self.assertAlmostEqual(first.rho,after.rho,places=14)
        np.testing.assert_array_equal(first.theta,after.theta)
        prediction=first.predict('A','B',cutoff+timedelta(days=1))['probabilities']
        self.assertTrue(np.all(prediction>=0));self.assertAlmostEqual(float(prediction.sum()),1.)
        first.rho=0
        np.testing.assert_allclose(first.predict('A','B')['probabilities'],LearnedPoisson.predict(first,'A','B')['probabilities'],atol=1e-12,rtol=0)


if __name__=='__main__':unittest.main()
