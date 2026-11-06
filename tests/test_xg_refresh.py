from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.data import Match
from research.performance import Performance
from research.xg_refresh import (SourceUnavailable, encoded, github_source, parse_understat,
                                 record_failed_attempt, refresh, validate_reference_coverage)
from research.xg_summary import REPOSITORY


def fixture(day='2026-10-25', home='Arsenal', away='Everton'):
    return {'date': day, 'home': home, 'away': away, 'home_goals': 2, 'away_goals': 0,
            'home_xg': 1.8, 'away_xg': .4, 'source_match_id': None, 'season': 2026}


def reference(rows):
    return [Performance(Match(date.fromisoformat(r['date']), r['season'], r['home'], r['away'],
                              r['home_goals'], r['away_goals']), None, None, None, None) for r in rows]


def verified(rows):
    return rows, {'provider': 'Understat cache', 'revision': 'a' * 40, 'files': [],
                  'lastDataDate': max((r['date'] for r in rows), default=None)}


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name); self.seed = self.path / 'seed.json'
        self.old = fixture(); self.new = fixture('2026-11-02', 'Liverpool', 'Chelsea')
        self.seed.write_bytes(encoded([self.old])); self.state = self.path / 'state'

    def run_refresh(self, rows, refs=None, **options):
        with patch('research.xg_refresh.github_source', return_value=verified(rows)):
            return refresh(self.state, self.seed, reference(refs or [self.old, self.new]),
                           now='2026-11-06T04:00:00Z', direct=False, **options)

    def test_fetching_unchanged_data_does_not_make_it_fresh(self):
        first = self.run_refresh([self.old])
        before = (self.state / 'success.json').read_bytes()
        with patch('research.xg_refresh.github_source', return_value=verified([self.old])):
            second = refresh(self.state, self.seed, reference([self.old, self.new]),
                             now='2026-11-07T04:00:00Z', direct=False)
        self.assertEqual(first['status'], 'stale'); self.assertEqual(second['status'], 'stale')
        self.assertEqual(second['dataLastMatchDate'], '2026-10-25')
        self.assertEqual(second['dataAgeDays'], 13)
        self.assertEqual(second['coverage']['missingXg'], 1)
        self.assertEqual(before, (self.state / 'success.json').read_bytes())
        self.assertEqual(second['lastSuccessfulAcceptedAt'], first['lastSuccessfulAcceptedAt'])

    def test_valid_new_fixture_is_appended_and_snapshot_hash_is_bound(self):
        report = self.run_refresh([self.old, self.new])
        self.assertEqual(report['status'], 'success'); self.assertEqual(report['newMatchesAdded'], 1)
        self.assertEqual(report['coverage']['matchedXg'], 2)
        body = (self.state / 'xg-matches.json').read_bytes()
        self.assertEqual(report['normalizedMatchesSha256'], hashlib.sha256(body).hexdigest())
        rows = json.loads(body); self.assertEqual(rows[0], self.old)

    def test_historical_correction_is_rejected_and_last_success_is_unchanged(self):
        self.run_refresh([self.old])
        before = {name: (self.state / name).read_bytes() for name in ['xg-matches.json', 'success.json']}
        report = self.run_refresh([{**self.old, 'home_xg': 2.4}, self.new])
        self.assertEqual(report['status'], 'failed'); self.assertIn('disagreement', report['error'])
        self.assertEqual(report['newMatchesAdded'], 0)
        for name, body in before.items():
            self.assertEqual((self.state / name).read_bytes(), body)

    def test_wrong_fixture_score_or_duplicate_rows_do_not_enter_snapshot(self):
        self.run_refresh([self.old]); before = (self.state / 'xg-matches.json').read_bytes()
        for rows in [[self.old, {**self.new, 'home_goals': 3}],
                     [self.old, {**self.new, 'date': '2026-11-03'}], [self.old, self.old]]:
            with self.subTest(rows=rows):
                result = self.run_refresh(rows)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual((self.state / 'xg-matches.json').read_bytes(), before)

    def test_all_source_outages_keep_last_success_and_show_failed_attempt(self):
        self.run_refresh([self.old]); before = (self.state / 'success.json').read_bytes()
        with patch('research.xg_refresh.github_source', side_effect=SourceUnavailable('temporarily offline')):
            result = refresh(self.state, self.seed, reference([self.old, self.new]),
                             now='2026-11-06T05:00:00Z', direct=False)
        self.assertEqual(result['status'], 'failed'); self.assertEqual(result['dataStatus'], 'stale')
        self.assertTrue(result['lastSuccessfulAvailable'])
        self.assertEqual(before, (self.state / 'success.json').read_bytes())

    def test_checksum_tampering_cannot_be_promoted_or_trusted_by_failure_report(self):
        self.run_refresh([self.old]); (self.state / 'xg-matches.json').write_bytes(encoded([self.new]))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.run_refresh([self.old, self.new])
        report = record_failed_attempt(self.state, 'invalid local snapshot', '2026-11-06T04:00:00Z')
        self.assertFalse(report['lastSuccessfulAvailable']); self.assertIsNone(report['normalizedMatchesSha256'])

    def test_direct_summary_ignores_future_fixtures_and_rejects_maintenance(self):
        with self.assertRaises(SourceUnavailable):
            parse_understat(b'<!DOCTYPE html><html>Maintenance</html>', 2026)
        packet = {'dates': [{'id': '123', 'isResult': True, 'datetime': '2026-10-25 16:00:00',
                             'h': {'title': 'Arsenal'}, 'a': {'title': 'Everton'},
                             'goals': {'h': '2', 'a': '0'}, 'xG': {'h': '1.8', 'a': '.4'}},
                            {'id': '124', 'isResult': False}]}
        result = parse_understat(json.dumps(packet).encode(), 2026)
        self.assertEqual(len(result), 1); self.assertEqual(result[0]['source_match_id'], 123)

    def test_github_head_resolved_once_and_raw_bytes_verified_against_tree(self):
        raw = (b'league,season,season_id,game_id,date,home_team,away_team,home_goals,away_goals,home_xg,away_xg,is_result,has_data,url\n'
               b'ENG-Premier League,2627,2026,123,2026-10-25 16:00:00,Arsenal,Everton,2,0,1.8,.4,True,True,https://understat.com/match/123\n')
        revision = 'a' * 40; path = 'data/raw/understat/eng/eng_2627.csv'
        blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        calls = []; fetched = []
        def api(url):
            calls.append(url)
            return ({'sha': revision, 'commit': {'committer': {'date': '2026-10-25T22:00:00Z'}}}
                    if url.endswith('/commits/HEAD') else
                    {'truncated': False, 'tree': [{'type': 'blob', 'path': path, 'sha': blob, 'size': len(raw)}]})
        def fetch(url):
            fetched.append(url); return raw
        with patch('research.xg_refresh.EXPECTED_FILES', {'eng_2627.csv'}):
            rows, manifest = github_source(REPOSITORY, 'summary', api=api, fetch=fetch)
            self.assertEqual(len(rows), 1)
            self.assertEqual(sum(url.endswith('/commits/HEAD') for url in calls), 1)
            self.assertIn('/' + revision + '/', fetched[0]); self.assertEqual(manifest['files'][0]['gitBlobSha1'], blob)
            with self.assertRaisesRegex(ValueError, 'Git blob'):
                github_source(REPOSITORY, 'summary', api=api, fetch=lambda url: raw.replace(b'1.8', b'2.8'))

    def test_reference_failure_is_visible_while_accepted_snapshot_survives(self):
        self.run_refresh([self.old]); before = (self.state / 'xg-matches.json').read_bytes()
        report = record_failed_attempt(self.state, 'result provider unavailable', '2026-11-06T05:00:00Z')
        self.assertTrue(report['lastSuccessfulAvailable']); self.assertTrue(report['referenceUnavailable'])
        self.assertEqual(report['status'], 'failed'); self.assertEqual(report['dataStatus'], 'stale')
        self.assertEqual(before, (self.state / 'xg-matches.json').read_bytes())

    def test_truncated_reference_cannot_make_missing_xg_appear_current(self):
        with patch('research.xg_refresh.github_source', return_value=verified([self.old])):
            refresh(self.state, self.seed, reference([self.old, self.new]), now='2026-11-06T04:00:00Z',
                    direct=False, reference_sources=[{'season': 2026, 'rows': 2}])
        before = (self.state / 'xg-matches.json').read_bytes()
        report = self.run_refresh([self.old], refs=[self.old])
        self.assertEqual(report['status'], 'failed'); self.assertIn('regressed', report['error'])
        self.assertEqual(report['dataStatus'], 'stale'); self.assertIsNone(report['coverage'])
        self.assertEqual(before, (self.state / 'xg-matches.json').read_bytes())
        with self.assertRaisesRegex(ValueError, '380'):
            validate_reference_coverage(reference([self.old]), 2026, closed_seasons=True)


if __name__ == '__main__':
    unittest.main()
