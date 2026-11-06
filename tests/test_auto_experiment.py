from dataclasses import replace
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from research.auto_experiment import cached_predictions, execute, metrics
from research.data import Match
from research.nightly import sha
from research.performance import Performance


RECIPE = {'id': 'xg', 'goalWeight': 0., 'halfLife': 180, 'ridge': .03,
          'windowDays': 1095, 'poissonWeight': .75}
ELO = {'k': 30, 'home_advantage': 80, 'draw_weight': .9}


def observations():
    rng = np.random.default_rng(149)
    names = ['A', 'B', 'C', 'D', 'E', 'F']
    result = []
    for i in range(220):
        home, away = names[i % 6], names[(i % 6 + 1 + (i // 6) % 5) % 6]
        when = date(2023, 1, 1) + timedelta(days=i)
        m = Match(when, 2023 if when.month >= 7 else 2022, home, away,
                  int(rng.poisson(1.6)), int(rng.poisson(1.1)))
        result.append(Performance(m, None, None, None, None,
                                  float(rng.uniform(.3, 3)), float(rng.uniform(.2, 2))))
    return result


def target(when, actual=0):
    return {'date': when, 'cutoff_date': (date.fromisoformat(when) - timedelta(days=1)).isoformat(),
            'home': 'A', 'away': 'B', 'season': 2023, 'actual': actual,
            'v1': [.5, .25, .25], 'base': [.4, .3, .3]}


class PredictionCacheTests(unittest.TestCase):
    def predict(self, rows, targets, directory, code='code-1'):
        return cached_predictions((rows, targets, RECIPE, ELO, directory, code))

    def test_cold_cache_and_partially_invalidated_cache_have_identical_predictions(self):
        rows = observations(); targets = [target('2023-08-07'), target('2023-08-20')]
        with tempfile.TemporaryDirectory() as temp:
            first = self.predict(rows, targets, Path(temp) / 'cache')
            cached = self.predict(rows, targets, Path(temp) / 'cache')
            self.assertEqual(first['predictions'], cached['predictions'])
            self.assertEqual(cached['cacheStats']['hits'], 2)
            self.assertEqual(cached['cacheStats']['fits'], 0)
            changed = rows[:-1] + [replace(rows[-1], match=replace(rows[-1].match, home_goals=9), home_xg=5.)]
            partial = self.predict(changed, targets, Path(temp) / 'cache')
            cold = self.predict(changed, targets, Path(temp) / 'cold')
            self.assertEqual(partial['predictions'], cold['predictions'])
            self.assertEqual(partial['predictions'][0], first['predictions'][0])
            self.assertEqual(partial['cacheStats']['hits'], 1)
            self.assertEqual(partial['cacheStats']['misses'], 1)

    def test_future_statistics_and_target_label_do_not_enter_their_own_cached_prediction(self):
        rows = observations(); targets = [target('2023-08-07')]
        with tempfile.TemporaryDirectory() as temp:
            first = self.predict(rows, targets, temp)
            cutoff = date(2023, 8, 6)
            changed = [replace(r, match=replace(r.match, home_goals=20), home_xg=14.)
                       if r.match.day >= cutoff else r for r in rows]
            after = self.predict(changed, [{**targets[0], 'actual': 2}], temp)
            self.assertEqual(first['predictions'], after['predictions'])
            self.assertEqual(after['cacheStats']['hits'], 1)
            p = first['predictions'][0]
            a = [{**targets[0], 'baseline': p, 'actual': int(np.argmax(p))}]
            b = [{**targets[0], 'baseline': p, 'actual': (int(np.argmax(p)) + 1) % 3}]
            self.assertNotEqual(metrics(a, 'baseline')['accuracy'], metrics(b, 'baseline')['accuracy'])

    def test_corrupt_cache_or_changed_implementation_requires_recomputation(self):
        with tempfile.TemporaryDirectory() as temp:
            rows = observations(); targets = [target('2023-08-20')]
            first = self.predict(rows, targets, temp)
            path = next(Path(temp).rglob('*.json'))
            packet = json.loads(path.read_text()); packet['probabilities'] = [.01, .01, .98]
            path.write_text(json.dumps(packet))
            second = self.predict(rows, targets, temp)
            self.assertEqual(second['predictions'], first['predictions'])
            self.assertEqual(second['cacheStats']['corrupt'], 1)
            changed = self.predict(rows, targets, temp, code='code-2')
            self.assertEqual(changed['cacheStats']['misses'], 1)


class AutomaticProcedureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.xg = self.root / 'xg.json'; self.xg.write_text('[]')
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'normalizedMatchesSha256': hashlib.sha256(b'[]').hexdigest(),
                                             'generatedAt': 'old check'}))
        self.plan = self.root / 'plan.json'
        self.plan.write_text(json.dumps({'protocol': 'automatic-experiments-1',
            'seasons': [2023], 'selectionSeasons': [2023], 'auditSeasons': [2023],
            'recentMonitoringSeasons': [2023], 'expectedValidationCommon': 1,
            'expectedAuditCommon': 1, 'baseline': RECIPE, 'models': [], 'elo': ELO}))
        self.rows = observations()
        xg_rows = [{'date': r.match.day.isoformat(), 'season': r.match.season,
                    'home': r.match.home, 'away': r.match.away,
                    'home_goals': r.match.home_goals, 'away_goals': r.match.away_goals,
                    'home_xg': r.home_xg, 'away_xg': r.away_xg} for r in self.rows]
        self.xg.write_text(json.dumps(xg_rows))
        self.xg_digest = hashlib.sha256(self.xg.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps({'normalizedMatchesSha256': self.xg_digest,
                                             'generatedAt': 'old check'}))
        self.source = [{'season': 2023, 'rows': 220, 'sha256': 'raw-source-1'}]
        self.target = [target('2023-08-07')]

    def run_experiment(self, code='code-1', as_of=date(2023, 9, 1)):
        with patch('research.auto_experiment.load_performance', return_value=(self.rows, self.source)), \
             patch('research.auto_experiment.reference_rows', return_value=self.target), \
             patch('research.auto_experiment.code_revision', return_value=code):
            return execute(self.root, self.xg, self.manifest, self.root / 'output',
                           self.root / 'cache', self.root / 'state', self.plan, as_of, workers=1)

    def test_date_advance_invalidates_only_when_it_makes_observations_eligible(self):
        first = self.run_experiment(as_of=date(2023, 8, 9))
        second = self.run_experiment(as_of=date(2023, 8, 10))
        self.assertNotEqual(first['inputFingerprint'], second['inputFingerprint'])
        third = self.run_experiment(as_of=date(2023, 8, 11))
        self.assertEqual(third['status'], 'unchanged')
        self.assertEqual(third['inputFingerprint'], second['inputFingerprint'])

    def test_unchanged_model_inputs_skip_all_fitting_even_if_raw_metadata_changes(self):
        first = self.run_experiment()
        state = (self.root / 'state/last-success.json').read_bytes()
        self.source[0]['sha256'] = 'odds-only-file-byte-change'
        self.manifest.write_text(json.dumps({'normalizedMatchesSha256': self.xg_digest,
                                             'generatedAt': 'new check'}))
        with patch('research.auto_experiment.cached_predictions', side_effect=AssertionError('Must skip fits')):
            second = self.run_experiment()
        self.assertEqual(second['status'], 'unchanged')
        self.assertEqual(second['evaluatedAt'], first['evaluatedAt'])
        self.assertEqual(second['inputFingerprint'], first['inputFingerprint'])
        self.assertEqual(state, (self.root / 'state/last-success.json').read_bytes())
        self.assertEqual(second['cacheStats']['fits'], 0)

    def test_changed_data_or_code_reexecutes_and_bad_sources_preserve_success(self):
        first = self.run_experiment()
        self.rows.append(Performance(Match(date(2023, 8, 9), 2023, 'A', 'B', 2, 0),
                                     None, None, None, None))
        second = self.run_experiment()
        self.assertNotEqual(second['inputFingerprint'], first['inputFingerprint'])
        third = self.run_experiment(code='code-2')
        self.assertNotEqual(third['inputFingerprint'], second['inputFingerprint'])
        state = (self.root / 'state/last-success.json').read_bytes()
        self.manifest.write_text('{"normalizedMatchesSha256":"incorrect"}')
        with self.assertRaisesRegex(ValueError, 'manifest'):
            self.run_experiment()
        self.assertEqual(state, (self.root / 'state/last-success.json').read_bytes())
        report = json.loads((self.root / 'output/report.json').read_text())
        self.assertEqual(report['status'], 'failed')
        self.assertFalse(report['promotedToDefault'])

    def test_state_corruption_is_rejected_instead_of_advertised_as_a_reused_success(self):
        self.run_experiment()
        path = self.root / 'state/last-success.json'; state = json.loads(path.read_text())
        state['report']['selection']['baseline']['accuracy'] = .123456
        path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ValueError, 'state checksum'):
            self.run_experiment()


if __name__ == '__main__':
    unittest.main()
