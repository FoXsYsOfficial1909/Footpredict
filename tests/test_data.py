import unittest
from datetime import date
from pathlib import Path
import tempfile
from unittest.mock import patch

from research.data import canonical, download_season, parse_csv, parse_day


class DataValidationTests(unittest.TestCase):
    def csv(self, row):
        return ('Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,B365H\n'+row+'\n').encode()

    def test_only_match_outcome_columns_are_loaded(self):
        matches=parse_csv(self.csv('24/10/2026,Chelsea,Tottenham,0,2,A,999999'),2026)
        self.assertEqual(matches[0].outcome,2)
        self.assertEqual(set(matches[0].__dict__),{'day','season','home','away','home_goals','away_goals'})

    def test_reject_result_disagreement(self):
        with self.assertRaises(ValueError):parse_csv(self.csv('24/10/2026,Chelsea,Tottenham,0,2,H,1.5'),2026)

    def test_reject_duplicate(self):
        body=self.csv('24/10/2026,Chelsea,Tottenham,0,2,A,1.5')+b'24/10/2026,Chelsea,Tottenham,0,2,A,2.0\n'
        with self.assertRaises(ValueError):parse_csv(body,2026)

    def test_season_bounds_and_negative_score(self):
        for row in ('24/10/2024,Chelsea,Tottenham,0,2,A,1.5','24/10/2026,Chelsea,Tottenham,-1,2,A,1.5'):
            with self.assertRaises(ValueError):parse_csv(self.csv(row),2026)

    def test_two_digit_dates_and_team_aliases(self):
        self.assertEqual(parse_day('15/08/15'),date(2015,8,15))
        self.assertEqual(canonical('Manchester United FC'),'Man United')

    def test_declared_division_is_required_and_mixed_leagues_are_rejected(self):
        body=b'Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\nE1,24/10/2026,Derby,Preston,2,1,H\n'
        self.assertEqual(parse_csv(body,2026,division='E1')[0].home,'Derby')
        with self.assertRaises(ValueError):parse_csv(body,2026,division='E0')
        with self.assertRaises(ValueError):parse_csv(self.csv('24/10/2026,Derby,Preston,2,1,H,1.5'),2026,division='E1')
        with self.assertRaises(ValueError):parse_csv(body+b'E0,25/10/2026,Chelsea,Arsenal,2,1,H\n',2026,division='E1')

    def test_cached_divisions_are_separate_and_invalid_divisions_never_make_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for division in ('E0','E1'):
                (root/f'2026-{division}.csv').write_text(
                    f'Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR\n{division},24/10/2026,Home,Away,2,1,H\n')
            with patch('research.data.urllib.request.urlopen') as request:
                for division in ('E0','E1'):
                    _,manifest=download_season(2026,root,division=division)
                    self.assertEqual(manifest['division'],division)
                    self.assertTrue(manifest['url'].endswith('/'+division+'.csv'))
                with self.assertRaises(ValueError):download_season(2026,root,division='../E1')
                request.assert_not_called()


if __name__=='__main__':unittest.main()
