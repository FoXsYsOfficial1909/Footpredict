import copy
from datetime import date,timedelta
import json
import hashlib
from pathlib import Path
import tempfile
from itertools import count
import unittest
from unittest.mock import patch

import numpy as np

from research.data import Match
from research.nightly import build_candidate,run,sha
from scripts.publish_training import merge_latest,save_public_model


def sample():
    rng=np.random.default_rng(62);teams=['A','B','C','D','E','F'];rows=[]
    for i in range(220):
        home=teams[i%6];away=teams[(i%6+1+(i//6)%5)%6]
        rows.append(Match(date(2023,9,1)+timedelta(days=i),2023,home,away,int(rng.poisson(1.6)),int(rng.poisson(1.1))))
    return rows


class NightlyTests(unittest.TestCase):
    def setUp(self):
        self.settings=json.loads((Path(__file__).resolve().parents[1]/'docs/research/selection.json').read_text())
        self.day=date(2024,4,10)

    def build(self,rows):return build_candidate(rows,self.settings,self.day,evaluation_limit=12,min_common=5)

    def test_same_day_previous_day_and_future_scores_do_not_change_the_candidate(self):
        rows=sample();first,report,_=self.build(rows)
        future=[Match(self.day+timedelta(days=n),2023,'A','B',20,0) for n in [-1,0,1]]
        second,changed,_=self.build(rows+future)
        self.assertEqual(first,second)
        self.assertEqual(report['dataSha256'],changed['dataSha256'])
        self.assertLess(report['latestTrainingMatch'],report['trainingCutoffExclusive'])
        self.assertEqual(report['modelSha256'],sha(first))
        self.assertFalse(report['promotedToDefault'])

    def test_a_target_result_cannot_change_its_own_historical_prediction(self):
        rows=sample();_,report,predictions=self.build(rows)
        changed=rows[:-1]+[Match(rows[-1].day,rows[-1].season,rows[-1].home,rows[-1].away,10,0)]
        _,_,after=self.build(changed)
        np.testing.assert_array_equal(predictions[-1]['candidate'],after[-1]['candidate'])
        ns={m['n'] for m in report['evaluation']['metrics'].values()}
        self.assertEqual(ns,{report['evaluation']['commonMatches']})

    def test_failed_run_has_no_stale_model_and_writes_an_explicit_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'model.json').write_text('{"old":true}')
            with patch('research.nightly.download_season',side_effect=OSError('network unavailable')):
                with self.assertRaises(OSError):run(root/'data',root,self.day)
            self.assertFalse((root/'model.json').exists())
            attempt=json.loads((root/'attempt.json').read_text())
            self.assertEqual(attempt['status'],'failed');self.assertFalse(attempt['promotedToDefault'])
            self.assertNotIn('network unavailable',json.dumps(attempt))

    def test_timing_measurements_are_report_only_and_cannot_change_forecasts(self):
        rows=sample()
        with patch('research.nightly.time.monotonic',side_effect=count(10,1)):
            first,report,predictions=self.build(rows)
        with patch('research.nightly.time.monotonic',side_effect=count(1000,2)):
            after,changed,other_predictions=self.build(rows)
        self.assertEqual(first,after)
        self.assertEqual(report['modelSha256'],changed['modelSha256'])
        self.assertEqual(predictions,other_predictions)
        self.assertNotEqual(report['timings'],changed['timings'])
        self.assertTrue(all(value>=0 for value in report['timings'].values()))
        self.assertNotIn('timings',first)


class PublishTests(unittest.TestCase):
    def success(self):
        return {'schemaVersion':1,'status':'success','generatedAt':'2026-11-04T00:23:00+00:00','asOf':'2026-11-04',
            'trainingCutoffExclusive':'2026-11-03','latestTrainingMatch':'2026-11-02','evaluation':{'commonMatches':110},
            'modelSha256':'a'*64,'promotedToDefault':False}

    def merge(self,previous,attempt):
        return merge_latest(previous,attempt,repository='FoXsYsOfficial1909/Footpredict',run_id='123',run_attempt='1',commit='b'*40)

    def test_failure_retains_last_success_and_preserves_attempt_provenance(self):
        old=self.merge(None,self.success());before=copy.deepcopy(old)
        failure={'schemaVersion':1,'status':'failed','generatedAt':'2026-11-05T00:23:00+00:00','promotedToDefault':False}
        merged=self.merge(old,failure)
        self.assertEqual(merged['latestSuccessful'],old['latestSuccessful'])
        self.assertEqual(merged['latestAttempt']['status'],'failed')
        self.assertEqual(merged['latestAttempt']['runUrl'],'https://github.com/FoXsYsOfficial1909/Footpredict/actions/runs/123')
        self.assertEqual(old,before)
        self.assertTrue(old['latestSuccessful']['modelUrl'].endswith('/models/'+'a'*64+'.json'))

    def test_public_model_bytes_match_the_report_hash_and_archive_is_immutable(self):
        model={'model_version':'ensemble-candidate-example','coefficient':1.0,'team':'Команда'}
        attempt={'status':'success','modelVersion':model['model_version'],'modelSha256':sha(model)}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);output=root/'output';output.mkdir();(output/'model.json').write_text(json.dumps(model))
            target=save_public_model(output,root,attempt)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),attempt['modelSha256'])
            self.assertEqual(json.loads(target.read_bytes()),model)
            self.assertEqual(save_public_model(output,root,attempt),target)
            target.write_text('corrupted')
            with self.assertRaises(ValueError):save_public_model(output,root,attempt)
            self.assertEqual(target.read_text(),'corrupted')
            with self.assertRaises(ValueError):save_public_model(output,root,{**attempt,'modelSha256':'../escape'})

    def test_publication_rejects_weights_that_do_not_match_the_successful_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'model.json').write_text('{"model_version":"other"}')
            with self.assertRaises(ValueError):save_public_model(root,root,{**self.success(),'modelVersion':'expected'})
            self.assertFalse((root/'models').exists())

    def test_old_attempt_or_leaking_training_boundary_is_rejected(self):
        old=self.merge(None,self.success());older={**self.success(),'generatedAt':'2026-11-03T00:23:00+00:00'}
        with self.assertRaises(ValueError):self.merge(old,older)
        with self.assertRaises(ValueError):self.merge(None,{**self.success(),'latestTrainingMatch':'2026-11-03'})
        with self.assertRaises(ValueError):self.merge(None,{**self.success(),'promotedToDefault':True})


if __name__=='__main__':unittest.main()
