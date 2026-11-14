import copy
from datetime import date, timedelta
import json
import gzip
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from research.championship import PLAN, load_snapshot, refresh, scoped_candidate, select_settings, validate_plan
from research.data import Match
from research.nightly import sha
from scripts.publish_training import merge_latest, save_public_model


def sample():
    rng = np.random.default_rng(29)
    teams = ['A', 'B', 'C', 'D', 'E', 'F']
    rows = []
    for season in (2022, 2023, 2024, 2025, 2026):
        for i in range(120):
            home, away = teams[i % 6], teams[(i % 6 + 1 + (i // 6) % 5) % 6]
            rows.append(Match(date(season, 8, 1) + timedelta(days=i), season, home, away,
                              int(rng.poisson(1.6)), int(rng.poisson(1.1))))
    return rows


class ChampionshipTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads(PLAN.read_text())
        self.plan.update(glmGrid=self.plan['glmGrid'][:2], glmWeights=[.75, 1.], maximumCandidates=4,
                         minimumCommonMatches={'selection': 100, 'audit': 60, 'monitoring': 60})

    def test_later_outcomes_cannot_change_selected_settings_or_selection_fingerprint(self):
        matches = sample()
        selected, trials = select_settings(matches, self.plan)
        changed = [Match(m.day, m.season, m.home, m.away, 8, 0) if m.season >= 2025 else m for m in matches]
        after, later_trials = select_settings(changed, self.plan)
        self.assertEqual(selected, after)
        self.assertEqual(trials, later_trials)
        self.assertEqual(len(trials), 4)
        self.assertEqual(selected['league'], 'ELC')
        self.assertTrue(selected['chosenBeforeAudit'])
        self.assertFalse(selected['promotedToDefault'])

    def test_candidate_identity_is_scoped_and_no_target_or_future_result_enters_its_fit(self):
        matches = sample()
        selected, _ = select_settings(matches, self.plan)
        day = date(2026, 12, 10)
        model, report, forecasts = scoped_candidate(matches, selected, day, evaluation_limit=12, min_common=5)
        new = matches + [Match(day + timedelta(days=i), 2026, 'A', 'B', 20, 0) for i in (-1, 0, 1)]
        other, changed, rows = scoped_candidate(new, selected, day, evaluation_limit=12, min_common=5)
        self.assertEqual(model, other)
        self.assertEqual(forecasts, rows)
        self.assertEqual(report['dataSha256'], changed['dataSha256'])
        self.assertEqual(report['modelSha256'], sha(model))
        self.assertEqual(model['league'], 'ELC')
        self.assertTrue(model['model_version'].startswith('ELC-candidate-'))
        self.assertLess(report['latestTrainingMatch'], report['trainingCutoffExclusive'])
        with self.assertRaises(ValueError):
            scoped_candidate(matches, {**selected, 'league': 'PL'}, day)

    def test_unbounded_search_and_overlapping_selection_audit_are_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan['glmGrid'] *= 10
        with self.assertRaises(ValueError):validate_plan(plan)
        plan = copy.deepcopy(self.plan)
        plan['auditSeasons'] = [2024]
        with self.assertRaises(ValueError):validate_plan(plan)

    def test_failed_refresh_removes_stale_weights_and_records_explicit_scoped_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'model.json').write_text('{"old":true}')
            with patch('research.championship.load_history', side_effect=OSError('source unavailable')):
                # This test supplies a structurally valid persisted choice, without running selection again.
                selection = {'planSha256': sha(json.loads(PLAN.read_text())), 'chosenBeforeAudit': True}
                selected = root / 'frozen'; selected.mkdir()
                path = selected / 'selection.json'; path.write_text(json.dumps(selection))
                (selected / 'report.json').write_text(json.dumps({'protocol': self.plan['protocol'],
                    'selectionSha256': sha(selection), 'historicalGatePassed': False}))
                with self.assertRaises(OSError):
                    refresh(root / 'data', root, date(2026, 11, 14), selection_path=path)
            self.assertFalse((root / 'model.json').exists())
            attempt = json.loads((root / 'attempt.json').read_text())
            self.assertEqual(attempt['status'], 'failed')
            self.assertEqual(attempt['league'], 'ELC')
            self.assertNotIn('source unavailable', json.dumps(attempt))

    def test_refresh_verifies_the_frozen_choice_and_cannot_overwrite_the_original_study(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); frozen = root / 'frozen'; frozen.mkdir()
            selection = {'planSha256': sha(json.loads(PLAN.read_text())), 'chosenBeforeAudit': True}
            path = frozen / 'selection.json'; path.write_text(json.dumps(selection))
            report = frozen / 'report.json'
            report.write_text(json.dumps({'protocol': self.plan['protocol'],
                'selectionSha256': 'wrong', 'historicalGatePassed': False}))
            before = report.read_bytes()
            with patch('research.championship.load_history') as load:
                with self.assertRaisesRegex(ValueError, 'verify'):
                    refresh(root / 'data', root / 'nightly', date(2026, 11, 14), selection_path=path)
                with self.assertRaisesRegex(ValueError, 'overwrite'):
                    refresh(root / 'data', frozen, date(2026, 11, 14), selection_path=path)
                load.assert_not_called()
            self.assertEqual(report.read_bytes(), before)

    def test_championship_report_cannot_replace_the_epl_archive(self):
        attempt = {'schemaVersion': 1, 'status': 'success', 'league': 'ELC', 'division': 'E1',
            'generatedAt': '2026-11-14T17:00:00+00:00', 'promotedToDefault': False,
            'latestTrainingMatch': '2026-11-07', 'trainingCutoffExclusive': '2026-11-13',
            'asOf': '2026-11-14', 'evaluation': {'commonMatches': 100}, 'modelSha256': 'a' * 64}
        args = {'repository': 'FoXsYsOfficial1909/Footpredict', 'run_id': '123', 'run_attempt': '1', 'commit': 'b' * 40}
        with self.assertRaisesRegex(ValueError, 'league'):
            merge_latest(None, attempt, **args)
        result = merge_latest(None, attempt, branch='championship-training-reports', **args)
        self.assertIn('/championship-training-reports/models/', result['latestSuccessful']['modelUrl'])
        with self.assertRaises(ValueError):
            merge_latest(None, {**attempt, 'league': 'PL', 'division': 'E0'}, branch='championship-training-reports', **args)

    def test_model_archive_rejects_a_cross_league_model_even_with_matching_hash(self):
        model = {'model_version': 'example', 'league': 'PL', 'division': 'E0'}
        attempt = {'status': 'success', 'league': 'ELC', 'division': 'E1',
                   'modelVersion': 'example', 'modelSha256': sha(model)}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'model.json').write_text(json.dumps(model))
            with self.assertRaisesRegex(ValueError, 'leagues'):
                save_public_model(root, root, attempt)

    def test_pinned_snapshot_is_offline_and_rejects_corruption_and_future_observations(self):
        records = [['2026-08-01', 2026, 'Derby', 'Preston', 2, 1]]
        raw = json.dumps(records, separators=(',', ':')).encode()
        body = gzip.compress(raw, mtime=0)
        manifest = {'league': 'ELC', 'division': 'E1', 'sourceMatches': 1,
            'compressedSha256': hashlib.sha256(body).hexdigest(),
            'uncompressedSha256': hashlib.sha256(raw).hexdigest(), 'dataSha256': sha(records), 'dataSources': []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / 'training-data.json.gz'
            path.write_bytes(body); (root / 'input-manifest.json').write_text(json.dumps(manifest))
            with patch('research.championship.download_season') as download:
                matches, _ = load_snapshot(path, date(2026, 11, 14))
                self.assertEqual(matches[0].home_goals, 2)
                with self.assertRaisesRegex(ValueError, 'fence'):
                    load_snapshot(path, date(2026, 8, 2))
                path.write_bytes(body + b'corrupted')
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    load_snapshot(path, date(2026, 11, 14))
                download.assert_not_called()


if __name__ == '__main__':
    unittest.main()
