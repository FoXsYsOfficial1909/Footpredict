import copy
from datetime import date, datetime, timedelta, timezone
import math
import unittest

from research.corners import CornerObservation
from research.corner_prospective import (build_packet, verify_packet, seal_packet,
    settle_packet, report_markdown)
from research.data import Match
from research.nightly import sha

NOW = datetime(2026, 11, 6, 17, 40, tzinfo=timezone.utc)


def model():
    result = {'schemaVersion': 1, 'model_version': 'corners-test', 'status': 'experimental',
        'target': 'corners_total_regulation', 'as_of': '2026-11-05',
        'intercept': math.log(4.5), 'home_advantage': math.log(1.2),
        'teams': {'A': {'attack': .1, 'defence': 0.}, 'B': {'attack': 0., 'defence': .1}},
        'aliases': {}, 'side_mean_bounds': [.1, 20.], 'family': 'poisson', 'alpha': 0.,
        'fit': {'training_games': 300, 'latest_training_date': '2026-11-02'}}
    for i in range(4, 15):
        result['teams']['A' + str(i)] = {'attack': .1, 'defence': 0.}
        result['teams']['B' + str(i)] = {'attack': 0., 'defence': .1}
    return result


def baseline():
    return {'as_of': '2026-11-05', 'training_cutoff_exclusive': '2026-11-05',
        'totals': {str(line): {'over': p, 'under': 1 - p} for line, p in [(8.5, .6), (9.5, .5), (10.5, .4)]},
        'expectedCorners': {'home': 5., 'away': 4.5, 'total': 9.5},
        'fit': {'training_games': 300, 'latest_training_date': '2026-11-02'}}


def fixture(identifier=1, *, kickoff=None, status='TIMED'):
    suffix = str(identifier) if identifier >= 4 else ''
    return {'id': identifier, 'utcDate': (kickoff or NOW + timedelta(days=2)).isoformat(),
        'status': status, 'competition': {'code': 'PL', 'id': 2021},
        'season': {'startDate': '2026-08-21'},
        'homeTeam': {'id': 10 + identifier, 'name': 'A' + suffix}, 'awayTeam': {'id': 30 + identifier, 'name': 'B' + suffix}}


def provider(*rows):
    return {'competition': {'code': 'PL', 'id': 2021}, 'filters': {'season': 2026}, 'matches': list(rows)}


def observation(raw, home=4, away=5, goals=(0, 0)):
    return CornerObservation(Match(date.fromisoformat(raw['utcDate'][:10]), 2026,
                                  'A', 'B', *goals), home, away)


class CornerProspectiveTests(unittest.TestCase):
    def build(self, rows=None):
        return build_packet(model(), baseline(), rows or provider(fixture()), NOW, {'test': 'a' * 64})

    def test_first_ten_ordered_fixtures_require_full_twenty_four_hours(self):
        raw = [fixture(1, kickoff=NOW + timedelta(hours=24)),
               fixture(2, kickoff=NOW + timedelta(hours=24) - timedelta(seconds=1)),
               fixture(3, kickoff=NOW + timedelta(days=8))]
        raw += [fixture(i, kickoff=NOW + timedelta(hours=24 + i)) for i in range(4, 15)]
        packet = self.build(provider(*reversed(raw)))
        self.assertEqual(len(packet['fixtures']), 10)
        self.assertEqual(packet['fixtures'][0]['fixtureId'], 1)
        self.assertNotIn(2, [f['fixtureId'] for f in packet['fixtures']])
        self.assertNotIn(3, [f['fixtureId'] for f in packet['fixtures']])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.build(provider(fixture(), fixture()))
        with self.assertRaisesRegex(ValueError, 'No eligible'):
            self.build(provider(fixture(status='FINISHED')))

    def test_frozen_hash_and_recomputed_probabilities_reject_mutations(self):
        packet = self.build(); self.assertTrue(verify_packet(packet))
        packet['fixtures'][0]['predictions']['candidate']['8.5']['over'] = .1
        with self.assertRaisesRegex(ValueError, 'digest'):
            verify_packet(packet)
        packet = seal_packet(packet)
        with self.assertRaisesRegex(ValueError, 'prediction digest'):
            verify_packet(packet)
        packet = self.build(); packet['model']['as_of'] = '2026-11-06'
        packet['modelSha256'] = sha(packet['model'])
        with self.assertRaisesRegex(ValueError, 'excluded training'):
            verify_packet(seal_packet(packet))

    def test_pending_missing_corners_do_not_become_zeros_or_model_losses(self):
        a, b = fixture(1), fixture(2, kickoff=NOW + timedelta(days=3, hours=2))
        packet = self.build(provider(a, b)); original = copy.deepcopy(packet)
        for row in (a, b):
            row['status'] = 'FINISHED'; row['score'] = {'fullTime': {'home': 0, 'away': 0}}
        results = [observation(a, None, 4), observation(b, 0, 0)]
        report = settle_packet(packet, provider(a, b), results, NOW + timedelta(days=4))
        self.assertEqual((report['settled'], report['pending'], report['excluded']), (1, 1, 0))
        self.assertEqual(report['fixtures'][1]['totalCorners'], 0)
        self.assertTrue(all(m['n'] == 1 for m in report['metrics'].values()))
        self.assertTrue(all(m['n'] == 1 for stats in report['metrics'].values() for m in stats['byLine'].values()))
        self.assertEqual(packet, original)
        self.assertIn('ожидают угловых: 1', report_markdown(report))

    def test_changed_kickoff_or_postponement_excludes_the_whole_pair(self):
        raw = fixture(); packet = self.build(provider(raw))
        raw['utcDate'] = (NOW + timedelta(days=3)).isoformat()
        report = settle_packet(packet, provider(raw), [], NOW + timedelta(days=4))
        self.assertEqual(report['excluded'], 1)
        raw = fixture(status='POSTPONED')
        report = settle_packet(packet, provider(raw), [], NOW + timedelta(days=4))
        self.assertEqual(report['excluded'], 1)
        self.assertTrue(all(m['n'] == 0 and m['logloss'] is None for m in report['metrics'].values()))

    def test_different_source_score_cannot_settle_a_wrongly_joined_corner_row(self):
        raw = fixture(); packet = self.build(provider(raw))
        raw['status'] = 'FINISHED'; raw['score'] = {'fullTime': {'home': 2, 'away': 1}}
        report = settle_packet(packet, provider(raw), [observation(raw, 8, 2, goals=(0, 0))], NOW + timedelta(days=3))
        self.assertEqual(report['fixtures'][0]['reason'], 'corner_source_score_disagreement')
        self.assertEqual(report['commonMatches'], 0)
        report = settle_packet(packet, provider(raw), [observation(raw, 8, 2, goals=(2, 1))], NOW + timedelta(days=3))
        self.assertEqual(report['commonMatches'], 1)
        self.assertAlmostEqual(report['metrics']['baseline']['byLine']['9.5']['brier'], .25)

    def test_future_results_and_malformed_scores_remain_pending(self):
        raw = fixture(); packet = self.build(provider(raw)); raw['status'] = 'FINISHED'
        raw['score'] = {'fullTime': {'home': 0, 'away': 0}}
        report = settle_packet(packet, provider(raw), [observation(raw)], NOW + timedelta(hours=1))
        self.assertEqual(report['pending'], 1)
        raw['score'] = 'not a score'
        report = settle_packet(packet, provider(raw), [observation(raw)], NOW + timedelta(days=3))
        self.assertEqual(report['pending'], 1)
        with self.assertRaisesRegex(ValueError, 'precedes'):
            settle_packet(packet, provider(raw), [], NOW - timedelta(seconds=1))
