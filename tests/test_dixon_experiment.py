import copy
from datetime import date, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from research.data import Match
from research.dixon_experiment import PLAN, ROOT, DIRECTORY, load_inputs, validate_plan, rate_rows, select, evaluate, research_gate, execute
from research.nightly import sha
from research.quality_diagnostic import metrics


class DixonExperimentTests(unittest.TestCase):
    def setUp(self):
        self.plan=json.loads(PLAN.read_text())

    def sample(self):
        teams=list('ABCDEFGH')
        history=[Match(date(2022,7,1)+timedelta(days=i),2022,teams[i%8],teams[(i+1+i//8)%8]
                       if (i+1+i//8)%8!=i%8 else teams[(i+2)%8],i%4,(i*3)%3) for i in range(300)]
        targets=[Match(date(2023,9,1)+timedelta(days=i),2023,'A','B',i%2,1) for i in range(4)]
        return history,targets

    def test_target_and_immediately_previous_day_scores_cannot_change_its_rates(self):
        history,targets=self.sample();before,_=rate_rows(history+targets,[2023],self.plan['glm'])
        changed=history+targets[:-2]+[
            Match(targets[-2].day,2023,'A','B',20,0),
            Match(targets[-1].day,2023,'A','B',0,20),
            Match(targets[-1].day+timedelta(days=1),2023,'B','A',30,0)]
        after,_=rate_rows(changed,[2023],self.plan['glm'])
        for key in ('home_rate','away_rate','rate_cutoff','latestTrainingDate','trainingSha256'):
            self.assertEqual(before[-1][key],after[-2][key])
        self.assertLess(before[-1]['latestTrainingDate'],before[-1]['rate_cutoff'])
        self.assertEqual({r['season'] for r in before},{2023})
        self.assertNotEqual(before[-1]['actual'],after[-2]['actual'])

    def rows(self,better=True):
        return [{'date':f'2024-01-{i+1:02d}','home':'A','away':'B','actual':0,
            'v1':[.45,.3,.25],'base':[.4,.3,.3],'uncorrected':[.5,.3,.2],
            'fitted':[.6,.25,.15] if better else [.4,.3,.3],'unseenTeams':[]} for i in range(4)]

    def test_only_early_log_loss_chooses_one_fixed_parameter_and_zero_is_allowed(self):
        earlier=self.rows();selection=select(earlier,-.05,self.plan)
        self.assertEqual(selection['selectedRho'],-.05)
        later=self.rows()
        for row in later:row['actual']=2
        evaluate(later,selection,self.plan)
        self.assertEqual(selection,select(earlier,-.05,self.plan))
        worse=select(self.rows(False),-.05,self.plan)
        self.assertEqual(worse['selectedRho'],0)
        block,predicted=evaluate(self.rows(False),worse,self.plan)
        self.assertTrue(all(r['selected']==r['uncorrected'] for r in predicted))
        self.assertEqual(block['metrics']['selected'],block['metrics']['uncorrected'])

    def test_common_cohort_exclusions_and_failed_later_period_are_visible(self):
        rows=self.rows();rows[-1]['v1']=None
        selection=select(rows,-.05,self.plan);block,_=evaluate(rows,selection,self.plan)
        self.assertEqual(block['completedMatches'],4);self.assertEqual(block['commonMatches'],3)
        self.assertEqual(block['excludedFromEveryModel'],1)
        self.assertEqual({m['n'] for m in block['metrics'].values()},{3})
        self.assertEqual(block['fullCoverageMetrics']['selected']['n'],4)
        bad=copy.deepcopy(block);bad['metrics']['selected']['logLoss']=bad['metrics']['v1']['logLoss']+1
        self.assertFalse(research_gate({'audit':block,'monitoring':bad},selection))

    def test_source_archive_and_frozen_settings_are_verified(self):
        matches,_,counts=load_inputs(self.plan)
        self.assertEqual(len(matches),4280);self.assertEqual(counts[2026],100)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/self.plan['sourceArchive'];source.parent.mkdir(parents=True)
            source.write_bytes((ROOT/self.plan['sourceArchive']).read_bytes()+b'changed')
            with self.assertRaises(ValueError):load_inputs(self.plan,root)
        for patch in ({'rhoBounds':[-.2,.04]},{'maximumCandidates':3},{'auditSeasons':[2024,2025]}):
            changed={**self.plan,**patch}
            with self.assertRaises(ValueError):validate_plan(changed)
        for output in (ROOT/'docs/research', ROOT/'docs/research/hundred-2026-11-11'):
            with self.subTest(output=output),self.assertRaises(ValueError):execute(output)

    def test_saved_predictions_reproduce_scores_selection_and_early_dependence_cohort(self):
        report=json.loads((DIRECTORY/'report.json').read_text())
        raw=(DIRECTORY/'rho-training-rows.json.gz').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),report['rhoTrainingCompressedSha256'])
        early=json.loads(gzip.decompress(raw))
        self.assertEqual(len(early),1900)
        self.assertEqual({r['season'] for r in early},set(range(2018,2023)))
        self.assertTrue(all(r['rate_cutoff']<r['date']<'2023-07-01' for r in early))
        self.assertEqual(sha(early),report['rhoTrainingRowsSha256'])
        for period in ('validation','audit','monitoring'):
            rows=json.loads((DIRECTORY/(period+'-predictions.json')).read_text())
            self.assertEqual(sha(rows),report['predictionSha256'][period])
            common=[r for r in rows if r['v1'] is not None]
            for method in ('selected','fitted','uncorrected','v1','base'):
                actual=metrics(common,method);saved=report['results'][period]['metrics'][method]
                self.assertEqual(actual,saved)
            if period=='validation':
                before_selection=[{k:v for k,v in r.items() if k!='selected'} for r in rows]
                self.assertEqual(sha(before_selection),report['selection']['validationDataSha256'])


if __name__=='__main__':unittest.main()
