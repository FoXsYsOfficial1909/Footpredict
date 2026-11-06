from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from research.nightly import sha
from scripts.publish_corners import merge_latest, publish


def attempt():
    return {'schemaVersion': 1, 'protocol': 'corner-candidate-1', 'status': 'success',
            'generatedAt': '2026-11-06T17:00:00Z', 'asOf': '2026-11-06',
            'trainingCutoffExclusive': '2026-11-05', 'latestTrainingMatch': '2026-11-02',
            'modelSha256': 'a' * 64, 'modelVersion': 'corners-test',
            'evaluation': {'commonMatches': 120}, 'promotedToDefault': False,
            'prospectiveValidationRequired': True}


@contextmanager
def cwd(path):
    before = Path.cwd(); os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)


class PublishCornersTests(unittest.TestCase):
    def merge(self, previous, report):
        return merge_latest(previous, report, repository='owner/repo', run_id='42', run_attempt='1', commit='b' * 40)

    def test_failed_attempt_retains_the_last_successful_corner_weights(self):
        original = self.merge(None, attempt())
        failed = {**attempt(), 'status': 'failed', 'generatedAt': '2026-11-07T17:00:00Z'}
        result = self.merge(original, failed)
        self.assertEqual(result['latestSuccessful'], original['latestSuccessful'])
        self.assertEqual(result['latestAttempt']['status'], 'failed')
        self.assertIn('/corner-reports/models/' + 'a' * 64, result['latestSuccessful']['modelUrl'])
        with self.assertRaises(ValueError):
            self.merge(result, attempt())

    def test_success_needs_a_real_data_fence_and_common_monitoring(self):
        for invalid in [{**attempt(), 'latestTrainingMatch': '2026-11-05'},
                        {**attempt(), 'evaluation': {'commonMatches': 0}},
                        {**attempt(), 'protocol': 'nightly-fixed-settings-1'},
                        {**attempt(), 'modelSha256': '../escape'}]:
            with self.assertRaises(ValueError):
                self.merge(None, invalid)

    def test_actual_git_publication_preserves_canonical_bytes_and_immutable_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); repo = root / 'repo'; repo.mkdir()
            output = root / 'output'; output.mkdir(); remote = root / 'remote.git'
            def git(*args):
                return subprocess.run(['git', *args], cwd=repo, check=True,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
            git('init', '--bare', str(remote)); git('init'); git('remote', 'add', 'origin', str(remote))
            git('-c', 'user.name=test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'initial')
            model = {'model_version': 'corners-test', 'target': 'corners_total_regulation', 'as_of': '2026-11-05', 'value': 6.2}
            report = {**attempt(), 'modelSha256': sha(model)}
            (output / 'model.json').write_text(json.dumps(model))
            (output / 'attempt.json').write_text(json.dumps(report))
            with cwd(repo):
                publish(output, 'owner/repo', '42', '1', 'b' * 40)
                git('fetch', 'origin', 'corner-reports')
                body = git('show', 'FETCH_HEAD:models/' + report['modelSha256'] + '.json')
                self.assertEqual(hashlib.sha256(body).hexdigest(), report['modelSha256'])
                self.assertEqual(json.loads(body), model)
                report['evaluation']['commonMatches'] = 121
                (output / 'attempt.json').write_text(json.dumps(report))
                with self.assertRaisesRegex(ValueError, 'Immutable'):
                    publish(output, 'owner/repo', '42', '1', 'b' * 40)
                model['value'] = 9.2; (output / 'model.json').write_text(json.dumps(model))
                with self.assertRaisesRegex(ValueError, 'model'):
                    publish(output, 'owner/repo', '43', '1', 'b' * 40)
