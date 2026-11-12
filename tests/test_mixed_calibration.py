import copy
from datetime import date,timedelta
import json
from pathlib import Path
import unittest

from research.mixed_calibration import evaluate,PLAN
from research.hundred import DIRECTORY,REGISTERED_SHA


class MixedCalibrationTests(unittest.TestCase):
    def sample(self):
        plan=json.loads(PLAN.read_text());rows=[]
        dates=[(date(2025,1,1)+timedelta(days=i)).isoformat() for i in range(400)]+[
            '2026-07-10','2026-08-10','2026-10-10','2026-11-01']
        for i,day in enumerate(dates):
            rows.append({'fixtureId':i+1,'league':plan['leagues'][i%4],'date':day,'kickoff':day+'T15:00:00Z',
                'actual':i%3,'v1':[.45,.30,.25],'base':[.4,.3,.3]})
        replay={'protocol':'mixed-v1-replay-1','sourceCohortSha256':plan['inputCohortSha256'],
            'sourceManifest':[{'rows':len(rows)}],'rows':rows,'skipped':[]}
        return replay,plan

    def test_later_outcomes_change_evaluation_but_not_the_earlier_trained_correction(self):
        replay,plan=self.sample();before=evaluate(replay,plan)
        changed=copy.deepcopy(replay)
        for row in changed['rows']:
            if row['date']>=plan['trainingCutoffExclusive']:row['actual']=(row['actual']+1)%3
        after=evaluate(changed,plan)
        self.assertEqual(before['calibration'],after['calibration'])
        self.assertNotEqual(before['results']['audit']['metrics']['v1']['logloss'],after['results']['audit']['metrics']['v1']['logloss'])
        self.assertLess(before['calibration']['latestTrainingMatch'],plan['trainingCutoffExclusive'])
        self.assertFalse(before['promotedToDefault'])

    def test_identity_is_fixed_and_experiment_cannot_modify_the_prospective_packet(self):
        replay,plan=self.sample();path=DIRECTORY/'packet.json';before=path.read_bytes()
        result=evaluate(replay,plan)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(result['sourceCohortSha256'],REGISTERED_SHA)
        self.assertTrue(result['retrospective']);self.assertTrue(result['prospectiveValidationRequired'])
        replay['sourceCohortSha256']='different'
        with self.assertRaises(ValueError):evaluate(replay,plan)
        replay,plan=self.sample();plan['candidateCount']=2
        with self.assertRaises(ValueError):evaluate(replay,plan)


if __name__=='__main__':unittest.main()
