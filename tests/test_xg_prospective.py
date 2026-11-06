import copy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import unittest

import numpy as np

from research.data import Match
from research.models import loss
from research.nightly import sha
from research.performance import Performance
from research.xg_prospective import (build_packet, report_markdown, seal_packet,
                                    settle_packet, verify_packet)

NOW = datetime(2026, 11, 6, 3, 48, tzinfo=timezone.utc)
XG = {'id': 'xg100-180', 'goalWeight': 0., 'halfLife': 180,
      'ridge': .03, 'windowDays': 1095, 'poissonWeight': .75}
REFERENCE = {'glm': {'half_life': 365, 'ridge': .03, 'window_days': 1095},
             'elo': {'k': 30, 'home_advantage': 80, 'draw_weight': .9}, 'glm_weight': .75}


def observations():
    rng = np.random.default_rng(84)
    rows = []
    for i in range(220):
        home, away = ('A', 'B') if i % 2 == 0 else ('B', 'A')
        m = Match(date(2026, 1, 1) + timedelta(days=i), 2025, home, away,
                  int(rng.poisson(1.6)), int(rng.poisson(1.1)))
        rows.append(Performance(m, 10, 9, 4, 3, float(rng.uniform(.1, 3)), float(rng.uniform(.1, 2))))
    return rows


def fixture(identifier=1, *, kickoff=None, status='TIMED', home='A', away='B'):
    return {'id': identifier, 'utcDate': (kickoff or (NOW + timedelta(days=2))).isoformat(),
            'status': status, 'competition': {'id': 2021, 'code': 'PL'},
            'season': {'startDate': '2026-08-21'},
            'homeTeam': {'id': 10, 'name': home}, 'awayTeam': {'id': 20, 'name': away}}


def provider(*rows):
    return {'competition': {'id': 2021, 'code': 'PL'}, 'filters': {'season': 2026}, 'matches': list(rows)}


class XgProspectiveTests(unittest.TestCase):
    def build(self, fixtures=None, rows=None):
        return build_packet(rows if rows is not None else observations(), XG, REFERENCE,
                            fixtures if fixtures is not None else provider(fixture()), NOW,
                            source_hashes={'testSource': 'a' * 64})

    def test_same_day_previous_day_and_future_data_never_change_frozen_models(self):
        rows = observations()
        first = self.build(rows=rows)
        cutoff = NOW.date() - timedelta(days=1)
        extra = [replace(rows[0], match=replace(rows[0].match, day=cutoff + timedelta(days=i),
                                              home_goals=20, away_goals=0), home_xg=float('nan'))
                 for i in (0, 1, 2)]
        after = self.build(rows=rows + extra)
        self.assertEqual(first['modelSha256'], after['modelSha256'])
        self.assertEqual(first['dataSha256'], after['dataSha256'])
        self.assertEqual(first['fixtures'], after['fixtures'])
        self.assertLess(first['training']['latestTrainingMatch'], first['trainingCutoffExclusive'])
        self.assertTrue(verify_packet(first))

    def test_lead_boundary_order_first_ten_and_duplicate_ids(self):
        rows = [fixture(2, kickoff=NOW + timedelta(hours=24) - timedelta(seconds=1)),
                fixture(1, kickoff=NOW + timedelta(hours=24)),
                fixture(99, kickoff=NOW + timedelta(days=8)),
                fixture(100, kickoff=NOW + timedelta(days=2), status='FINISHED')]
        rows += [fixture(i, kickoff=NOW + timedelta(days=1, hours=i)) for i in range(3, 15)]
        packet = self.build(provider(*reversed(rows)))
        self.assertEqual(len(packet['fixtures']), 10)
        self.assertEqual(packet['fixtures'][0]['fixtureId'], 1)
        self.assertNotIn(2, [r['fixtureId'] for r in packet['fixtures']])
        self.assertNotIn(99, [r['fixtureId'] for r in packet['fixtures']])
        self.assertNotIn(100, [r['fixtureId'] for r in packet['fixtures']])
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.build(provider(fixture(), fixture()))
        with self.assertRaisesRegex(ValueError, 'No eligible'):
            self.build(provider(fixture(status='FINISHED')))

    def test_settlement_keeps_packet_immutable_and_only_scores_common_finished_games(self):
        fixtures = provider(fixture(1), fixture(2), fixture(3), fixture(4))
        packet = self.build(fixtures)
        before = copy.deepcopy(packet)
        current = copy.deepcopy(fixtures)
        current['matches'][0].update(status='FINISHED', score={'fullTime': {'home': 2, 'away': 0}})
        current['matches'][1].update(status='FINISHED', score={'fullTime': {'home': 1, 'away': 1}})
        current['matches'][2]['utcDate'] = (NOW + timedelta(days=3)).isoformat()
        report = settle_packet(packet, current, NOW + timedelta(days=4))
        self.assertEqual(packet, before)
        self.assertEqual((report['settled'], report['pending'], report['excluded']), (2, 1, 1))
        self.assertEqual(report['commonMatches'], 2)
        self.assertEqual({m['n'] for m in report['metrics'].values()}, {2})
        for key in ('xg', 'reference'):
            expected = [loss(packet['fixtures'][i]['predictions'][key], actual) for i, actual in ((0, 0), (1, 1))]
            self.assertAlmostEqual(report['metrics'][key]['logloss'], np.mean([m['logloss'] for m in expected]))
        self.assertIn('settled common matches: 2', report_markdown(report))

    def test_changed_teams_postponement_and_cancellation_exclude_complete_pairs(self):
        for change in ({'status': 'POSTPONED'}, {'status': 'CANCELLED'},
                       {'homeTeam': {'id': 30, 'name': 'C'}}):
            fixtures = provider(fixture())
            packet = self.build(fixtures)
            current = copy.deepcopy(fixtures)
            current['matches'][0].update(change)
            current['matches'][0]['score'] = {'fullTime': {'home': 3, 'away': 0}}
            report = settle_packet(packet, current, NOW + timedelta(days=4))
            self.assertEqual(report['excluded'], 1)
            self.assertEqual(report['commonMatches'], 0)
            self.assertTrue(all(m['n'] == 0 and m['accuracy'] is None for m in report['metrics'].values()))

    def test_invalid_scores_missing_results_and_future_outcomes_stay_pending(self):
        fixtures = provider(fixture())
        packet = self.build(fixtures)
        for score in ({'home': -1, 'away': 0}, {'home': True, 'away': 0}, {'home': 31, 'away': 0},
                      {'home': 1.5, 'away': 0}, {'home': None, 'away': 0}):
            current = copy.deepcopy(fixtures)
            current['matches'][0].update(status='FINISHED', score={'fullTime': score})
            report = settle_packet(packet, current, NOW + timedelta(days=4))
            self.assertEqual((report['pending'], report['settled']), (1, 0))
        current['matches'][0]['score'] = {'fullTime': {'home': 3, 'away': 0}}
        self.assertEqual(settle_packet(packet, current, NOW)['settled'], 0)
        self.assertEqual(settle_packet(packet, provider(), NOW)['pending'], 1)
        with self.assertRaisesRegex(ValueError, 'precede registration'):
            settle_packet(packet, fixtures, NOW - timedelta(seconds=1))

    def test_digest_probability_and_lead_time_tampering_are_rejected(self):
        packet = self.build()
        bad = copy.deepcopy(packet)
        bad['fixtures'][0]['predictions']['xg'] = [.9, .05, .05]
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            verify_packet(bad)
        bad['fixtures'][0]['predictionsSha256'] = sha(bad['fixtures'][0]['predictions'])
        bad = seal_packet(bad)
        with self.assertRaisesRegex(ValueError, 'disagree with the frozen model'):
            verify_packet(bad)
        bad = copy.deepcopy(packet)
        bad['fixtures'][0]['utcDate'] = (NOW + timedelta(hours=23)).isoformat()
        with self.assertRaisesRegex(ValueError, 'lead time'):
            verify_packet(seal_packet(bad))


if __name__ == '__main__':
    unittest.main()
