import copy
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.data import Match
from research.division_model import DivisionGame
from research.performance import Performance
from research.nightly import sha
from research.league_one_experiment import (ROOT,PLAN,DIRECTORY,validate_plan,validate_combined,
    load_inputs,load_cohorts,eligible,bridge_graph,forecast,score_block,execute,METHODS)


class LeagueOneExperimentTests(unittest.TestCase):
    def setUp(self):
        self.plan=json.loads(PLAN.read_text())

    @staticmethod
    def game(division,day,season,home,away,hg=1,ag=0):
        return DivisionGame(division,Performance(Match(day,season,home,away,hg,ag),
                                                12.,10.,float(1+hg),float(1+ag)))

    def sample(self):
        pools={'E0':list('ABCDEF'),'E1':list('TUVWXY'),'E2':list('LMNOPQ')};games=[]
        for year in (2021,2022,2023):
            for division,pool in pools.items():
                pool=list(pool)
                if year==2021:
                    if division=='E0':pool[-1]='Bridge01'
                    if division=='E1':pool[-1]='Bridge12'
                else:
                    if division=='E1':pool[-1]='Bridge01'
                    if division=='E2':pool[-1]='Bridge12'
                for i in range(40):
                    h=i%6;a=(i+1+i//6)%6
                    if h==a:a=(a+1)%6
                    games.append(self.game(division,date(year,8,1)+timedelta(days=i),year,
                                           pool[h],pool[a],i%4,(i*3+1)%3))
        return sorted(games,key=lambda g:(*g.observation.match.key,g.division))

    @staticmethod
    def row(day='2024-08-05'):
        return {'date':day,'cutoff_date':(date.fromisoformat(day)-timedelta(days=1)).isoformat(),
            'home':'T','away':'U','actual':0,'joint':[.5,.3,.2],'joint2':[.5,.3,.2],
            'shots':[.48,.3,.22],'v1':[.45,.3,.25],'base':[.4,.3,.3]}

    def test_future_e2_statistics_and_target_score_cannot_influence_a_forecast(self):
        games=self.sample();row=self.row();before,fits=forecast(games,[row],self.plan)
        day=date.fromisoformat(row['date'])
        future=[self.game('E2',day+timedelta(days=n),2024,'L','M',20,0) for n in (-1,0,1)]
        future.append(self.game('E1',day,2024,'T','U',0,20))
        changed={**row,'actual':2}
        after,other_fits=forecast(games+future,[changed],self.plan)
        self.assertEqual(before[0]['three'],after[0]['three'])
        self.assertEqual(before[0]['elo'],after[0]['elo'])
        self.assertEqual(before[0]['pastClubGames'],after[0]['pastClubGames'])
        self.assertEqual(fits,other_fits)
        self.assertNotEqual(before[0]['actual'],after[0]['actual'])
        self.assertLess(fits[0]['latestTrainingDate'],row['cutoff_date'])
        self.assertEqual(set(fits[0]['byDivision']),{'E0','E1','E2'})

    def test_bridge_connectivity_and_membership_conflicts_are_explicit(self):
        edges=bridge_graph(self.sample())
        self.assertIn('Bridge01',edges['E0-E1']);self.assertIn('Bridge12',edges['E1-E2'])
        independent=[self.game(d,date(2023,8,1),2023,d+'A',d+'B') for d in ('E0','E1','E2')]
        with self.assertRaises(ValueError):bridge_graph(independent)
        same_club=[self.game('E0',date(2023,8,1),2023,'A','B'),
                   self.game('E2',date(2023,8,2),2023,'A','C')]
        with self.assertRaisesRegex(ValueError,'conflicting divisions'):
            validate_combined(same_club,self.plan)

    def test_subgroup_boundaries_empty_scores_and_equal_forecast_coverage(self):
        self.assertTrue(eligible({'A':{'E0':0,'E1':19,'E2':20}}))
        self.assertFalse(eligible({'A':{'E0':0,'E1':20,'E2':20}}))
        self.assertFalse(eligible({'A':{'E0':0,'E1':19,'E2':19}}))
        rows=[{**self.row(),'three':[.55,.25,.2],'lowELCHistoryWithL1':False}]
        before=copy.deepcopy(rows);block=score_block(rows,self.plan)
        self.assertEqual({m['n'] for m in block['metrics'].values()},{1})
        empty=block['lowELCHistoryWithL1']
        self.assertEqual(empty['matches'],0);self.assertEqual(empty['evidence'],'no_applicable_fixtures')
        self.assertTrue(all(m['n']==0 and m['logLoss'] is None for m in empty['metrics'].values()))
        self.assertTrue(all(v is None for v in empty['pairedIntervals'].values()))
        self.assertEqual(rows,before)

    def test_pinned_sources_and_exact_previous_cohorts_are_preserved(self):
        games,_=load_inputs(self.plan);cohorts=load_cohorts(games,self.plan)
        self.assertEqual(len(games),14002)
        self.assertEqual({k:len(v) for k,v in cohorts.items()},{'validation':1061,'audit':533,'monitoring':159})
        for name,rows in cohorts.items():self.assertEqual(sha(rows),self.plan['sourcePredictionSha256'][name])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);old=root/self.plan['sourceJointStudy'];old.mkdir(parents=True)
            (old/'training-data.json.gz').write_bytes(b'corrupt')
            with patch('research.league_one_experiment.ROOT',root):
                with self.assertRaisesRegex(ValueError,'earlier joint study'):load_inputs(self.plan)
        for change in ({'candidateCount':2},{'subgroup':{'E1GamesExclusiveMaximum':30}},
                       {'divisions':['E0','E1']},{'automaticPromotion':True}):
            with self.assertRaises(ValueError):validate_plan({**self.plan,**change})
        for output in (ROOT/'docs/research',ROOT/self.plan['sourceJointStudy'],
                       ROOT/'docs/research/hundred-2026-11-11'):
            with self.assertRaisesRegex(ValueError,'overwrite'):execute(output)

    def test_saved_vectors_and_subgroups_reproduce_the_report_without_refitting(self):
        from research.quality_diagnostic import metrics
        report=json.loads((DIRECTORY/'report.json').read_text())
        for period,n in self.plan['expectedCommonMatches'].items():
            rows=json.loads((DIRECTORY/(period+'-predictions.json')).read_text())
            self.assertEqual(len(rows),n);self.assertEqual(sha(rows),report['predictionSha256'][period])
            self.assertTrue(all(r['joint2']==r['joint'] for r in rows))
            group=[r for r in rows if r['lowELCHistoryWithL1']]
            self.assertEqual(len(group),report['results'][period]['lowELCHistoryWithL1']['matches'])
            for method in METHODS:
                self.assertEqual(metrics(rows,method),report['results'][period]['metrics'][method])
                self.assertEqual(metrics(group,method),report['results'][period]['lowELCHistoryWithL1']['metrics'][method])
        self.assertFalse(report['independentAdvantageEstablished'])
        self.assertFalse(report['promotedToDefault']);self.assertFalse(report['existingFrozenPredictionsChanged'])


if __name__=='__main__':unittest.main()
