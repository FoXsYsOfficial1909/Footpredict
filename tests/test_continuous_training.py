import copy
from datetime import date, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from research.continuous_training import (ROOT, fetch_previous, fingerprint, previous_success, run)


class ContinuousTrainingTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 11, 15)
        self.records = [['2026-11-12', 2026, 'A', 'B', 2, 1],
                        ['2026-11-13', 2026, 'B', 'C', 0, 0]]
        self.configuration = 'c' * 64

    def previous(self, league='PL', method='goals', value=None):
        return {'schemaVersion': 1, 'latestSuccessful': {
            'status': 'success', 'league': league, 'division': 'E0' if league == 'PL' else 'E1',
            'method': method, 'modelSha256': 'a' * 64, 'promotedToDefault': False,
            'generatedAt': '2026-11-15T00:30:00Z',
            'trainingFingerprint': value or fingerprint(league, self.day, self.records, self.configuration)}}

    @staticmethod
    def fitted(directory, output, day, **kwargs):
        path = Path(output); path.mkdir(parents=True, exist_ok=True)
        report = {'status': 'success', 'modelSha256': 'b' * 64, 'generatedAt': '2026-11-15T01:00:00Z'}
        for name in ('report.json', 'attempt.json'):
            (path / name).write_text(json.dumps(report))
        (path / 'model.json').write_text('{"coefficient":1}')

    def test_unchanged_check_does_not_fit_rewrite_dates_or_leave_stale_attempt(self):
        previous = self.previous(); before = copy.deepcopy(previous)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'; output.mkdir()
            (output / 'attempt.json').write_text('{"status":"success"}')
            (output / 'model.json').write_text('{"stale":true}')
            with patch('research.continuous_training.load_sources', return_value=self.records), \
                 patch('research.continuous_training.configuration_hash', return_value=self.configuration), \
                 patch('research.continuous_training.fetch_previous', return_value=previous), \
                 patch('research.nightly.run') as fit:
                result = run('PL', Path(directory) / 'data', output, self.day)
            fit.assert_not_called()
            self.assertEqual(result[0]['status'], 'unchanged')
            self.assertEqual(result[0]['modelSha256'], 'a' * 64)
            self.assertFalse((output / 'attempt.json').exists())
            self.assertFalse((output / 'model.json').exists())
            self.assertEqual(json.loads((output / 'check.json').read_text()), result[0])
            self.assertEqual(previous, before)

    def test_changed_records_code_or_daily_cutoff_trigger_a_fit_with_fixed_model_bytes(self):
        previous = self.previous()
        corrected = copy.deepcopy(self.records); corrected[0][4] = 3
        cases = [(corrected, self.configuration, self.day),
                 (self.records, 'd' * 64, self.day),
                 (self.records, self.configuration, date(2026, 11, 16))]
        for records, configuration, day in cases:
            with self.subTest(records=records, configuration=configuration, day=day), \
                 tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'output'
                with patch('research.continuous_training.load_sources', return_value=records), \
                     patch('research.continuous_training.configuration_hash', return_value=configuration), \
                     patch('research.continuous_training.fetch_previous', return_value=previous), \
                     patch('research.nightly.run', side_effect=self.fitted) as fit:
                    result = run('PL', Path(directory) / 'data', output, day)
                fit.assert_called_once_with(Path(directory) / 'data', output, day, offline=True)
                self.assertEqual(result[0]['status'], 'trained')
                attempt = json.loads((output / 'attempt.json').read_text())
                self.assertEqual(attempt['trainingFingerprint'], result[0]['trainingFingerprint'])
                self.assertEqual(attempt['generatedAt'], '2026-11-15T01:00:00Z')
                self.assertEqual((output / 'model.json').read_text(), '{"coefficient":1}')

    def test_future_and_immediately_previous_day_cannot_change_input_fingerprint(self):
        first = fingerprint('PL', self.day, self.records, self.configuration)
        future = [['2026-11-14', 2026, 'A', 'B', 20, 0],
                  ['2026-11-15', 2026, 'A', 'C', 0, 20],
                  ['2026-11-20', 2026, 'B', 'C', 10, 10]]
        self.assertEqual(first, fingerprint('PL', self.day, future + self.records[::-1], self.configuration))
        shots = [r + [10, 8, 3, 2, []] for r in self.records]
        changed = copy.deepcopy(shots); changed[0][8] = 4
        self.assertNotEqual(fingerprint('ELC', self.day, shots, self.configuration),
                            fingerprint('ELC', self.day, changed, self.configuration))
        with self.assertRaises(ValueError):
            fingerprint('PL', self.day, self.records + self.records, self.configuration)

    def test_championship_skips_only_when_both_scoped_candidates_match(self):
        previous = [self.previous('ELC'), self.previous('ELC', 'shots_on_target')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('research.continuous_training.load_sources', return_value=self.records), \
                 patch('research.continuous_training.configuration_hash', return_value=self.configuration), \
                 patch('research.continuous_training.fetch_previous', side_effect=previous), \
                 patch('research.championship.refresh') as goals, \
                 patch('research.championship_shots.refresh') as shots:
                checks = run('ELC', root / 'data', root / 'goal', self.day, shots_output=root / 'shot')
            goals.assert_not_called(); shots.assert_not_called()
            self.assertEqual([c['status'] for c in checks], ['unchanged', 'unchanged'])
            self.assertEqual([c['method'] for c in checks], ['goals', 'shots_on_target'])
        # One missing method forces a real refresh of the shared source bundle.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('research.continuous_training.load_sources', return_value=self.records), \
                 patch('research.continuous_training.configuration_hash', return_value=self.configuration), \
                 patch('research.continuous_training.fetch_previous', side_effect=[previous[0], None]), \
                 patch('research.championship.refresh', side_effect=self.fitted) as goals, \
                 patch('research.championship_shots.refresh',
                       side_effect=lambda directory, report, output, day: self.fitted(directory, output, day)) as shots:
                checks = run('ELC', root / 'data', root / 'goal', self.day, shots_output=root / 'shot')
            goals.assert_called_once_with(root / 'data', root / 'goal', self.day, offline=True)
            shots.assert_called_once_with(root / 'data', root / 'goal' / 'report.json', root / 'shot', self.day)
            self.assertEqual([c['status'] for c in checks], ['trained', 'trained'])
            self.assertEqual(checks[0]['trainingFingerprint'], checks[1]['trainingFingerprint'])

    def test_shot_failure_does_not_relabel_completed_goal_training_as_unchanged_or_successful_shots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('research.continuous_training.load_sources', return_value=self.records), \
                 patch('research.continuous_training.configuration_hash', return_value=self.configuration), \
                 patch('research.continuous_training.fetch_previous', return_value=None), \
                 patch('research.championship.refresh', side_effect=self.fitted), \
                 patch('research.championship_shots.refresh', side_effect=RuntimeError('fit failed')):
                with self.assertRaises(RuntimeError):
                    run('ELC', root / 'data', root / 'goal', self.day, shots_output=root / 'shot')
            goal = json.loads((root / 'goal/check.json').read_text())
            shot = json.loads((root / 'shot/check.json').read_text())
            self.assertEqual(goal['status'], 'trained'); self.assertEqual(shot['status'], 'failed')
            self.assertNotIn('modelSha256', shot)

    def test_mixed_scopes_fail_closed_and_do_not_advertise_training(self):
        with self.assertRaises(ValueError): previous_success(self.previous(), 'ELC', 'E1', 'goals')
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            with patch('research.continuous_training.load_sources', return_value=self.records), \
                 patch('research.continuous_training.configuration_hash', return_value=self.configuration), \
                 patch('research.continuous_training.fetch_previous', return_value=self.previous('ELC')), \
                 patch('research.nightly.run') as fit:
                with self.assertRaises(ValueError): run('PL', Path(directory) / 'data', output, self.day)
            fit.assert_not_called()
            check = json.loads((output / 'check.json').read_text())
            self.assertEqual(check['status'], 'failed')
            self.assertFalse((output / 'attempt.json').exists())

    def test_failure_retains_explicit_check_without_sensitive_exception_text(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            with patch('research.continuous_training.load_sources', side_effect=OSError('private-detail')):
                with self.assertRaises(OSError): run('PL', Path(directory) / 'data', output, self.day)
            raw = (output / 'check.json').read_text(); check = json.loads(raw)
            self.assertEqual(check['status'], 'failed'); self.assertEqual(check['errorType'], 'OSError')
            self.assertNotIn('private-detail', raw); self.assertNotIn('trainingFingerprint', check)
            self.assertFalse((output / 'model.json').exists())

    def test_missing_archive_is_first_run_but_other_http_errors_are_not_silent_skips(self):
        for status in (404, 403, 503):
            error = urllib.error.HTTPError('https://example.invalid', status, 'error', {}, None)
            with self.subTest(status=status), patch('urllib.request.urlopen', side_effect=error):
                if status == 404: self.assertIsNone(fetch_previous('owner/repo', 'training-reports'))
                else:
                    with self.assertRaises(urllib.error.HTTPError): fetch_previous('owner/repo', 'training-reports')
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{"schemaVersion":1}')):
            self.assertEqual(fetch_previous('owner/repo', 'training-reports'), {'schemaVersion': 1})
        with self.assertRaises(ValueError): fetch_previous('owner/repo/escape', 'training-reports')

    def test_frozen_studies_and_same_output_are_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory); old = output / 'attempt.json'; old.write_text('keep')
            with self.assertRaises(ValueError): run('ELC', output / 'data', output, self.day, shots_output=output)
            self.assertEqual(old.read_text(), 'keep')
            with self.assertRaises(ValueError):
                run('ELC', output / 'data', output, self.day,
                    shots_output=ROOT / 'docs/research/continuous-output-must-not-be-created')
            self.assertEqual(old.read_text(), 'keep')

    def test_incomplete_closed_championship_cache_is_refreshed_and_still_incomplete_source_fails(self):
        from research.championship import load_history
        from research.data import Match
        def downloaded(year, directory, refresh=False, **kwargs):
            n = 10 if year == 2026 else (551 if year == 2015 and not refresh else 552)
            return [Match(date(year, 8, 1)+timedelta(days=i//24), year, f'H{i}', f'A{i}', 1, 0)
                    for i in range(n)], {'season':year}
        with tempfile.TemporaryDirectory() as directory:
            with patch('research.championship.download_season',side_effect=downloaded) as source:
                rows,_=load_history(directory,self.day)
            self.assertEqual(len(rows),11*552+10)
            self.assertTrue(any(c.args[0]==2015 and c.kwargs['refresh'] for c in source.call_args_list))
            with patch('research.championship.download_season',return_value=([],{})) as source:
                with self.assertRaises(ValueError):load_history(directory,self.day)
                self.assertEqual(source.call_count,2)


if __name__ == '__main__': unittest.main()
