from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from research.data import Match
from scripts.prepare_experiments import prepare, LATEST, REPOSITORY


class PrepareExperimentsTests(unittest.TestCase):
    def inputs(self):
        self.current = Match(date(2026, 10, 25), 2026, 'A', 'B', 1, 0)
        self.rows = [{'date': '2026-10-25', 'season': 2026, 'home': 'A', 'away': 'B',
            'home_goals': 1, 'away_goals': 0, 'home_xg': 1.3, 'away_xg': .7}]
        self.body = json.dumps(self.rows).encode()
        checksum = hashlib.sha256(self.body).hexdigest()
        self.url = f'https://raw.githubusercontent.com/{REPOSITORY}/xg-data-reports/datasets/{checksum}.json'
        self.latest = {'schemaVersion': 1, 'latestAttempt': {'status': 'stale', 'generatedAt': '2026-11-06T01:00:00Z'},
            'lastAccepted': {'status': 'success', 'normalizedMatchesSha256': checksum, 'matchCount': 1,
                'dataLastMatchDate': '2026-10-25', 'datasetUrl': self.url, 'acceptedAt': '2026-11-01T01:00:00Z'}}

    def download(self, year, directory, refresh):
        if year == 2026:
            return [self.current, Match(date(2026, 11, 2), 2026, 'B', 'A', 0, 0)], {'season': year}
        return [Match(date(year, 7, 1) + timedelta(days=i // 2), year,
                      'A' if i % 2 else 'C', 'B', 1, 0) for i in range(380)], {'season': year}

    def fetch(self, url):
        return json.dumps(self.latest).encode() if url == LATEST else self.body

    def run_prepare(self, root, fetch=None):
        return prepare(root / 'csv', root / 'inputs', as_of=date(2026, 11, 6),
                       fetch=fetch or self.fetch, download=self.download)

    def test_checked_today_does_not_hide_old_xg_and_current_missing_results(self):
        self.inputs()
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_prepare(Path(directory))
            freshness = result['sourceFreshness']
            self.assertEqual(freshness['status'], 'stale')
            self.assertEqual(freshness['dataAgeDays'], 12)
            self.assertEqual((freshness['completedMatches'], freshness['matchedXg'], freshness['missingXg']), (2, 1, 1))

    def test_bad_archive_cannot_replace_previous_inputs(self):
        self.inputs()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.run_prepare(root)
            previous = {p.name: p.read_bytes() for p in (root / 'inputs').iterdir()}
            with self.assertRaisesRegex(ValueError, 'checksum'):
                self.run_prepare(root, lambda url: self.fetch(url) if url == LATEST else b'[]')
            self.assertEqual(previous, {p.name: p.read_bytes() for p in (root / 'inputs').iterdir()})
            self.latest['lastAccepted']['datasetUrl'] = 'https://example.invalid/arbitrary.json'
            with self.assertRaisesRegex(ValueError, 'URL'):
                self.run_prepare(root)

    def test_score_conflict_in_an_otherwise_hash_valid_snapshot_is_rejected(self):
        self.inputs(); self.rows[0]['home_goals'] = 4; self.body = json.dumps(self.rows).encode()
        checksum = hashlib.sha256(self.body).hexdigest()
        self.latest['lastAccepted']['normalizedMatchesSha256'] = checksum
        self.latest['lastAccepted']['datasetUrl'] = f'https://raw.githubusercontent.com/{REPOSITORY}/xg-data-reports/datasets/{checksum}.json'
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'conflict'):
                self.run_prepare(Path(directory))
