import copy
from datetime import date
import unittest

from research.workload import workload_state


def event(when='2026-11-01', competition='PL', **changes):
    return {'date': when, 'home': 'Arsenal', 'away': 'Chelsea', 'competition': competition,
            'minutes': 90, 'minutes_known': True, 'status': 'played',
            'source_id': when + '-' + competition, 'source_url': 'https://example.test/fixture', **changes}


def coverage(*competitions):
    return {'requiredCompetitions': list(competitions), 'requiredCompetitionsVerified': True,
            'scopes': [{'competition': comp, 'start': '2026-10-01', 'end': '2026-11-30',
                        'complete': True, 'source_url': 'https://example.test/season'} for comp in competitions]}


class WorkloadTests(unittest.TestCase):
    def state(self, rows, proof=None):
        return workload_state(rows, 'Arsenal FC', 'Chelsea FC', date(2026, 11, 6),
                              coverage=proof if proof is not None else coverage('PL', 'LC'))

    def test_previous_day_target_and_future_events_cannot_affect_features(self):
        rows = [event()]
        first = self.state(rows)
        excluded = [event(when, minutes=-1, home=None) for when in ('2026-11-05', '2026-11-06', '2026-11-07')]
        self.assertEqual(first, self.state(rows + excluded))
        self.assertLess(first['home']['lastKnownMatch']['date'], first['cutoffExclusive'])

    def test_cup_match_changes_rest_and_congestion_without_touching_opponent(self):
        league = event(away='Liverpool')
        first = self.state([league])
        cup = event('2026-11-03', 'LC', away='Everton', minutes=120)
        after = self.state([league, cup])
        self.assertEqual((first['home']['daysRest'], after['home']['daysRest']), (5, 3))
        self.assertEqual((after['home']['matchesLast7Days'], after['home']['matchesLast14Days']), (2, 2))
        self.assertEqual(after['home']['extraTimeGamesLast14Days'], 1)
        self.assertEqual(first['away'], after['away'])
        self.assertEqual(after['home']['lastKnownMatch']['competition'], 'LC')

    def test_duplicate_fixtures_and_source_ids_never_double_count(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate workload fixture'):
            self.state([event(), event(competition='LC')])
        with self.assertRaisesRegex(ValueError, 'source fixture ID'):
            self.state([event(), event('2026-11-02', source_id='2026-11-01-PL')])

    def test_unknown_or_missing_competition_coverage_never_imputes_zero(self):
        for proof in ({}, {'requiredCompetitions': ['PL'], 'scopes': coverage('PL')['scopes']},
                      {**coverage('PL', 'LC'), 'scopes': coverage('PL')['scopes']}):
            state = self.state([event()], proof)
            self.assertIsNone(state['home']['daysRest'])
            self.assertIsNone(state['home']['matchesLast7Days'])
            self.assertIsNone(state['away']['extraTimeGamesLast14Days'])
            self.assertTrue(all(value is None for value in state['vector'].values()))
            self.assertEqual(state['home']['observedLowerBounds']['matchesLast7Days'], 1)
            self.assertFalse(state['home']['coverage']['allCompetitionsComplete'])

    def test_unknown_minutes_do_not_become_zero_extra_time(self):
        rows = [event(), event('2026-11-03', 'LC', minutes=None, minutes_known=False)]
        state = self.state(rows)
        self.assertEqual(state['home']['matchesLast7Days'], 2)
        self.assertIsNone(state['home']['extraTimeGamesLast14Days'])
        self.assertFalse(state['home']['coverage']['extraTimeCoverageComplete'])
        self.assertEqual(state['home']['unknownMinutesGamesLast14Days'], 1)
        with self.assertRaisesRegex(ValueError, 'minutes coverage'):
            self.state([event(minutes=None)])

    def test_complete_empty_window_gives_capped_rest_and_verified_zero_counts(self):
        state = self.state([])
        self.assertEqual(state['home']['daysRest'], 14)
        self.assertEqual(state['home']['matchesLast14Days'], 0)
        self.assertEqual(state['home']['extraTimeGamesLast14Days'], 0)
        self.assertTrue(state['home']['restCappedAt14'])
        self.assertIsNone(state['home']['lastKnownMatch'])

    def test_team_scopes_and_adjacent_intervals_require_continuous_coverage(self):
        proof = coverage('PL')
        proof['scopes'] = [
            {'competition': 'PL', 'team': 'Arsenal', 'start': '2026-10-23', 'end': '2026-10-29', 'complete': True},
            {'competition': 'PL', 'team': 'Arsenal', 'start': '2026-10-30', 'end': '2026-11-04', 'complete': True},
        ]
        state = self.state([event()], proof)
        self.assertIsNotNone(state['home']['daysRest'])
        self.assertIsNone(state['away']['daysRest'])
        gap = copy.deepcopy(proof)
        gap['scopes'][1]['start'] = '2026-10-31'
        self.assertIsNone(self.state([event()], gap)['home']['daysRest'])


if __name__ == '__main__':
    unittest.main()
