from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from research.transfer_future import DIRECTORY,PROTOCOL,REGISTERED_SHA,verify_files,freeze,settle,joint_forecast
from research.elc_future import (REGISTERED_SHA as PREVIOUS_SHA,verify_files as verify_previous,
    settle as settle_previous,settle_files as settle_shared_files)
from research.hundred import REGISTERED_SHA as ORIGINAL_SHA,verify_files as verify_original,instant
from scripts.publish_elc_comparison import compact,merge_latest


class TransferFutureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.packet=verify_files();cls.previous=verify_previous();cls.original=verify_original()

    def source(self,checked=None):
        return {'schemaVersion':1,'protocol':'hundred-future-1','packetSha256':ORIGINAL_SHA,'status':'active',
            'checkedAt':checked or self.packet['registeredAt'],'forecastsChanged':False,
            'fixtures':[{'fixtureId':f['fixtureId'],'status':'pending','outcome':None,'score':None,'reason':None}
                        for f in self.original['fixtures']]}

    def test_only_six_of_the_same_36_forecasts_change_and_earlier_packets_remain_exact(self):
        p=self.packet;self.assertEqual(p['forecastCount'],36);self.assertEqual(p['applicableMatches'],6)
        self.assertEqual(p['changedMatches'],6);old={f['fixtureId']:f for f in self.previous['fixtures']}
        self.assertEqual(set(old),{f['fixtureId'] for f in p['fixtures']})
        for f in p['fixtures']:
            self.assertEqual(f['v1'],old[f['fixtureId']]['v1'])
            self.assertEqual(f['previousShots'],old[f['fixtureId']]['candidate'])
            self.assertGreaterEqual(instant(f['kickoff'])-instant(p['registeredAt']),timedelta(hours=24))
            if not f['policyApplicable']:self.assertEqual(f['candidate'],f['v1'])
        self.assertEqual(self.previous,verify_previous())

    def test_registration_refuses_overwrite_and_rejects_a_changed_eligibility_or_unknown_club(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'packet';shutil.copytree(DIRECTORY,root)
            before=(root/'packet.json').read_bytes()
            with self.assertRaises(ValueError):freeze(root)
            self.assertEqual((root/'packet.json').read_bytes(),before)
            changed=json.loads(before);changed['fixtures'][0]['policyApplicable']=not changed['fixtures'][0]['policyApplicable']
            raw=json.dumps(changed).encode();digest=hashlib.sha256(raw).hexdigest()
            (root/'packet.json').write_bytes(raw);(root/'packet.sha256').write_text(digest)
            with self.assertRaises(ValueError):verify_files(root)
            with self.assertRaises(ValueError):verify_files(root,expected_sha=digest)
        with self.assertRaises(ValueError):joint_forecast(self.packet['model'],'Missing','Wolves')

    def test_full_and_applicable_results_have_distinct_paired_counts(self):
        p=self.packet;source=self.source();empty=settle(p,source,instant(source['checkedAt']))
        self.assertEqual(empty['counts']['pending'],36);self.assertEqual(empty['applicableCounts']['pending'],6)
        self.assertTrue(all(m['n']==0 and m['accuracy'] is None for m in empty['metrics'].values()))
        normal=next(f for f in p['fixtures'] if not f['policyApplicable'])
        special=next(f for f in p['fixtures'] if f['policyApplicable'])
        checked=max(instant(normal['kickoff']),instant(special['kickoff']))+timedelta(hours=5)
        source['checkedAt']=checked.isoformat();index={r['fixtureId']:r for r in source['fixtures']}
        index[normal['fixtureId']].update(status='settled',score={'home':1,'away':0},outcome=0)
        first=settle(p,source,checked)
        self.assertEqual(first['metrics']['candidate']['n'],1)
        self.assertEqual(first['applicableMetrics']['candidate']['n'],0)
        self.assertEqual(first['metrics']['candidate'],first['metrics']['v1'])
        index[special['fixtureId']].update(status='settled',score={'home':0,'away':0},outcome=1)
        second=settle(p,source,checked)
        self.assertEqual({m['n'] for m in second['metrics'].values()},{2})
        self.assertEqual({m['n'] for m in second['applicableMetrics'].values()},{1})
        self.assertEqual(second['metrics']['previousShots'],settle_previous(self.previous,source,checked)['metrics']['candidate'])
        self.assertFalse(second['forecastsChanged']);self.assertEqual(second['additionalProviderRequests'],0)

    def test_comparison_registry_cannot_overwrite_the_prior_archive_and_failed_adapter_keeps_identity(self):
        source=self.source();report=settle(self.packet,source,instant(source['checkedAt']))
        with self.assertRaises(ValueError):compact(report)
        view=compact(report,comparison='transfer');self.assertEqual(view['packetSha256'],REGISTERED_SHA)
        args={'repository':'FoXsYsOfficial1909/Footpredict','run_id':'123','attempt':'1','commit':'b'*40}
        merged=merge_latest(None,report,comparison='transfer',**args)
        with self.assertRaises(ValueError):merge_latest(merged,report,comparison='shots',**args)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaises(OSError):
                settle_shared_files(root/'missing-source.json',root/'out',packet_loader=verify_files,result_fn=settle,
                    protocol=PROTOCOL,packet_sha=REGISTERED_SHA)
            failed=json.loads((root/'out/report.json').read_text())
            self.assertEqual(failed['protocol'],PROTOCOL);self.assertEqual(failed['packetSha256'],REGISTERED_SHA)
            current=merge_latest(merged,failed,comparison='transfer',**args)
            self.assertEqual(current['latestSuccessful'],merged['latestSuccessful'])
            self.assertEqual(current['latestAttempt']['status'],'failed')


if __name__=='__main__':unittest.main()
