import copy
from datetime import date, timedelta
import hashlib
import json
import unittest

import numpy as np

from research.hundred import DIRECTORY, REGISTERED_SHA
from research.league_strength import PLAN, cutoff_date, evaluate, forecast_league
from research.nightly import sha


def sample_games():
    rng = np.random.default_rng(49)
    games = []
    for i in range(220):
        home, away = i % 6 + 1, (i % 6 + 1 + (i // 6) % 5) % 6 + 1
        day = date(2025, 1, 1) + timedelta(days=i)
        games.append({'id': i + 1, 'utcDate': day.isoformat() + 'T15:00:00Z',
            '__year': 2025, 'status': 'FINISHED', 'homeTeam': {'id': home}, 'awayTeam': {'id': away},
            'score': {'duration': 'REGULAR', 'fullTime': {
                'home': int(rng.poisson(2.5 if home == 1 else 1.4)), 'away': int(rng.poisson(1.1))}}})
    return games


def target(code='PL', day='2025-08-20', fixture_id=1001):
    kickoff = date.fromisoformat(day)
    return {'fixtureId': fixture_id, 'league': code, 'date': day, 'kickoff': day + 'T21:00:00Z',
        'simulatedForecastAt': (kickoff - timedelta(days=1)).isoformat() + 'T21:00:00Z',
        'season': 2025, 'homeId': 1, 'awayId': 2, 'actual': 0,
        'v1': [.45, .3, .25], 'base': [.4, .3, .3]}


class LeagueStrengthTests(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads(PLAN.read_text())

    def test_information_fence_rounds_down_in_utc_including_early_kickoffs(self):
        row = target(day='2026-09-20')
        self.assertEqual(cutoff_date(row), date(2026, 9, 19))
        row['kickoff'] = '2026-09-20T02:00:00Z'
        row['simulatedForecastAt'] = '2026-09-19T02:00:00Z'
        self.assertEqual(cutoff_date(row), date(2026, 9, 18))
        row['simulatedForecastAt'] = '2026-09-19T03:00:00Z'
        with self.assertRaises(ValueError):
            cutoff_date(row)

    def test_unknown_results_cannot_change_forecast_but_eligible_results_can(self):
        games, row = sample_games(), target()
        before, _, fits = forecast_league('PL', games, [row], self.plan['candidate'])
        late = copy.deepcopy(games[0])
        late.update(id=999, utcDate='2025-08-19T00:01:00Z')
        late['score']['fullTime'] = {'home': 12, 'away': 0}
        changed, _, changed_fits = forecast_league('PL', games + [late], [row], self.plan['candidate'])
        self.assertEqual(before, changed)
        self.assertEqual(fits, changed_fits)
        late['utcDate'] = '2025-08-18T23:59:00Z'
        earlier, _, earlier_fits = forecast_league('PL', games + [late], [row], self.plan['candidate'])
        self.assertEqual(earlier[0]['trainingMatches'], before[0]['trainingMatches'] + 1)
        self.assertNotEqual(earlier_fits[0]['trainingSha256'], fits[0]['trainingSha256'])
        self.assertNotEqual(earlier[0]['candidate'], before[0]['candidate'])
        self.assertLess(before[0]['latestTrainingDate'], before[0]['cutoff_date'])

    def test_fit_reused_per_cutoff_and_unknown_clubs_are_excluded(self):
        rows = [target(), target(fixture_id=1002), {**target(fixture_id=1003), 'homeId': 99}]
        predictions, excluded, fits = forecast_league('PL', sample_games(), rows, self.plan['candidate'])
        self.assertEqual(len(fits), 1)
        self.assertEqual(len(predictions), 2)
        self.assertEqual(excluded, [{'fixtureId': 1003, 'league': 'PL', 'reason': 'unseen_team'}])
        self.assertEqual(predictions[0]['candidate'], predictions[1]['candidate'])
        with self.assertRaises(ValueError):
            forecast_league('BSA', sample_games(), [target()], self.plan['candidate'])

    def sample_experiment(self):
        history, rows = {}, []
        for code in self.plan['leagues']:
            games = sample_games()
            for fixture_id, day in [(1001, '2026-08-20'), (1002, '2026-10-20')]:
                row = target(code, day, fixture_id)
                row['season'] = 2026
                rows.append(row)
                games.append({'id': fixture_id, 'utcDate': row['kickoff'], '__year': 2026,
                    'status': 'FINISHED', 'homeTeam': {'id': 1}, 'awayTeam': {'id': 2},
                    'score': {'duration': 'REGULAR', 'fullTime': {'home': 2, 'away': 0}}})
            history[code] = games
        packet = {'leagues': self.plan['leagues'], 'inputSha256': {code: sha(games) for code, games in history.items()}}
        replay = {'protocol': 'mixed-v1-replay-1', 'sourceCohortSha256': REGISTERED_SHA,
            'leadHours': 24, 'resultSafetyHours': 4, 'rows': rows,
            'sourceManifest': [{'league': code, 'inputSha256': packet['inputSha256'][code],
                                'rows': len(games)} for code, games in history.items()]}
        return history, packet, replay

    def test_leagues_are_isolated_even_when_team_ids_match_and_packet_is_immutable(self):
        history, packet, replay = self.sample_experiment()
        original = (DIRECTORY / 'packet.json').read_bytes()
        report, rows, fits = evaluate(replay, history, packet, self.plan)
        for game in history['BSA'][:220]:
            game['score']['fullTime'] = {'home': 4, 'away': 0}
        other_report, other_rows, other_fits = evaluate(replay, history, packet, self.plan)
        self.assertEqual([row for row in rows if row['league'] == 'PL'],
                         [row for row in other_rows if row['league'] == 'PL'])
        self.assertEqual([fit for fit in fits if fit['league'] == 'PL'],
                         [fit for fit in other_fits if fit['league'] == 'PL'])
        self.assertNotEqual([row['candidate'] for row in rows if row['league'] == 'BSA'],
                            [row['candidate'] for row in other_rows if row['league'] == 'BSA'])
        self.assertFalse(report['promotedToDefault'])
        self.assertTrue(other_report['prospectiveValidationRequired'])
        self.assertEqual((DIRECTORY / 'packet.json').read_bytes(), original)
        self.assertEqual(hashlib.sha256(original).hexdigest(), REGISTERED_SHA)

    def test_changed_recipe_or_fixture_identity_is_rejected(self):
        history, packet, replay = self.sample_experiment()
        replay['rows'][0]['homeId'] = 999
        with self.assertRaisesRegex(ValueError, 'identity'):
            evaluate(replay, history, packet, self.plan)
        history, packet, replay = self.sample_experiment()
        altered = copy.deepcopy(self.plan)
        altered['candidate']['glm']['ridge'] = 0.15
        with self.assertRaisesRegex(ValueError, 'recipe'):
            evaluate(replay, history, packet, altered)


if __name__ == '__main__':
    unittest.main()
