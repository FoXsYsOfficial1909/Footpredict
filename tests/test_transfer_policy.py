import copy
import json
import unittest

from research.transfer_policy import PLAN, eligible, predict, select, evaluate


class TransferPolicyTests(unittest.TestCase):
    def setUp(self):
        self.plan=json.loads(PLAN.read_text())
        self.rows=[{'date':f'2024-01-{i+1:02d}','actual':0,'v1':[.4,.3,.3],'joint':[.7,.2,.1],
            'shots':[.5,.3,.2],'base':[.4,.3,.3],'pastClubGames':{'A':{'E0':30,'E1':10},'B':{'E0':0,'E1':60}}}
            for i in range(10)]

    def test_eligibility_boundaries_and_noneligible_probabilities_are_exactly_baseline(self):
        rule=self.plan['eligibility']
        self.assertTrue(eligible({'A':{'E0':20,'E1':19}},rule))
        self.assertFalse(eligible({'A':{'E0':20,'E1':20}},rule))
        self.assertFalse(eligible({'A':{'E0':19,'E1':19}},rule))
        rows=copy.deepcopy(self.rows)
        for row in rows:row['pastClubGames']={'A':{'E0':30,'E1':20},'B':{'E0':0,'E1':60}}
        before=copy.deepcopy(rows);after=predict(rows,1,rule)
        self.assertTrue(all(r['candidate']==r['v1'] and not r['policyChanged'] for r in after))
        self.assertEqual(rows,before)

    def test_weight_uses_earlier_labels_and_zero_applicable_audit_is_not_evidence(self):
        chosen=select(self.rows,self.plan)
        audit=copy.deepcopy(self.rows)
        for row in audit:row['pastClubGames']={'A':{'E0':0,'E1':60}}
        periods={'validation':self.rows,'audit':audit,'monitoring':copy.deepcopy(self.rows)}
        first=evaluate(periods,chosen,self.plan)[0]
        for row in periods['monitoring']:row['actual']=2
        after=evaluate(periods,chosen,self.plan)[0]
        self.assertEqual(chosen,select(self.rows,self.plan));self.assertEqual(chosen['weight'],1)
        self.assertEqual(first['audit']['adjustmentEvidence'],'no_applicable_fixtures')
        self.assertEqual(first['audit']['metrics']['candidate'],first['audit']['metrics']['v1'])
        self.assertEqual(first['validation'],after['validation'])
        self.assertNotEqual(first['monitoring']['metrics']['candidate'],after['monitoring']['metrics']['candidate'])

    def test_no_second_threshold_search_or_forced_nonzero_weight(self):
        rows=[{**r,'joint':list(r['v1'])} for r in self.rows]
        self.assertEqual(select(rows,self.plan)['weight'],0)
        changed=copy.deepcopy(self.plan);changed['eligibility']['E1GamesExclusiveMaximum']=30
        with self.assertRaises(ValueError):select(rows,changed)


if __name__=='__main__':unittest.main()
