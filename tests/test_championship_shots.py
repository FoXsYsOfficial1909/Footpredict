from dataclasses import replace
from datetime import date, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.championship import ROOT, load_snapshot, scoped_candidate
from research.championship_shots import (PLAN, coverage, final_model, load_csv, load_inputs,
                                         load_current, normalized, refresh, save_inputs, shot_predictions, validate_plan)
from research.nightly import sha
from research.performance import Performance, parse_performance
from tests.test_performance import sample
from scripts.publish_training import merge_latest, save_public_model


class ChampionshipShotTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads(PLAN.read_text())
        target = sample()[180].match
        self.row = {'date': target.day.isoformat(), 'cutoff_date': (target.day - timedelta(days=1)).isoformat(),
                    'home': target.home, 'away': target.away, 'actual': target.outcome}

    def test_current_previous_day_and_future_goals_or_shots_cannot_change_a_forecast(self):
        observations = sample()
        cutoff = date.fromisoformat(self.row['cutoff_date'])
        changed = [replace(o, match=replace(o.match, home_goals=20, away_goals=0),
                           home_shots=90, away_shots=10, home_sot=80, away_sot=1)
                   if o.match.day >= cutoff else o for o in observations]
        before = shot_predictions(observations, [self.row], self.plan['candidate'])
        after = shot_predictions(changed, [self.row], self.plan['candidate'])
        self.assertEqual(before, after)
        self.assertLess(before[2][0]['latest_training_date'], self.row['cutoff_date'])

    def test_eligible_past_shots_have_an_effect_and_missing_shots_retain_the_result(self):
        observations = sample()
        before = shot_predictions(observations, [self.row], self.plan['candidate'])
        changed = [replace(o, home_sot=20, home_shots=40) if o.match.home == self.row['home'] else o
                   for o in observations]
        after = shot_predictions(changed, [self.row], self.plan['candidate'])
        self.assertNotEqual(before[0], after[0])
        self.assertNotEqual(before[2][0]['trainingSha256'], after[2][0]['trainingSha256'])
        observations[0] = replace(observations[0], home_sot=None)
        missing = shot_predictions(observations, [self.row], self.plan['candidate'])
        self.assertEqual(missing[2][0]['training_games'], before[2][0]['training_games'])
        self.assertEqual(missing[2][0]['goalFallbackMatches'], 1)

    def test_same_cutoff_reuses_fit_and_unsafe_cutoffs_or_unseen_clubs_are_rejected(self):
        rows = [self.row, {**self.row, 'home': 'B', 'away': 'D'}]
        result = shot_predictions(sample(), rows, self.plan['candidate'])
        self.assertEqual(len(result[0]), 2)
        self.assertEqual(len(result[2]), 1)
        with self.assertRaises(ValueError):
            shot_predictions(sample(), [{**self.row, 'cutoff_date': self.row['date']}], self.plan['candidate'])
        with self.assertRaisesRegex(ValueError, 'unseen'):
            shot_predictions(sample(), [{**self.row, 'home': 'Unknown'}], self.plan['candidate'])

    def test_e1_statistics_cannot_be_loaded_as_another_division(self):
        raw = b'Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HS,AS,HST,AST\nE1,24/10/2026,Derby,Preston,2,1,H,10,8,4,9\n'
        row = parse_performance(raw, 2026, division='E1')[0]
        self.assertEqual(row.match.away_goals, 1)
        self.assertIsNone(row.away_sot)
        with self.assertRaises(ValueError):parse_performance(raw, 2026, division='E0')
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / '2015-E1.csv').write_bytes(raw)
            with self.assertRaisesRegex(ValueError, 'bytes changed'):
                load_csv(directory, self.plan)

    def test_offline_archive_reproduces_fields_and_binds_the_original_goal_identities(self):
        matches, sources = load_snapshot(ROOT / self.plan['sourceStudy'] / 'training-data.json.gz', date(2026, 11, 14))
        observations = [Performance(m, 12., 10., 4., 3.) for m in matches]
        observations[0] = replace(observations[0], home_sot=None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = save_inputs(root, observations, sources, self.plan)
            path = root / 'shots-data.json.gz'
            restored, _ = load_inputs(path, self.plan)
            self.assertEqual(observations, restored)
            self.assertEqual(coverage(restored)['goalFallbackMatches'], 1)
            path.write_bytes(path.read_bytes() + b'corrupt')
            with self.assertRaisesRegex(ValueError, 'checksum'):load_inputs(path, self.plan)
            records = normalized(observations); records[0][4] = 20
            raw = json.dumps(records, ensure_ascii=False, separators=(',', ':')).encode()
            compressed = gzip.compress(raw, mtime=0); path.write_bytes(compressed)
            manifest.update(compressedSha256=hashlib.sha256(compressed).hexdigest(),
                            uncompressedSha256=hashlib.sha256(raw).hexdigest(), dataSha256=sha(records))
            (root / 'shots-manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'goal fixtures'):load_inputs(path, self.plan)

    def test_one_recipe_is_fixed_and_export_never_promotes_it_or_calls_shots_xg(self):
        validate_plan(self.plan)
        altered = {**self.plan, 'candidateCount': 2}
        with self.assertRaises(ValueError):validate_plan(altered)
        altered = {**self.plan, 'candidate': {**self.plan['candidate'], 'goal_weight': .1}}
        with self.assertRaises(ValueError):validate_plan(altered)
        model = final_model(sample(), self.plan, False)
        self.assertFalse(model['promoted_to_default'])
        self.assertFalse(model['is_xg'])
        self.assertFalse(model['glm']['shot_target']['is_xg'])
        self.assertEqual(model['league'], 'ELC')
        self.assertEqual(model['glm']['shot_target']['goal_weight'], .5)
        self.assertLess(model['glm']['fit']['latest_training_date'], model['training_cutoff_exclusive'])

    def daily_files(self, root):
        observations = sample()
        selection = json.loads((ROOT / self.plan['sourceStudy'] / 'selection.json').read_text())
        _, report, _ = scoped_candidate([o.match for o in observations], selection, date(2026, 11, 14),
                                        evaluation_limit=60, min_common=60)
        goal = root / 'goals' / 'report.json'; goal.parent.mkdir()
        goal.write_text(json.dumps(report))
        study = root / 'study' / 'report.json'; study.parent.mkdir()
        study.write_text(json.dumps({'protocol': self.plan['protocol'], 'planSha256': sha(self.plan),
            'settings': self.plan['candidate'], 'historicalGatePassed': False}))
        return observations, goal, study, report

    def test_daily_update_reuses_inputs_preserves_false_qualification_and_hashes_exact_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); observations, goal, study, _ = self.daily_files(root)
            with patch('research.championship_shots.load_current', return_value=(observations, [])), \
                 patch('research.data.urllib.request.urlopen') as network:
                refresh(root / 'data', goal, root / 'shots', date(2026, 11, 14), study_path=study)
                network.assert_not_called()
            output = root / 'shots'; model = json.loads((output / 'model.json').read_text())
            report = json.loads((output / 'attempt.json').read_text())
            self.assertEqual(report['status'], 'success')
            self.assertEqual(report['modelSha256'], sha(model))
            self.assertEqual(report['predictionSha256'], sha(json.loads((output / 'evaluation.json').read_text())))
            self.assertEqual(report['additionalProviderRequests'], 0)
            self.assertFalse(report['historicalGatePassed']); self.assertFalse(model['promoted_to_default'])
            self.assertEqual(model['method'], 'shots_on_target')
            self.assertLess(report['latestTrainingMatch'], report['trainingCutoffExclusive'])

    def test_current_source_mismatch_fails_without_network_or_stale_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); observations, goal, study, report = self.daily_files(root)
            report['dataSha256'] = 'changed'
            with patch('research.championship_shots.load_history', return_value=([o.match for o in observations], [])) as load:
                with self.assertRaisesRegex(ValueError, 'changed'):
                    load_current(root, report, date(2026, 11, 14), self.plan)
                self.assertTrue(load.call_args.kwargs['offline'])
            output = root / 'shots'; output.mkdir(); (output / 'model.json').write_text('{"old":true}')
            with patch('research.championship_shots.load_current', side_effect=OSError('private source detail')):
                with self.assertRaises(OSError):
                    refresh(root / 'data', goal, output, date(2026, 11, 14), study_path=study)
            self.assertFalse((output / 'model.json').exists())
            attempt = json.loads((output / 'attempt.json').read_text())
            self.assertEqual(attempt['status'], 'failed'); self.assertEqual(attempt['method'], 'shots_on_target')
            self.assertNotIn('private source detail', json.dumps(attempt))

    def test_shot_archive_cannot_replace_a_goal_model_even_within_the_same_league(self):
        model = {'league': 'ELC', 'division': 'E1', 'method': 'shots_on_target', 'model_version': 'shot-example'}
        attempt = {'schemaVersion': 1, 'status': 'success', 'league': 'ELC', 'division': 'E1',
            'method': 'shots_on_target', 'generatedAt': '2026-11-14T18:00:00+00:00', 'promotedToDefault': False,
            'latestTrainingMatch': '2026-11-07', 'trainingCutoffExclusive': '2026-11-13', 'asOf': '2026-11-14',
            'evaluation': {'commonMatches': 60}, 'modelSha256': sha(model), 'modelVersion': model['model_version']}
        args = {'repository': 'FoXsYsOfficial1909/Footpredict', 'run_id': '123', 'run_attempt': '1', 'commit': 'b' * 40}
        with self.assertRaisesRegex(ValueError, 'method'):
            merge_latest(None, attempt, branch='championship-training-reports', **args)
        merged = merge_latest(None, attempt, branch='championship-shots-training-reports', **args)
        self.assertIn('/championship-shots-training-reports/models/', merged['latestSuccessful']['modelUrl'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'model.json').write_text(json.dumps(model))
            with self.assertRaisesRegex(ValueError, 'methods'):
                save_public_model(root, root, {**attempt, 'method': 'goals'})


if __name__ == '__main__':
    unittest.main()
