from contextlib import ExitStack
from datetime import date
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.data import Match
from research.nightly import sha
from research.performance import Performance
from research.xg_data import merge_xg
from research.xg_experiment import execute


IMPLEMENTATION_FILES = (
    'research/xg_experiment.py', 'research/xg_model.py', 'research/xg_data.py',
    'research/performance.py', 'research/models.py', 'research/evaluate.py',
    'research/data.py',
)


class XgExperimentGuardsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'output'
        self.output.mkdir()
        self.plan_path = self.root / 'plan.json'
        self.xg_path = self.root / 'xg-matches.json'
        self.source_path = self.root / 'xg-manifest.json'
        self.settings_path = self.root / 'docs/research/selection.json'
        self.settings_path.parent.mkdir(parents=True)
        self.observations = [Performance(
            Match(date(2023, 8, 15), 2023, 'A', 'B', 1, 0), 10, 8, 4, 2)]
        self.csv_manifest = [{'season': 2023, 'rows': 380, 'sha256': 'verified-csv'}]
        self.plan = {
            'protocol': 'test-xg', 'seasons': [2023, 2024, 2025],
            'selectionSeasons': [2023], 'auditSeasons': [2024],
            'recentMonitoringSeasons': [2025], 'models': [],
            'expectedValidationCommon': 736, 'expectedAuditCommon': 369,
        }
        self.settings = {'glm': {'half_life': 365, 'ridge': .03, 'window_days': 1095},
                         'elo': {'k': 30, 'home_advantage': 80, 'draw_weight': .9},
                         'glm_weight': .75}
        self.old_outputs = {}
        for name in ('data-manifest.json', 'implementation.json', 'protocol.json'):
            content = ('{"previousResult":"' + name + '"}\n').encode()
            (self.output / name).write_bytes(content)
            self.old_outputs[name] = content
        self.reset_inputs()

    def write_json(self, path, value):
        path.write_text(json.dumps(value))

    def reset_inputs(self):
        self.write_json(self.plan_path, self.plan)
        self.write_json(self.settings_path, self.settings)
        self.xg_path.write_text('[]')
        self.write_json(self.source_path, {
            'normalizedMatchesSha256': hashlib.sha256(self.xg_path.read_bytes()).hexdigest()})
        for name in IMPLEMENTATION_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture implementation: ' + name)

    def frozen_inputs(self):
        _, joined = merge_xg(self.observations, [])
        provenance = {
            'csv': self.csv_manifest,
            'xgSha256': hashlib.sha256(self.xg_path.read_bytes()).hexdigest(),
            'join': joined,
            'archive': json.loads(self.source_path.read_text()),
        }
        implementations = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                           for name in IMPLEMENTATION_FILES}
        return {
            'planSha256': sha(json.loads(self.plan_path.read_text())),
            'dataSha256': sha(provenance),
            'referenceSettingsSha256': sha(json.loads(self.settings_path.read_text())),
            'implementationSha256': sha(implementations),
        }

    def guards(self):
        stack = ExitStack()
        stack.enter_context(patch('research.xg_experiment.ROOT', self.root))
        stack.enter_context(patch('research.xg_experiment.load_performance',
                                  return_value=(self.observations, self.csv_manifest)))
        self.reference = stack.enter_context(patch('research.xg_experiment.reference_rows',
            side_effect=AssertionError('Input rejection must precede reference computation')))
        self.glm = stack.enter_context(patch('research.xg_experiment.glm_predictions',
            side_effect=AssertionError('Input rejection must precede model fitting')))
        self.challenger = stack.enter_context(patch('research.xg_experiment.ExpectedGoalsPoisson.fit',
            side_effect=AssertionError('Input rejection must precede challenger fitting')))
        return stack

    def run_experiment(self, *, phase='selection', source_manifest=True):
        execute(self.root / 'csv', self.xg_path, self.output, self.plan_path,
                phase=phase, workers=1,
                source_manifest=self.source_path if source_manifest else None)

    def assert_outputs_preserved_and_no_fitting(self):
        for name, original in self.old_outputs.items():
            self.assertEqual((self.output / name).read_bytes(), original)
        self.reference.assert_not_called()
        self.glm.assert_not_called()
        self.challenger.assert_not_called()

    def test_source_manifest_must_bind_exact_normalized_bytes_before_fitting(self):
        cases = [('absent', None), ('missing checksum', {}),
                 ('wrong checksum', {'normalizedMatchesSha256': '0' * 64})]
        for label, manifest in cases:
            with self.subTest(source=label), self.guards():
                if manifest is not None:
                    self.write_json(self.source_path, manifest)
                with self.assertRaisesRegex(ValueError, 'source manifest'):
                    self.run_experiment(source_manifest=manifest is not None)
                self.assert_outputs_preserved_and_no_fitting()

    def test_audit_rejects_changed_frozen_inputs_without_overwriting_or_fitting(self):
        for changed in ('implementation', 'recipe', 'source bytes', 'plan'):
            self.reset_inputs()
            self.write_json(self.output / 'selection.json', self.frozen_inputs())
            if changed == 'implementation':
                path = self.root / IMPLEMENTATION_FILES[0]
                path.write_text(path.read_text() + '\nchanged implementation')
            elif changed == 'recipe':
                recipe = json.loads(self.settings_path.read_text())
                recipe['glm']['ridge'] = .15
                self.write_json(self.settings_path, recipe)
            elif changed == 'source bytes':
                self.xg_path.write_text('[]\n')
                self.write_json(self.source_path, {
                    'normalizedMatchesSha256': hashlib.sha256(self.xg_path.read_bytes()).hexdigest()})
            else:
                plan = json.loads(self.plan_path.read_text())
                plan['protocol'] = 'changed plan'
                self.write_json(self.plan_path, plan)
            with self.subTest(changed=changed), self.guards():
                with self.assertRaisesRegex(ValueError, 'changed after selection'):
                    self.run_experiment(phase='audit')
                self.assert_outputs_preserved_and_no_fitting()

    def test_matching_frozen_inputs_reach_reference_stage_without_real_fitting(self):
        self.write_json(self.output / 'selection.json', self.frozen_inputs())
        with self.guards():
            self.reference.side_effect = RuntimeError('reference stage reached')
            with self.assertRaisesRegex(RuntimeError, 'reference stage reached'):
                self.run_experiment(phase='audit')
            self.reference.assert_called_once()
            self.glm.assert_not_called()
            self.challenger.assert_not_called()

    def test_truncated_closed_season_is_rejected_before_fitting_or_output_changes(self):
        self.csv_manifest[0]['rows'] = 379
        with self.guards():
            with self.assertRaisesRegex(ValueError, '380 verified results'):
                self.run_experiment()
            self.assert_outputs_preserved_and_no_fitting()

    def test_changed_validation_cohort_is_rejected_before_challenger_fitting(self):
        row = {
            'date': '2023-08-15', 'cutoff_date': '2023-08-14', 'season': 2023,
            'home': 'A', 'away': 'B', 'actual': 0,
            'base': [.4, .3, .3], 'v1': [.5, .25, .25],
        }
        with self.guards():
            self.reference.side_effect = None
            self.reference.return_value = [row]
            self.glm.side_effect = None
            self.glm.return_value = {'predictions': [[.5, .25, .25]]}
            with patch('research.xg_experiment.elo_predictions', return_value=[[.5, .25, .25]]), \
                 patch('research.xg_experiment.ProcessPoolExecutor') as pool:
                with self.assertRaisesRegex(ValueError, 'Validation cohort'):
                    self.run_experiment()
                self.challenger.assert_not_called()
                pool.assert_not_called()


if __name__ == '__main__':
    unittest.main()
