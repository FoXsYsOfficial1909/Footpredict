import csv
from datetime import date
import io
import json
import tempfile
from pathlib import Path
import unittest

from research.data import Match
from research.performance import Performance
from research.xg_recent import parse_recent_csv, load_recent, merge_sources


def csv_body(**changes):
    row = {'Date': '2025-08-17', 'Comp': 'Premier League', 'Venue': '0',
           'GF': '1', 'GA': '0', 'Opponent': 'Manchester Utd',
           'XG_for': '1.3304', 'XG_against': '1.37653',
           'XG_for_last3': '999999', 'simulated_prediction': 'future-secret'}
    row.update(changes)
    output = io.StringIO(); writer = csv.DictWriter(output, fieldnames=list(row))
    writer.writeheader(); writer.writerow(row)
    return output.getvalue().encode()


class RecentXgTests(unittest.TestCase):
    def test_raw_away_fields_are_oriented_and_external_features_ignored(self):
        first = parse_recent_csv(csv_body(), 'Arsenal')[0]
        after = parse_recent_csv(csv_body(XG_for_last3='NaN', simulated_prediction='anything'), 'Arsenal')[0]
        self.assertEqual(first, after)
        self.assertEqual((first['home'], first['away']), ('Man United', 'Arsenal'))
        self.assertEqual((first['home_goals'], first['away_goals']), (0, 1))
        self.assertEqual((first['home_xg'], first['away_xg']), (1.37653, 1.3304))
        self.assertIsNone(first['source_match_id'])

    def test_bad_raw_dates_venue_scores_and_xg_are_rejected(self):
        for change in [{'Date': '17/08/2025'}, {'Venue': '2'}, {'GF': '1.5'},
                       {'XG_for': 'NaN'}, {'XG_against': '-1'}, {'Opponent': 'Arsenal'}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_recent_csv(csv_body(**change), 'Arsenal')

    def test_reverse_team_copies_agree_but_conflicts_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            (p / 'Arsenal.csv').write_bytes(csv_body())
            reverse = {'Venue': '1', 'GF': '0', 'GA': '1', 'Opponent': 'Arsenal',
                       'XG_for': '1.37653', 'XG_against': '1.3304'}
            (p / 'Manchester Utd.csv').write_bytes(csv_body(**reverse))
            rows, manifest = load_recent(p)
            self.assertEqual(len(rows), 1); self.assertEqual(manifest['bothTeamCopies'], 1)
            self.assertFalse(manifest['pinnedFilesVerified'])
            (p / 'Manchester Utd.csv').write_bytes(csv_body(**{**reverse, 'XG_for': '1.9'}))
            with self.assertRaisesRegex(ValueError, 'Conflicting'):
                load_recent(p)

    def test_reference_date_and_score_must_match_exactly(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory); (p / 'Arsenal.csv').write_bytes(csv_body())
            match = Match(date(2025, 8, 17), 2025, 'Man United', 'Arsenal', 0, 1)
            original = Performance(match, None, None, None, None)
            rows, _ = load_recent(p, [original])
            self.assertEqual(rows[0]['home_goals'], original.match.home_goals)
            with self.assertRaisesRegex(ValueError, 'no exact reference'):
                load_recent(p, [])
            wrong = Performance(Match(match.day, 2025, match.home, match.away, 2, 1), None, None, None, None)
            with self.assertRaisesRegex(ValueError, 'original score'):
                load_recent(p, [wrong])

    def test_merge_keeps_primary_values_and_rejects_disagreement(self):
        recent = parse_recent_csv(csv_body(), 'Arsenal')[0]
        primary = {**recent, 'source_match_id': 28500, 'home_xg': recent['home_xg'] + 1e-6}
        merged, manifest = merge_sources([primary], [recent])
        self.assertEqual(merged, [primary]); self.assertEqual(manifest['overlappingMatches'], 1)
        self.assertEqual(manifest['primaryMatches'], 1)
        for change in [{'away_xg': .5}, {'away_goals': 2}, {'season': 2024}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                merge_sources([primary], [{**recent, **change}])
        added = {**recent, 'date': '2025-09-17'}
        merged, manifest = merge_sources([primary], [recent, added])
        self.assertEqual(len(merged), 2); self.assertEqual(manifest['recentOnlyMatches'], 1)
        self.assertLess(merged[0]['date'], merged[1]['date'])

    def test_duplicate_fixture_within_one_team_file_is_rejected(self):
        body = csv_body(); lines = body.splitlines()
        with self.assertRaisesRegex(ValueError, 'Duplicate fixture'):
            parse_recent_csv(b'\n'.join(lines + [lines[-1]]), 'Arsenal')

    def test_pinned_source_set_and_duplicate_raw_headers_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory); (p / 'Arsenal.csv').write_bytes(csv_body())
            (p / 'manifest.json').write_text(json.dumps([]))
            with self.assertRaisesRegex(ValueError, 'twenty team CSVs'):
                load_recent(p, source_manifest=p / 'manifest.json')
        body = csv_body(); header, row = body.splitlines()
        with self.assertRaisesRegex(ValueError, 'Duplicate.*header'):
            parse_recent_csv(header + b',GF\n' + row + b',2\n', 'Arsenal')


if __name__ == '__main__':
    unittest.main()
