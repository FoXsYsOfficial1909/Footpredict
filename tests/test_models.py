import unittest
from datetime import date,timedelta
import numpy as np

from research.data import Match
from research.models import LearnedPoisson,loss,score_probabilities,v1_statistics


def sample_matches():
    rng=np.random.default_rng(49)
    teams=['A','B','C','D','E','F']
    result=[]
    for i in range(220):
        home=teams[i%6];away=teams[(i%6+1+(i//6)%5)%6]
        result.append(Match(date(2024,1,1)+timedelta(days=i),2023,home,away,
                            int(rng.poisson(2.5 if home=='A' else 1.4)),int(rng.poisson(1.1))))
    return result


class ModelTests(unittest.TestCase):
    def test_poisson_symmetry_and_normalization(self):
        p=score_probabilities(1.5,1.5)
        self.assertAlmostEqual(p[0],p[2],places=12)
        self.assertAlmostEqual(float(p.sum()),1.)
        self.assertGreater(score_probabilities(2.5,.7)[0],score_probabilities(2.5,.7)[2])

    def test_objective_gradient(self):
        rng=np.random.default_rng(8);n=4
        theta=rng.normal(0,.1,size=2+2*n)
        args=(np.array([0,1,2,3]),np.array([1,2,3,0]),np.array([2,0,1,3]),np.array([0,1,1,2]),np.array([1.,.8,.5,.3]),.15,n)
        _,gradient=LearnedPoisson.objective(theta,*args)
        numerical=[]
        for index in range(len(theta)):
            left,right=theta.copy(),theta.copy();left[index]-=1e-6;right[index]+=1e-6
            numerical.append((LearnedPoisson.objective(right,*args)[0]-LearnedPoisson.objective(left,*args)[0])/2e-6)
        np.testing.assert_allclose(gradient,numerical,rtol=1e-5,atol=1e-7)

    def test_future_results_cannot_change_fit(self):
        rows=sample_matches();cutoff=date(2024,9,1)
        original=LearnedPoisson().fit(rows,cutoff)
        future=Match(cutoff+timedelta(days=1),2024,'A','B',20,0)
        changed=LearnedPoisson().fit(rows+[future],cutoff)
        np.testing.assert_allclose(original.theta,changed.theta,rtol=0,atol=0)
        self.assertLess(original.fit_info['latest_training_date'],cutoff.isoformat())

    def test_v1_target_and_future_excluded(self):
        rows=sample_matches();day=date(2024,9,1)
        before=v1_statistics(rows,day,2024,'A','B')
        after=v1_statistics(rows+[Match(day,2024,'A','B',20,0),Match(day+timedelta(days=1),2024,'A','B',0,20)],day,2024,'A','B')
        np.testing.assert_array_equal(before['probabilities'],after['probabilities'])

    def test_unseen_teams_are_explicit_and_future_fit_rejected(self):
        model=LearnedPoisson().fit(sample_matches(),date(2024,9,1))
        prediction=model.predict('Promoted','B',date(2024,9,2))
        self.assertEqual(prediction['unseen_teams'],['Promoted'])
        self.assertAlmostEqual(float(prediction['probabilities'].sum()),1.)
        with self.assertRaises(ValueError):model.predict('A','B',date(2024,8,1))

    def test_losses_use_probability_of_actual_outcome(self):
        self.assertLess(loss([.8,.1,.1],0)['logloss'],loss([.1,.1,.8],0)['logloss'])
        self.assertAlmostEqual(loss([1.,0.,0.],0)['brier'],0.)
        with self.assertRaises(ValueError):loss([1.,1.,1.],0)


if __name__=='__main__':unittest.main()
