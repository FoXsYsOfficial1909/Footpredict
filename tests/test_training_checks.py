import copy
import unittest

from scripts.publish_training import merge_check, merge_latest


class TrainingCheckTests(unittest.TestCase):
    def setUp(self):
        self.metadata=dict(repository='FoXsYsOfficial1909/Footpredict',run_id='123',run_attempt='1',commit='a'*40)
        self.success={'schemaVersion':1,'status':'success','generatedAt':'2026-11-15T00:23:00Z','asOf':'2026-11-15',
            'trainingCutoffExclusive':'2026-11-14','latestTrainingMatch':'2026-11-08',
            'evaluation':{'commonMatches':120},'modelSha256':'b'*64,'trainingFingerprint':'c'*64,'promotedToDefault':False}
        self.check={'schemaVersion':1,'protocol':'continuous-training-1','status':'unchanged',
            'checkedAt':'2026-11-15T06:43:00Z','asOf':'2026-11-15','league':'PL','division':'E0','method':'goals',
            'trainingFingerprint':'c'*64,'modelSha256':'b'*64,'reason':'Identical training inputs.','promotedToDefault':False}

    def test_unchanged_check_does_not_refresh_model_or_training_dates(self):
        old=merge_latest(None,self.success,**self.metadata);before=copy.deepcopy(old)
        result=merge_check(old,self.check,**self.metadata)
        self.assertEqual(result['latestAttempt'],old['latestAttempt'])
        self.assertEqual(result['latestSuccessful'],old['latestSuccessful'])
        self.assertEqual(result['latestCheck']['status'],'unchanged');self.assertEqual(old,before)
        self.assertNotEqual(result['latestCheck']['checkedAt'],result['latestSuccessful']['generatedAt'])

    def test_skip_cannot_claim_a_different_model_or_scope_or_fingerprint(self):
        previous=merge_latest(None,self.success,**self.metadata)
        for patch in ({'modelSha256':'d'*64},{'trainingFingerprint':'d'*64},{'league':'ELC'},
                      {'method':'shots_on_target'},{'promotedToDefault':True}):
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                merge_check(previous,{**self.check,**patch},**self.metadata)
        with self.assertRaises(ValueError):merge_check(None,self.check,**self.metadata)

    def test_source_failure_is_recorded_without_fake_training_and_last_weights_survive(self):
        failed={**self.check,'status':'failed'}
        initial=merge_check(None,failed,**self.metadata)
        self.assertIsNone(initial['latestAttempt']);self.assertIsNone(initial['latestSuccessful'])
        old=merge_latest(None,self.success,**self.metadata)
        after=merge_check(old,failed,**self.metadata)
        self.assertEqual(after['latestSuccessful'],old['latestSuccessful'])
        new=merge_latest(after,{**self.success,'generatedAt':'2026-11-16T00:23:00Z'},**self.metadata)
        self.assertEqual(new['latestCheck'],after['latestCheck'])

    def test_older_checks_do_not_hide_a_newer_failure(self):
        old=merge_check(merge_latest(None,self.success,**self.metadata),self.check,**self.metadata)
        with self.assertRaises(ValueError):
            merge_check(old,{**self.check,'checkedAt':'2026-11-15T00:43:00Z'},**self.metadata)


if __name__=='__main__':unittest.main()
