from datetime import date, datetime, timezone, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
import io

from research.data import Match
from research.performance import Performance
from research.workload_data import (COMPETITIONS, INCEPTION, download_snapshots,
    load_snapshots, parse_scoreboard, scoreboard_url, verify_reference)


def event(kind='STATUS_FULL_TIME', *, identifier='100', clock=5400, period=2, completed=True):
    return {'id': identifier, 'date': '2023-08-11T19:00Z', 'competitions': [{
        'id': identifier, 'status': {'type': {'name': kind, 'state': 'post' if completed else 'pre', 'completed': completed},
                                   'period': period, 'clock': clock},
        'competitors': [{'homeAway': 'home', 'team': {'displayName': 'Burnley'}, 'score': '0'},
                        {'homeAway': 'away', 'team': {'displayName': 'Manchester City'}, 'score': '3'}]}]}


def body(events, slug='eng.1'):
    return json.dumps({'leagues': [{'slug': slug}], 'events': events}).encode()


class WorkloadDataTests(unittest.TestCase):
    def test_completed_event_has_real_identity_kickoff_and_known_duration(self):
        rows, audit = parse_scoreboard(body([event()]), 'eng.1', 2023)
        self.assertEqual(rows[0]['away'], 'Man City')
        self.assertEqual(rows[0]['kickoff'], '2023-08-11T19:00:00Z')
        self.assertEqual(rows[0]['source_id'], 'ESPN:100')
        self.assertEqual(rows[0]['minutes'], 90)
        self.assertFalse(audit['coverageIndependentlyVerified'])

    def test_extra_time_requires_explicit_evidence_and_penalties_are_unknown(self):
        aft, _ = parse_scoreboard(body([event('STATUS_FINAL_AET', clock=7200, period=4)]), 'eng.1', 2023)
        pens, _ = parse_scoreboard(body([event('STATUS_FINAL_PEN', clock=5820, period=5)]), 'eng.1', 2023)
        self.assertEqual(aft[0]['minutes'], 120)
        self.assertIsNone(pens[0]['minutes'])
        self.assertFalse(pens[0]['minutes_known'])

    def test_scheduled_canceled_and_unrecognized_results_are_not_played_games(self):
        rows, audit = parse_scoreboard(body([event('STATUS_SCHEDULED', completed=False),
                                             event('STATUS_CANCELED', identifier='101', completed=False),
                                             event('STATUS_FORFEIT', identifier='102')]), 'eng.1', 2023)
        self.assertEqual(rows, [])
        self.assertEqual(len(audit['excludedEvents']), 3)

    def test_duplicate_wrong_league_wrong_year_and_limit_fail(self):
        for data, league, year in [(body([event(), event()]), 'eng.1', 2023),
                                   (body([event()]), 'eng.fa', 2023),
                                   (body([event()]), 'eng.1', 2024),
                                   (body([event(identifier=str(i)) for i in range(1000)]), 'eng.1', 2023)]:
            with self.assertRaises(ValueError):
                parse_scoreboard(data, league, year)

    def test_reference_scores_are_checked_without_replacing_labels(self):
        rows, _ = parse_scoreboard(body([event()]), 'eng.1', 2023)
        original = Performance(Match(date(2023, 8, 11), 2023, 'Burnley', 'Man City', 0, 3), 1, 2, 1, 1)
        report = verify_reference(rows, [original])
        self.assertEqual(report['matched'], 1)
        self.assertEqual(report['conflicts'], [])
        rows[0]['away_goals'] = 1
        self.assertEqual(len(verify_reference(rows, [original])['conflicts']), 1)
        self.assertEqual(original.match.away_goals, 3)

    def test_snapshot_hash_is_enforced_and_coverage_defaults_to_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            raw = body([event()]); name = 'eng.1-2023.json.gz'
            Path(folder, name).write_bytes(gzip.compress(raw))
            entry = {'competition': 'eng.1', 'calendarYear': 2023, 'sourceUrl': scoreboard_url('eng.1', 2023),
                     'ok': True, 'file': name, 'rawSha256': hashlib.sha256(raw).hexdigest(), 'rawBytes': len(raw), 'events': 1}
            rows, manifest = load_snapshots(folder, {'queries': [entry]})
            self.assertEqual(len(rows), 1)
            self.assertFalse(manifest['requiredCompetitionsVerified'])
            self.assertFalse(manifest['scopes'][0]['complete'])
            entry['rawSha256'] = '0' * 64
            with self.assertRaises(ValueError):
                load_snapshots(folder, {'queries': [entry]})

    def test_download_is_explicit_bounded_and_does_not_upgrade_coverage(self):
        def response(request, timeout):
            self.assertEqual(timeout, 45)
            slug = request.full_url.split('/soccer/', 1)[1].split('/', 1)[0]
            return io.BytesIO(body([event(identifier=str(100 + COMPETITIONS.index(slug)))], slug=slug))
        with tempfile.TemporaryDirectory() as folder, patch('research.workload_data.urllib.request.urlopen', side_effect=response) as network:
            index = download_snapshots(folder, [2023, 2023], now=datetime(2026, 11, 6, tzinfo=timezone.utc))
            expected = sum(2023 >= INCEPTION.get(comp, 2000) for comp in COMPETITIONS)
            self.assertEqual(len(index['queries']), expected)
            self.assertEqual(network.call_count, expected)
            for entry in index['queries']:
                self.assertTrue(entry['ok'])
                raw = gzip.decompress(Path(folder, entry['file']).read_bytes())
                self.assertEqual(hashlib.sha256(raw).hexdigest(), entry['rawSha256'])
            rows, manifest = load_snapshots(folder, index)
            self.assertEqual(len(rows), expected)
            self.assertFalse(manifest['allCompetitionsComplete'])
            self.assertTrue(all(not scope['complete'] for scope in manifest['scopes']))

    def test_failed_download_preserves_previous_raw_and_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); previous = gzip.compress(body([event()]))
            (root / 'eng.1-2023.json.gz').write_bytes(previous)
            (root / 'eng.1-2023.meta.json').write_text('old metadata')
            with patch('research.workload_data.urllib.request.urlopen', side_effect=URLError('offline')):
                index = download_snapshots(root, [2023], now=datetime(2026, 11, 6, tzinfo=timezone.utc))
            self.assertTrue(all(not row['ok'] for row in index['queries']))
            self.assertTrue(index['queries'][0]['previouslyStoredFilePreserved'])
            self.assertEqual((root / 'eng.1-2023.json.gz').read_bytes(), previous)
            self.assertEqual((root / 'eng.1-2023.meta.json').read_text(), 'old metadata')

    def test_future_year_uses_utc_date_and_duplicate_source_queries_are_rejected(self):
        # Local January 2027 can still be December 2026 in UTC.
        now = datetime(2027, 1, 1, 1, tzinfo=timezone(timedelta(hours=14)))
        with tempfile.TemporaryDirectory() as folder, patch('research.workload_data.urllib.request.urlopen') as network:
            with self.assertRaisesRegex(ValueError, 'download years'):
                download_snapshots(folder, [2027], now=now)
            network.assert_not_called()
            raw = body([event()]); name = 'eng.1-2023.json.gz'
            Path(folder, name).write_bytes(gzip.compress(raw))
            entry = {'competition': 'eng.1', 'calendarYear': 2023, 'sourceUrl': scoreboard_url('eng.1', 2023),
                     'ok': True, 'file': name, 'rawSha256': hashlib.sha256(raw).hexdigest(), 'rawBytes': len(raw), 'events': 1}
            with self.assertRaisesRegex(ValueError, 'Duplicate workload snapshot query'):
                load_snapshots(folder, {'queries': [entry, entry]})


if __name__ == '__main__':
    unittest.main()
