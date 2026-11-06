from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from research.nightly import sha
from scripts.publish_experiments import merge_latest, publish, read_state, restore


def success():
    return {'schemaVersion': 1, 'protocol': 'automatic-experiments-1', 'status': 'success',
        'generatedAt': '2026-11-06T11:00:00Z', 'evaluatedAt': '2026-11-06T11:00:00Z',
        'inputFingerprint': 'a' * 64, 'dataSha256': 'b' * 64, 'planSha256': 'c' * 64,
        'codeSha256': 'd' * 64, 'promotedToDefault': False, 'prospectiveValidationRequired': True,
        'cacheStats': {'hits': 0, 'misses': 10, 'corrupt': 0}, 'selection': {'chosen': None}}


def state_for(report):
    return {'schemaVersion': 1, 'inputFingerprint': report['inputFingerprint'],
            'reportSha256': sha(report), 'report': report}


@contextmanager
def cwd(directory):
    previous = Path.cwd(); os.chdir(directory)
    try:
        yield
    finally:
        os.chdir(previous)


class ExperimentPublicationTests(unittest.TestCase):
    def merge(self, previous, report, state):
        return merge_latest(previous, report, state, repository='owner/repo',
            run_id='123', run_attempt='1', commit='f' * 40)

    def test_failed_and_unchanged_checks_preserve_success_and_scientific_timestamp(self):
        report = success(); state = state_for(report)
        previous = self.merge(None, report, state); saved = copy.deepcopy(previous)
        unchanged = {**report, 'status': 'unchanged', 'generatedAt': '2026-11-07T11:00:00Z',
                     'cacheStats': {'hits': 0, 'misses': 0, 'corrupt': 0}}
        checked = self.merge(previous, unchanged, state)
        self.assertEqual(checked['latestSuccessful'], previous['latestSuccessful'])
        self.assertEqual(checked['latestAttempt']['evaluatedAt'], report['evaluatedAt'])
        failed = {'schemaVersion': 1, 'protocol': report['protocol'], 'status': 'failed',
            'generatedAt': '2026-11-08T11:00:00Z', 'promotedToDefault': False,
            'prospectiveValidationRequired': True, 'inputFingerprint': None}
        after = self.merge(checked, failed, state)
        self.assertEqual(after['latestSuccessful'], previous['latestSuccessful'])
        self.assertEqual(after['latestAttempt']['status'], 'failed')
        self.assertEqual(previous, saved)
        with self.assertRaisesRegex(ValueError, 'newer'):
            self.merge(after, report, state)
        with self.assertRaisesRegex(ValueError, 'timestamp'):
            self.merge(previous, {**unchanged, 'evaluatedAt': unchanged['generatedAt']}, state)

    def test_state_digest_identity_and_successful_report_must_agree(self):
        report = success(); state = state_for(report)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root / 'last-success.json'
            path.write_text(json.dumps(state))
            self.assertEqual(read_state(root), state)
            state['report']['selection']['chosen'] = 'tampered'
            path.write_text(json.dumps(state))
            with self.assertRaisesRegex(ValueError, 'state'):
                read_state(root)
        with self.assertRaisesRegex(ValueError, 'matching'):
            self.merge(None, report, None)
        with self.assertRaisesRegex(ValueError, 'differs'):
            self.merge(None, {**report, 'selection': {'chosen': 'changed'}}, state_for(success()))

    def test_bare_git_roundtrip_keeps_archive_and_old_state_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); repository = root / 'repo'; repository.mkdir()
            output = root / 'output'; output.mkdir(); state_dir = root / 'state'; state_dir.mkdir()
            remote = root / 'origin.git'
            def git(*args):
                return subprocess.run(['git', *args], cwd=repository, check=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            git('init', '--bare', str(remote)); git('init')
            git('remote', 'add', 'origin', str(remote))
            git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid',
                'commit', '--allow-empty', '-m', 'initial')
            report = success(); state = state_for(report)
            (output / 'report.json').write_text(json.dumps(report))
            (state_dir / 'last-success.json').write_text(json.dumps(state))
            with cwd(repository):
                self.assertFalse(restore(root / 'restored'))
                publish(output, state_dir, 'owner/repo', '123', '1', 'f' * 40)
                self.assertTrue(restore(root / 'restored'))
                self.assertEqual((root / 'restored/last-success.json').read_bytes(), (state_dir / 'last-success.json').read_bytes())
                checksum = sha(report)
                original = git('show', 'FETCH_HEAD:reports/' + checksum + '.json').stdout
                self.assertEqual(sha(json.loads(original)), checksum)
                failed = {**report, 'status': 'failed', 'generatedAt': '2026-11-07T11:00:00Z'}
                (output / 'report.json').write_text(json.dumps(failed))
                publish(output, state_dir, 'owner/repo', '124', '1', 'f' * 40)
                self.assertTrue(restore(root / 'failed-restored'))
                self.assertEqual((root / 'failed-restored/last-success.json').read_bytes(), (state_dir / 'last-success.json').read_bytes())
                self.assertEqual(git('show', 'FETCH_HEAD:reports/' + checksum + '.json').stdout, original)
                changed = {**failed, 'errorType': 'different'}
                (output / 'report.json').write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, 'Immutable'):
                    publish(output, state_dir, 'owner/repo', '124', '1', 'f' * 40)

    def test_first_failed_attempt_has_no_invented_success_or_state(self):
        report = {**success(), 'status': 'failed'}
        merged = self.merge(None, report, None)
        self.assertIsNone(merged['latestSuccessful'])


if __name__ == '__main__':
    unittest.main()
