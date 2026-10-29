import copy
import unittest

from research.features import feature_row


def snapshot():
    team = {'name': 'Chelsea FC', 'form': [{'kickoff': '2026-10-25T14:00:00Z', 'goalsFor': 2, 'goalsAgainst': 1}],
            'home': {'games': 1, 'goalsFor': 2, 'goalsAgainst': 1}, 'away': {'games': 0},
            'injuries': {'status': 'unavailable', 'reports': []}, 'calendar': {'gapDays': 3, 'coverage': 'partial'},
            'lineup': {'status': 'not_available', 'players': []}}
    return {'fixture': {'id': 560626, 'competition': 'PL', 'home': 'Chelsea FC', 'away': 'Manchester United FC', 'kickoff': '2026-10-31T12:30:00Z'},
            'collectedAt': '2026-10-27T19:00:00Z', 'featureCutoffAt': '2026-10-27T19:00:00Z',
            'teams': [team, {**copy.deepcopy(team), 'name': 'Manchester United FC'}], 'articles': [], 'ai': {'status': 'budget_reached', 'facts': []},
            'candidate': {'status': 'experimental', 'modelVersion': 'v2-test', 'probabilities': [.4, .3, .3],
                          'generatedAt': '2026-10-27T19:00:00Z', 'asOf': '2026-10-27', 'newsUsedInProbabilities': False}}


class EvidenceFeatureTests(unittest.TestCase):
    def test_source_reports_are_counted_without_turning_them_into_confirmed_injuries(self):
        packet=snapshot()
        packet['articles']=[{'id':'official:one','team':'Chelsea FC','url':'https://www.chelseafc.com/en/news/article/smith-training','title':'Alex Smith returns to training','summary':'Alex Smith has returned to team training.',
                             'publishedAt':'2026-10-27T12:00:00Z','fetchedAt':'2026-10-27T18:00:00Z'}]
        packet['availabilityReports']=[{'sourceId':'official:one','team':'Chelsea FC','sourceType':'official_club','quote':'Alex Smith has returned to team training.'}]
        row=feature_row(packet)
        self.assertEqual(row['features']['home_source_availability_reports'],1)
        self.assertEqual(row['features']['home_official_availability_reports'],1)
        self.assertIsNone(row['features']['home_injury_reports'])
        packet['availabilityReports'][0]['quote']='Alex Smith is fit for the next match.'
        with self.assertRaisesRegex(ValueError,'not grounded'):feature_row(packet)

    def test_missing_availability_stays_null_and_target_is_separate(self):
        row = feature_row(snapshot())
        self.assertIsNone(row['features']['home_injury_reports'])
        self.assertIsNone(row['features']['home_lineup_players'])
        self.assertIsNone(row['features']['home_availability_mentions'])
        self.assertFalse(row['features']['home_calendar_complete'])
        self.assertEqual(row['features']['home_form_points'], 3)
        self.assertIsNone(row['target'])
        self.assertNotIn('outcome', row['features'])

    def test_source_returning_no_reports_is_distinguished_from_unavailable(self):
        packet = snapshot()
        packet['teams'][0]['injuries']['status'] = 'no_reports_found'
        row = feature_row(packet)
        self.assertEqual(row['features']['home_injury_reports'], 0)
        self.assertTrue(row['features']['home_injury_source_available'])

    def test_future_news_and_late_forecasts_are_rejected(self):
        packet = snapshot()
        packet['articles'] = [{'publishedAt': '2026-10-28T19:00:00Z', 'fetchedAt': packet['collectedAt']}]
        with self.assertRaisesRegex(ValueError, 'News'): feature_row(packet)
        packet = snapshot()
        packet['candidate']['generatedAt'] = '2026-11-01T12:00:00Z'
        with self.assertRaisesRegex(ValueError, 'Forecast'): feature_row(packet)
        packet = snapshot()
        packet['teams'][0]['form'][0]['kickoff'] = '2026-10-27T18:00:00Z'
        with self.assertRaisesRegex(ValueError, 'four-hour'): feature_row(packet)

    def test_snapshot_identity_and_forecast_are_stable(self):
        packet = snapshot()
        first, second = feature_row(packet), feature_row(copy.deepcopy(packet))
        self.assertEqual(first, second)
        self.assertEqual(len(first['snapshot_sha256']), 64)
        packet['teams'][0]['injuries']['status'] = 'no_reports_found'
        self.assertNotEqual(first['snapshot_sha256'], feature_row(packet)['snapshot_sha256'])


if __name__ == '__main__': unittest.main()
