import copy
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from research.elc_future import (DIRECTORY, ORIGINAL_SHA, PROTOCOL, REGISTERED_SHA, TEAM_MAP,
    build_packet, freeze, settle, settle_files, shot_forecast, verify_files)
from research.hundred import DIRECTORY as ORIGINAL, instant, verify_files as verify_original
from scripts.publish_elc_comparison import compact, merge_latest


class ELCFutureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packet = verify_files(); cls.original = verify_original()

    def source(self, checked=None):
        return {'schemaVersion': 1, 'protocol': 'hundred-future-1', 'packetSha256': ORIGINAL_SHA,
            'checkedAt': checked or self.packet['registeredAt'], 'status': 'active', 'forecastsChanged': False,
            'fixtures': [{'fixtureId': f['fixtureId'], 'status': 'pending', 'outcome': None, 'score': None, 'reason': None}
                         for f in self.original['fixtures']]}

    def finished(self):
        fixture = self.packet['fixtures'][0]
        checked = instant(fixture['kickoff']) + timedelta(hours=5)
        source = self.source(checked.isoformat())
        row = next(r for r in source['fixtures'] if r['fixtureId'] == fixture['fixtureId'])
        row.update(status='settled', outcome=2, score={'home': 0, 'away': 1})
        return source, row, checked

    def test_all_36_are_before_kickoff_and_exactly_reproduced_without_changing_the_hundred(self):
        p = self.packet
        self.assertEqual(p['forecastCount'], 36)
        self.assertEqual(p['mappingProof']['verifiedHistoricalFixtures'], 180)
        self.assertTrue(p['candidateEqualsRawShots'])
        self.assertTrue(all(f['candidate'] == f['shots'] for f in p['fixtures']))
        self.assertEqual({f['fixtureId'] for f in p['fixtures']},
                         {f['fixtureId'] for f in self.original['fixtures'] if f['league'] == 'ELC'})
        self.assertTrue(all(instant(f['kickoff']) - instant(p['registeredAt']) >= timedelta(hours=24) for f in p['fixtures']))
        self.assertEqual(hashlib.sha256((ORIGINAL/'packet.json').read_bytes()).hexdigest(), ORIGINAL_SHA)

    def test_packet_cannot_be_rewritten_and_logical_corruption_is_also_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'cohort'; shutil.copytree(DIRECTORY, root)
            before = (root/'packet.json').read_bytes()
            with self.assertRaisesRegex(ValueError, 'overwrite'):freeze(root)
            self.assertEqual((root/'packet.json').read_bytes(), before)
            changed = json.loads(before); changed['fixtures'][0]['candidate'] = [.1, .8, .1]
            raw = json.dumps(changed).encode(); (root/'packet.json').write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest(); (root/'packet.sha256').write_text(digest)
            with self.assertRaises(ValueError):verify_files(root)
            with self.assertRaisesRegex(ValueError, 'reproduced'):verify_files(root, expected_sha=digest)
        def rounding(model, home, away):
            p = shot_forecast(model, home, away)
            return [p[0]+1e-13, p[1]-1e-13, p[2]]
        with patch('research.elc_future.shot_forecast', side_effect=rounding):
            self.assertEqual(verify_files()['forecastCount'], 36)

    def test_swapped_team_names_and_late_registration_cannot_silently_pass(self):
        mapping = json.loads(TEAM_MAP.read_text())
        mapping['teams'][0]['csv'], mapping['teams'][1]['csv'] = mapping['teams'][1]['csv'], mapping['teams'][0]['csv']
        with self.assertRaisesRegex(ValueError, 'historical fixture'):
            build_packet(self.packet['registeredAt'], mapping=mapping)
        first = self.packet['fixtures'][0]
        late = instant(first['kickoff']) - timedelta(hours=23)
        selected = build_packet(late)
        self.assertIn(first['fixtureId'], {f['fixtureId'] for f in selected['skipped']})
        self.assertEqual(len(selected['fixtures']) + len(selected['skipped']), 36)
        self.assertTrue(all(instant(f['kickoff']) - late >= timedelta(hours=24) for f in selected['fixtures']))

    def test_pending_scores_are_null_and_settled_metrics_share_the_same_denominator(self):
        source = self.source(); p = self.packet
        empty = settle(p, source, instant(source['checkedAt']))
        self.assertEqual(empty['counts']['pending'], 36)
        self.assertTrue(all(m['n'] == 0 and m['accuracy'] is None and m['brier'] is None for m in empty['metrics'].values()))
        source, row, checked = self.finished(); original = copy.deepcopy(p)
        result = settle(p, source, checked)
        self.assertEqual({m['n'] for m in result['metrics'].values()}, {1})
        self.assertEqual(result['counts']['settled'], 1)
        self.assertEqual(p, original)
        row.update(score={'home': 3, 'away': 0}, outcome=0)
        corrected = settle(p, source, checked + timedelta(hours=1))
        self.assertNotEqual(corrected['metrics']['candidate']['logLoss'], result['metrics']['candidate']['logLoss'])
        self.assertEqual(p, original)

    def test_wrong_cohort_missing_rows_early_scores_and_impossible_results_are_rejected(self):
        source, row, checked = self.finished()
        with self.assertRaises(ValueError):settle(self.packet, {**source, 'packetSha256': 'other'}, checked)
        with self.assertRaises(ValueError):settle(self.packet, {**source, 'fixtures': source['fixtures'][:-1]}, checked)
        with self.assertRaises(ValueError):settle(self.packet, source, checked - timedelta(minutes=10))
        with self.assertRaises(ValueError):settle(self.packet, {**source, 'checkedAt': (checked-timedelta(hours=2)).isoformat()}, checked)
        row['outcome'] = 0
        with self.assertRaises(ValueError):settle(self.packet, source, checked)
        row.update(status='review', outcome=None, score=None, reason='fixture_identity_or_kickoff_changed')
        excluded = settle(self.packet, source, checked)
        self.assertEqual(excluded['counts']['review'], 1)
        self.assertEqual(excluded['metrics']['candidate']['n'], 0)
        with self.assertRaises(ValueError):settle(self.packet, {**self.source(), 'finalized': True}, checked)

    def test_finalized_source_can_be_reused_without_fabricating_a_new_source_check(self):
        checked = max(instant(f['kickoff']) for f in self.original['fixtures']) + timedelta(days=15)
        source = self.source(checked.isoformat()); source.update(status='complete', finalized=True)
        for row in source['fixtures']:row.update(status='settled', score={'home': 0, 'away': 0}, outcome=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); prior = root/'previous.json'; prior.write_text(json.dumps(source))
            with patch('research.elc_future.datetime') as clock, patch('sys.stdout', new_callable=io.StringIO):
                clock.now.return_value = checked + timedelta(days=1)
                settle_files(root/'missing.json', root/'out', previous_source=prior)
            result = json.loads((root/'out/report.json').read_text())
            self.assertEqual(result['checkedAt'], source['checkedAt'])
            self.assertTrue(result['finalized']); self.assertEqual(result['additionalProviderRequests'], 0)
            self.assertEqual(result['metrics']['candidate']['n'], 36)

    def test_failure_keeps_prior_success_and_cannot_replace_another_cohort_archive(self):
        source = self.source(); report = settle(self.packet, source, instant(source['checkedAt']))
        args = {'repository': 'FoXsYsOfficial1909/Footpredict', 'run_id': '123', 'attempt': '1', 'commit': 'b'*40}
        previous = merge_latest(None, report, **args)
        failed = {'schemaVersion': 1, 'protocol': PROTOCOL, 'packetSha256': REGISTERED_SHA,
            'sourceCohortSha256': ORIGINAL_SHA, 'status': 'failed', 'forecastsChanged': False,
            'checkedAt': (instant(source['checkedAt'])+timedelta(hours=1)).isoformat()}
        current = merge_latest(previous, failed, **args)
        self.assertEqual(current['latestSuccessful'], previous['latestSuccessful'])
        recovered = merge_latest(current, report, **args)
        self.assertEqual(recovered['latestSuccessful']['checkedAt'], report['checkedAt'])
        with self.assertRaises(ValueError):compact({**report, 'packetSha256': ORIGINAL_SHA})
        with self.assertRaises(ValueError):merge_latest({**previous, 'packetSha256': ORIGINAL_SHA}, report, **args)


if __name__ == '__main__':unittest.main()
