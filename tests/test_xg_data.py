from dataclasses import replace
from datetime import date
import unittest

from research.data import Match
from research.performance import Performance
from research.xg_data import aggregate_shots, merge_xg


def shot(identifier=1, **changes):
    return {'league': 'EPL', 'id': identifier, 'match_id': 101, 'xG': .2,
            'h_a': 'h', 'date': '2024-08-16 19:00:00', 'season': 2024,
            'home_team': 'Manchester United', 'away_team': 'Fulham',
            'home_goals': 1, 'away_goals': 0, **changes}


class XgDataTests(unittest.TestCase):
    def test_aggregation_canonicalizes_teams_and_deduplicates_shots(self):
        rows, manifest = aggregate_shots([shot(), shot(), shot(2, xG=.3), shot(3, h_a='a', xG=.7)])
        self.assertEqual(rows[0]['home'], 'Man United')
        self.assertAlmostEqual(rows[0]['home_xg'], .5)
        self.assertAlmostEqual(rows[0]['away_xg'], .7)
        self.assertEqual(manifest['identicalDuplicateShots'], 1)

    def test_conflicting_shots_or_metadata_never_silently_overwrite(self):
        for rows in ([shot(), shot(xG=.4)], [shot(), shot(2, home_goals=2)],
                     [shot(), shot(2, date='2024-08-17 19:00:00')]):
            with self.assertRaises(ValueError):
                aggregate_shots(rows)

    def test_later_archive_side_column_is_explicitly_supported(self):
        rows, _ = aggregate_shots([shot(h_a=None, home_away='a')])
        self.assertEqual(rows[0]['away_xg'], .2)
        with self.assertRaises(ValueError):
            aggregate_shots([shot(h_a='h', home_away='a')])

    def test_invalid_values_and_wrong_season_fail_closed(self):
        for changes in ({'xG': float('nan')}, {'xG': -1}, {'xG': 1.2},
                        {'h_a': 'home'}, {'home_goals': 1.5}, {'season': 2022}):
            with self.assertRaises(ValueError):
                aggregate_shots([shot(**changes)])

    def test_join_keeps_scores_and_only_uses_the_selected_provider(self):
        rows, _ = aggregate_shots([shot()])
        game = Match(date(2024, 8, 16), 2024, 'Man United', 'Fulham', 1, 0)
        original = Performance(game, 10, 8, 4, 2, 9., 8.)
        missing = replace(original, match=replace(game, day=date(2024, 8, 17)))
        merged, manifest = merge_xg([original, missing], rows)
        self.assertIs(merged[0].match, game)
        self.assertEqual(merged[0].home_xg, .2)
        self.assertEqual(merged[0].away_xg, 0.)
        self.assertIsNone(merged[1].home_xg)
        self.assertEqual(original.home_xg, 9.)
        self.assertEqual(manifest['matched'], 1)

    def test_score_conflict_is_rejected_and_can_only_be_audited_as_missing(self):
        rows, _ = aggregate_shots([shot()])
        original = Performance(Match(date(2024, 8, 16), 2024, 'Man United', 'Fulham', 0, 1), 1, 1, 1, 1)
        with self.assertRaises(ValueError):
            merge_xg([original], rows)
        merged, manifest = merge_xg([original], rows, strict=False)
        self.assertIsNone(merged[0].home_xg)
        self.assertEqual(len(manifest['scoreConflicts']), 1)

    def test_missing_whole_fixtures_are_not_zero_xg_and_duplicates_fail(self):
        original = Performance(Match(date(2024, 8, 16), 2024, 'Man United', 'Fulham', 1, 0), 1, 1, 1, 1)
        merged, _ = merge_xg([original], [])
        self.assertIsNone(merged[0].away_xg)
        rows, _ = aggregate_shots([shot()])
        with self.assertRaises(ValueError):
            merge_xg([original], rows + rows)
        with self.assertRaises(ValueError):
            merge_xg([original, original], rows)


if __name__ == '__main__':
    unittest.main()
