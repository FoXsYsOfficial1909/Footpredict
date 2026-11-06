import csv
from datetime import date
import io
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from research.data import Match
from research.performance import Performance
from research.xg_summary import load_summary, parse_summary_csv, REPOSITORY, REVISION


def row(**changes):
    return {'league': 'ENG-Premier League', 'season': '2526', 'season_id': '2025',
            'game_id': '28780', 'date': '2025-08-17 15:30:00',
            'home_team': 'Manchester United', 'away_team': 'Arsenal',
            'home_goals': '0', 'away_goals': '1', 'home_xg': '1.37653', 'away_xg': '1.3304',
            'is_result': 'True', 'has_data': 'True', 'url': 'https://understat.com/match/28780', **changes}


def body(rows):
    stream = io.StringIO(); writer = csv.DictWriter(stream, fieldnames=list(row()))
    writer.writeheader(); writer.writerows(rows)
    return stream.getvalue().encode()


class XgSummaryTests(unittest.TestCase):
    def test_completed_provider_summary_keeps_actual_ids_and_values(self):
        matches, stats = parse_summary_csv(body([row()]), 2025)
        self.assertEqual(matches[0]['home'], 'Man United')
        self.assertEqual(matches[0]['source_match_id'], 28780)
        self.assertEqual(matches[0]['home_xg'], 1.37653)
        self.assertEqual(stats['completedMatches'], 1)

    def test_future_and_missing_data_are_skipped_without_zero_imputation(self):
        future = row(is_result='False', has_data='False', home_goals='', away_goals='', home_xg='', away_xg='')
        missing = row(has_data='False', home_xg='', away_xg='')
        for item, counter in [(future, 'futureFixturesSkipped'), (missing, 'missingDataSkipped')]:
            matches, stats = parse_summary_csv(body([item]), 2025)
            self.assertEqual(matches, [])
            self.assertEqual(stats[counter], 1)

    def test_bad_raw_metadata_and_values_fail(self):
        for changes in ({'home_xg': ''}, {'away_xg': 'nan'}, {'home_goals': '0.5'},
                        {'is_result': 'yes'}, {'has_data': ''}, {'league': 'FRA-Ligue 1'},
                        {'season_id': '2024'}, {'season': '2425'}, {'date': '2024-08-17'},
                        {'url': 'https://understat.com/match/999'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parse_summary_csv(body([row(**changes)]), 2025)

    def test_duplicate_id_or_fixture_fails(self):
        for duplicate in (row(), row(game_id='28781', url='https://understat.com/match/28781'),
                          row(date='2025-08-18 19:00:00')):
            with self.assertRaises(ValueError):
                parse_summary_csv(body([row(), duplicate]), 2025)

    def test_reference_join_is_exact_and_labels_stay_immutable(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'eng_2526.csv').write_bytes(body([row()]))
            original = Performance(Match(date(2025, 8, 17), 2025, 'Man United', 'Arsenal', 0, 1), 10, 9, 4, 3)
            rows, manifest = load_summary(folder, [original])
            self.assertEqual(len(rows), 1)
            self.assertEqual(manifest['join']['matched'], 1)
            self.assertEqual(original.match.home_goals, 0)
            self.assertFalse(manifest['pinnedFilesVerified'])
            wrong = Performance(Match(date(2025, 8, 17), 2025, 'Man United', 'Arsenal', 2, 1), 10, 9, 4, 3)
            with self.assertRaises(ValueError):
                load_summary(folder, [wrong])
            with self.assertRaises(ValueError):
                load_summary(folder, [])

    def test_unregistered_or_incomplete_source_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'eng_2526.csv').write_bytes(body([row()]))
            with self.assertRaises(ValueError):
                load_summary(folder, source_manifest={'repo': 'other/repo', 'revision': 'main', 'files': []})

    def test_pinned_files_verify_bytes_and_reject_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            entries = []
            for season in range(2019, 2027):
                code = f'{season % 100:02d}{(season + 1) % 100:02d}'
                identifier = str(10000 + season)
                data = body([row(season=code, season_id=str(season), date=f'{season}-08-17 15:30:00',
                                 game_id=identifier, url='https://understat.com/match/' + identifier)])
                name = f'eng_{code}.csv'; Path(folder, name).write_bytes(data)
                path = 'data/raw/understat/eng/' + name
                entries.append({'path': path, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
                                'sha': hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest(),
                                'url': f'https://raw.githubusercontent.com/{REPOSITORY}/{REVISION}/' + path})
            manifest = {'repo': REPOSITORY, 'revision': REVISION, 'files': entries}
            matches, audit = load_summary(folder, source_manifest=manifest)
            self.assertEqual(len(matches), 8)
            self.assertTrue(audit['pinnedFilesVerified'])
            changed = Path(folder, 'eng_2526.csv')
            changed.write_bytes(changed.read_bytes().replace(b'1.37653', b'9.99999'))
            with self.assertRaises(ValueError):
                load_summary(folder, source_manifest=manifest)


if __name__ == '__main__':
    unittest.main()
