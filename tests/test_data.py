import unittest
from datetime import date

from research.data import canonical, parse_csv, parse_day


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


if __name__=='__main__':unittest.main()
