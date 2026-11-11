import copy
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from research.availability_experiment import (PROTOCOL,BASELINE,SHARES,MIN_TRAIN,canonical,
    ingest_entry,select_day_ahead,fit_correction,predict,metrics,target,evaluate,chronological_pairs)
from scripts.publish_availability import merge_state


def training_rows(n=90):
    start=datetime(2026,7,1,12,tzinfo=timezone.utc)
    rows=[]
    for i in range(n):
        kickoff=start+timedelta(days=i);label=i%3;x=.4 if label==0 else -.4 if label==2 else 0.
        features={'horizon_hours':48}
        for side in ('home','away'):
            features[side+'_fpl_minutes_complete']=True
            features[side+'_fpl_source_available']=True
            features[side+'_provider_xi_received']=False
            for key in SHARES:features[side+'_fpl_'+key]=0.
        features['away_fpl_'+SHARES[0]]=max(0,x);features['home_fpl_'+SHARES[0]]=max(0,-x)
        rows.append({'fixture_id':i+1,'home':'Home','away':'Away','kickoff':kickoff.isoformat(),
            'collected_at':(kickoff-timedelta(hours=48)).isoformat(),'snapshot_sha256':hashlib.sha256(str(i).encode()).hexdigest(),
            'archive_sha256':hashlib.sha256(('archive'+str(i)).encode()).hexdigest(),
            'forecast':{'model_version':BASELINE,'probabilities':[.4,.2,.4]},'features':features,'target':label})
    return rows


class AvailabilityExperimentTests(unittest.TestCase):
    def test_model_learns_from_signal_and_produces_normalized_odds_not_fixed_injury_penalties(self):
        rows=training_rows();now=datetime(2026,11,1,tzinfo=timezone.utc)
        model=fit_correction(rows,now)
        self.assertIsNotNone(model);self.assertGreater(model['coefficients'][0],0)
        p=predict([.4,.2,.4],[.4,0,0],model)
        self.assertGreater(p[0],p[2]);self.assertAlmostEqual(sum(p),1)
        self.assertTrue(all(0<=n<=1 for n in p))
        np.testing.assert_allclose(predict([.4,.2,.4],[0,0,0],model),[.4,.2,.4])
        self.assertFalse(model['promotedToDefault']);self.assertFalse(model['lineupsUsed'])

    def test_future_outcomes_and_small_or_constant_samples_cannot_produce_a_trained_model(self):
        rows=training_rows();cutoff=datetime(2026,9,1,tzinfo=timezone.utc)
        before=fit_correction(rows,cutoff)
        modified=copy.deepcopy(rows)
        for row in modified:
            if datetime.fromisoformat(row['kickoff'])>cutoff-timedelta(hours=4):row['target']=(row['target']+1)%3
        self.assertEqual(before,fit_correction(modified,cutoff))
        self.assertIsNone(fit_correction(rows[:MIN_TRAIN-1],cutoff))
        for row in modified:
            for side in ('home','away'):row['features'][side+'_fpl_'+SHARES[0]]=0
        self.assertIsNone(fit_correction(modified,datetime(2026,11,1,tzinfo=timezone.utc)))

    def test_chronological_evaluation_waits_for_prior_results_and_reports_paired_denominators(self):
        rows=training_rows();pairs=chronological_pairs(rows)
        self.assertGreater(len(pairs),0);self.assertLess(len(pairs),len(rows)-MIN_TRAIN+1)
        result=metrics(pairs);self.assertEqual(result['n'],len(pairs))
        self.assertLess(result['availability']['logLoss'],result['baseline']['logLoss'])
        self.assertEqual(metrics([])['baseline'],None)

    def test_selection_deduplicates_fixture_and_keeps_late_xi_out_of_day_ahead_fits(self):
        rows=training_rows(3);duplicate=copy.deepcopy(rows[0]);duplicate['collected_at']='2026-06-30T10:00:00+00:00'
        duplicate['features']['horizon_hours']=26
        late=copy.deepcopy(rows[1]);late['fixture_id']=99;late['features']['horizon_hours']=1
        selected,reasons=select_day_ahead(rows+[duplicate,late])
        self.assertEqual(len(selected),3);self.assertEqual(selected[0]['collected_at'],duplicate['collected_at'])
        self.assertEqual(reasons['outside_24_to_168_hour_window'],1)

    def test_archives_are_checksum_verified_immutable_and_never_mutate_original_forecasts(self):
        with tempfile.TemporaryDirectory() as directory:
            payload='{"fixture":{"id":1},"text":"Цитата"}'
            entry={'id':'source:1','payload':payload,'sha256':hashlib.sha256(payload.encode()).hexdigest()}
            ingest_entry(entry,directory);ingest_entry(entry,directory)
            changed={**entry,'payload':'{}','sha256':hashlib.sha256(b'{}').hexdigest()}
            with self.assertRaisesRegex(ValueError,'changed'):ingest_entry(changed,directory)
            with self.assertRaisesRegex(ValueError,'checksum'):ingest_entry({**entry,'payload':'{}'},directory)
        old={'schemaVersion':1,'protocol':PROTOCOL,'cursor':'1','forecasts':{'1':{'p':[.4,.2,.4]}}}
        new=copy.deepcopy(old);new['forecasts']['1']['p']=[.5,.2,.3]
        with self.assertRaisesRegex(ValueError,'overwritten'):merge_state(old,new)

    def test_score_identity_status_and_result_fence_prevent_fake_labels(self):
        row=training_rows(1)[0];now=datetime.fromisoformat(row['kickoff'])+timedelta(hours=5)
        game={'id':1,'status':'FINISHED','utcDate':row['kickoff'],'homeTeam':{'name':'Home'},'awayTeam':{'name':'Away'},
            'score':{'duration':'REGULAR','fullTime':{'home':2,'away':1}}}
        self.assertEqual(target(row,{1:game},now),0)
        for bad in [{**game,'status':'TIMED'},{**game,'homeTeam':{'name':'Other'}},
            {**game,'utcDate':(now+timedelta(days=1)).isoformat()}]:self.assertIsNone(target(row,{1:bad},now))
        self.assertIsNone(target(row,{1:game},now-timedelta(hours=2)))

    def test_ready_model_freezes_future_forecast_once_and_verifies_exact_archived_weights(self):
        rows=training_rows();now=datetime(2026,11,1,tzinfo=timezone.utc)
        future=copy.deepcopy(rows[0]);future.update(fixture_id=999,kickoff=(now+timedelta(hours=36)).isoformat(),
            collected_at=(now-timedelta(hours=12)).isoformat(),snapshot_sha256='f'*64,archive_sha256='a'*64,target=None)
        rows.append(future)
        games={r['fixture_id']:{'id':r['fixture_id'],'status':'FINISHED','utcDate':r['kickoff'],
            'homeTeam':{'name':r['home']},'awayTeam':{'name':r['away']},'score':{'duration':'REGULAR',
                'fullTime':{'home':2 if r['target']==0 else 1,'away':2 if r['target']==2 else 1}}}
            for r in rows if r['target'] is not None}
        counts={'snapshots':len(rows),'fixtures':len(rows),'validFeatureSnapshots':len(rows),'rejectedSnapshots':{}}
        with tempfile.TemporaryDirectory() as directory,patch('research.availability_experiment.archive_rows',return_value=(rows,counts)):
            state={'schemaVersion':1,'protocol':PROTOCOL,'cursor':'1','forecasts':{}}
            report,model=evaluate(directory,state,games,now)
            self.assertEqual(report['status'],'candidate');self.assertEqual(report['prospective']['registered'],1)
            frozen=copy.deepcopy(state['forecasts']['999'])
            evaluate(directory,state,games,now+timedelta(hours=1))
            self.assertEqual(state['forecasts']['999'],frozen)
            self.assertTrue((Path(directory)/'models'/(frozen['modelSha256']+'.json')).exists())
            self.assertFalse(report['promotedToDefault']);self.assertFalse(report['promotionEligible'])
            state['forecasts']['999']['availability'][0]=.999
            with self.assertRaisesRegex(ValueError,'archived weights'):evaluate(directory,state,games,now+timedelta(hours=2))


if __name__=='__main__':unittest.main()
