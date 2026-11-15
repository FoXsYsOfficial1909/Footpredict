import copy
import shutil
from pathlib import Path
import tempfile
import unittest

from research.quality_diagnostic import DIRECTORY, checked_probabilities, reliability, metrics, evaluate, execute, load_inputs


class QualityDiagnosticTests(unittest.TestCase):
    def row(self, probabilities=None, outcome=0):
        p = [.6, .25, .15] if probabilities is None else probabilities
        return {'date': '2025-08-15', 'cutoff_date': '2025-08-14', 'season': 2025,
                'home': 'A', 'away': 'B', 'actual': outcome,
                **{k: list(p) for k in ('v1', 'candidate', 'base', 'glm', 'elo')}}

    def test_score_scale_ties_and_empty_calibration_are_explicit(self):
        rows = [self.row([1/3]*3)]
        m = metrics(rows, 'v1')
        self.assertAlmostEqual(m['brier'], 2/3)
        self.assertEqual(m['accuracy'], 1)
        self.assertEqual(m['topTies'], 1)
        self.assertIsNone(m['uniqueTopAccuracy'])
        self.assertIsNone(metrics([], 'v1')['accuracy'])
        cells = reliability([], 'v1')
        self.assertTrue(all(v['n'] == 0 and v['ece'] is None for v in cells.values()))

    def test_calibration_bins_boundaries_and_brier_components(self):
        rows = [self.row([1, 0, 0]), self.row([0, 0, 1], 2)]
        cal = reliability(rows, 'v1')
        self.assertEqual(cal['P1']['bins'][-1]['n'], 1)
        self.assertEqual(cal['P1']['bins'][0]['n'], 1)
        self.assertEqual(cal['X']['bins'][0]['n'], 2)
        self.assertEqual(sum(c['binaryBrier'] for c in cal.values()), metrics(rows, 'v1')['brier'])
        self.assertEqual(cal['P1']['ece'], 0)

    def test_shared_cohort_and_information_fence_are_checked(self):
        first = self.row(); second = self.row(); second.update(home='C', v1=None)
        report = evaluate([first, second])
        self.assertEqual(report['completedMatches'], 2)
        self.assertEqual(report['checkedCommonMatches'], 1)
        self.assertEqual(report['excludedFromEveryModel'], 1)
        self.assertEqual({v['n'] for v in report['metrics'].values()}, {1})
        self.assertEqual(report['fullCoverageMetrics']['candidate']['n'], 2)
        with self.assertRaises(ValueError): evaluate([first, first])
        changed = copy.deepcopy(first); changed['cutoff_date'] = changed['date']
        with self.assertRaises(ValueError): evaluate([changed])
        for p in ([.5, .5, .5], [float('nan'), .5, .5], [True, 0, 0], [-.1, .5, .6]):
            with self.assertRaises(ValueError): checked_probabilities(p)

    def test_persisted_prediction_scores_match_the_original_without_a_new_fit(self):
        with tempfile.TemporaryDirectory() as directory:
            report = execute(directory)
            self.assertTrue(report['publishedMetricsReproduced'])
            self.assertFalse(report['newTrainingPerformed'])
            self.assertFalse(report['independentTest'])
            later = report['results']['held_out']
            self.assertEqual(later['completedMatches'], 380)
            self.assertEqual(later['checkedCommonMatches'], 369)
            self.assertEqual(later['excludedFromEveryModel'], 11)
            self.assertAlmostEqual(later['metrics']['v1']['accuracy'], 179/369)
            live = report['liveSnapshot']
            self.assertEqual(live['legacyArchives']['v1']['metrics']['correct'], 2)
            self.assertEqual(live['legacyArchives']['v1']['metrics']['n'], 12)
            self.assertFalse(live['legacyArchivesComparable'])
            self.assertTrue(live['nightlyComparison']['liveMetricsIndependentlyReproduced'])
            self.assertEqual(live['nightlyComparison']['metrics']['candidate']['n'], 10)
            self.assertTrue((Path(directory)/'report.json').exists())

    def test_changed_saved_predictions_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'inputs'; shutil.copytree(DIRECTORY, target)
            snapshot = target/'held-out-predictions.json.gz'
            snapshot.write_bytes(snapshot.read_bytes()+b'changed')
            with self.assertRaises(ValueError): load_inputs(target)


if __name__ == '__main__': unittest.main()
