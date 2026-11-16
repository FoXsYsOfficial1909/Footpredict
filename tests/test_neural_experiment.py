import copy
from datetime import date
import json
from pathlib import Path
import unittest

import numpy as np

from research.neural_experiment import (PLAN,ROOT,validate_plan,source_games,training_weights,
    macro,macro_interval,select,train,predict,evaluate,gate,execute)


class NeuralExperimentTests(unittest.TestCase):
    def setUp(self):self.plan=json.loads(PLAN.read_text())

    def rows(self):
        return [{'date':f'2022-01-{i+1:02d}','division':d,'actual':i%3,
            'features':[float(i),float(j)],'base':[.4,.3,.3],'frequency':[1/3]*3,'v1':None,
            'v1_available':False} for i in range(12) for j,d in enumerate(('E0','E1','E2'))]

    def test_training_weights_balance_leagues_and_reject_future_labels(self):
        rows=self.rows()+[dict(r) for r in self.rows() if r['division']=='E2']
        w=training_weights(rows,'2023-07-01')
        for d in ('E0','E1','E2'):
            self.assertAlmostEqual(sum(a for r,a in zip(rows,w) if r['division']==d),1/3)
        with self.assertRaises(ValueError):training_weights(rows,'2022-01-12')
        with self.assertRaises(ValueError):training_weights([r for r in rows if r['division']!='E2'],'2023-07-01')

    def test_macro_is_equal_division_mean_not_fixture_weighted(self):
        rows=[{'division':d,'actual':0,'p':p} for d,p in
              [('E0',[.8,.1,.1]),('E1',[.5,.25,.25])]+[('E2',[.2,.4,.4])]*20]
        actual=macro(rows,'p')['logLoss']
        self.assertAlmostEqual(actual,float(np.mean(-np.log([.8,.5,.2]))))
        same=macro_interval([{**r,'date':'2025-01-01','selected':r['p'],'base':r['p']} for r in rows],samples=20)
        self.assertEqual(same['difference'],0);self.assertEqual(same['ci95'],[0.,0.])
        missing=macro_interval([{'division':'E0','date':'2025-01-01','actual':0,'selected':[.8,.1,.1],'base':[.8,.1,.1]}])
        self.assertIsNone(missing['ci95'])

    def test_fit_preprocessing_is_fold_only_and_zero_blend_retains_all_fallbacks(self):
        rows=self.rows();before=copy.deepcopy(rows)
        model=train(rows,'2023-07-01',self.plan)
        selected=predict(rows,model,0)
        self.assertTrue(all(r['selected']==r['base'] for r in selected))
        block=evaluate(selected,self.plan)
        self.assertEqual(block['matches'],len(rows));self.assertEqual(block['frequencyFallback'],len(rows))
        self.assertEqual(block['v1Common']['matches'],0)
        self.assertEqual(rows,before)
        self.assertLess(model['provenance']['latestTrainingMatch'],model['provenance']['cutoffExclusive'])
        later=[{**r,'features':[10000.,-10000.],'actual':2} for r in rows]
        saved=copy.deepcopy(model);predict(later,model,1);self.assertEqual(model,saved)

    def test_selection_uses_only_earlier_rows_and_ties_allow_zero(self):
        rows=self.rows();model=train(rows,'2023-07-01',self.plan)
        model['weights']['output']=[[0.,0.,0.] for _ in range(16)]
        model['weights']['outputBias']=[0.,0.,0.]
        selection=select(rows,model,self.plan)
        # Floating normalization may differ at machine precision, so use an exact uniform base.
        uniform=[{**r,'base':[1/3]*3} for r in rows]
        selection=select(uniform,model,self.plan)
        self.assertEqual(selection['weight'],0)
        before=copy.deepcopy(selection)
        later=[{**r,'actual':2} for r in rows]
        evaluate(predict(later,model,selection['weight']),self.plan)
        self.assertEqual(before,selection)
        self.assertFalse(gate({},selection))

    def test_pinned_source_and_plan_cannot_expand_or_overwrite_other_studies(self):
        self.assertEqual(len(source_games(self.plan)),14002)
        for changed in ({'neuralArchitectures':2},{'settings':{**self.plan['settings'],'seed':99}},
                        {'blendWeights':[.5,1.]},{'automaticPromotion':True}):
            with self.assertRaises(ValueError):validate_plan({**self.plan,**changed})
        with self.assertRaises(ValueError):source_games({**self.plan,'sourceCompressedSha256':'0'*64})
        for p in (ROOT/'docs/research',ROOT/'docs/research/hundred-2026-11-11'):
            with self.assertRaisesRegex(ValueError,'overwrite'):execute(p)


if __name__=='__main__':unittest.main()
