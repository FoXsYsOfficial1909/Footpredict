from contextlib import contextmanager
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.publish_xg_data import merge_latest, publish, read_state, restore


def sample_state(root):
    root.mkdir(parents=True, exist_ok=True)
    rows = [{'date': (date(2026, 7, 1) + timedelta(days=i)).isoformat(), 'season': 2026,
             'home': 'A', 'away': 'B', 'home_goals': 1, 'away_goals': 0,
             'home_xg': 1.2, 'away_xg': .7, 'source_match_id': i + 1} for i in range(100)]
    body = json.dumps(rows).encode()
    success = {'schemaVersion': 1, 'status': 'success',
        'normalizedMatchesSha256': hashlib.sha256(body).hexdigest(),
        'acceptedAt': '2026-11-01T00:00:00Z', 'dataLastMatchDate': rows[-1]['date'],
        'matchCount': len(rows), 'sourceManifest': {}}
    report = {'schemaVersion': 1, 'generatedAt': '2026-11-06T00:00:00Z',
        'status': 'stale', 'promotedToDefault': False,
        'normalizedMatchesSha256': success['normalizedMatchesSha256'],
        'dataLastMatchDate': success['dataLastMatchDate']}
    (root / 'xg-matches.json').write_bytes(body)
    (root / 'success.json').write_text(json.dumps(success))
    (root / 'report.json').write_text(json.dumps(report))
    return success, report


@contextmanager
def cwd(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class PublishXgTests(unittest.TestCase):
    def merge(self, previous, report, success):
        return merge_latest(previous, report, success, repository='owner/repo',
                            run_id='12', run_attempt='1', commit='a' * 40)

    def test_failed_attempt_keeps_identical_accepted_dataset_and_its_original_date(self):
        with tempfile.TemporaryDirectory() as temp:
            success, report = sample_state(Path(temp))
            previous = self.merge(None, report, success)
            failed = {**report, 'status': 'failed', 'generatedAt': '2026-11-07T00:00:00Z'}
            current = self.merge(previous, failed, success)
            self.assertEqual(current['lastAccepted'], previous['lastAccepted'])
            self.assertEqual(current['latestAttempt']['status'], 'failed')
            self.assertEqual(current['lastAccepted']['acceptedAt'], '2026-11-01T00:00:00Z')
            self.assertIn('/datasets/' + success['normalizedMatchesSha256'], current['lastAccepted']['datasetUrl'])
            self.assertEqual(previous['latestAttempt']['status'], 'stale')
            with self.assertRaisesRegex(ValueError, 'newer'):
                self.merge(current, report, success)
            with self.assertRaisesRegex(ValueError, 'regress'):
                self.merge(current, {**failed, 'generatedAt': '2026-11-08T00:00:00Z'},
                           {**success, 'matchCount': 99})

    def test_corrupt_data_or_report_digest_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); success, report = sample_state(root)
            read_state(root)
            with self.assertRaises(ValueError):
                self.merge(None, {**report, 'normalizedMatchesSha256': '0' * 64}, success)
            (root / 'xg-matches.json').write_bytes((root / 'xg-matches.json').read_bytes() + b'\n')
            with self.assertRaisesRegex(ValueError, 'snapshot'):
                read_state(root)

    def test_local_git_roundtrip_restores_old_state_and_rejects_attempt_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); repository = root / 'repo'; repository.mkdir()
            remote = root / 'remote.git'; state = root / 'state'; sample_state(state)
            def git(*args):
                return subprocess.run(['git', *args], cwd=repository, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            git('init', '--bare', str(remote)); git('init')
            git('remote', 'add', 'origin', str(remote))
            git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                'commit', '--allow-empty', '-m', 'initial')
            with cwd(repository):
                self.assertFalse(restore(root / 'restored'))
                publish(state, 'owner/repo', '12', '1', 'a' * 40)
                self.assertTrue(restore(root / 'restored'))
                for name in ('xg-matches.json', 'success.json'):
                    self.assertEqual((state / name).read_bytes(), (root / 'restored' / name).read_bytes())
                report = json.loads((state / 'report.json').read_text())
                report['status'] = 'failed'
                (state / 'report.json').write_text(json.dumps(report))
                with self.assertRaisesRegex(ValueError, 'attempt differs'):
                    publish(state, 'owner/repo', '12', '1', 'a' * 40)
                self.assertTrue(restore(root / 'unchanged'))
                self.assertEqual((state / 'xg-matches.json').read_bytes(), (root / 'unchanged/xg-matches.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
