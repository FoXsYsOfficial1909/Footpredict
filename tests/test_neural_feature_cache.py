import copy
from dataclasses import replace
from datetime import date,timedelta
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.data import Match
from research.division_model import DivisionGame
from research.neural_experiment import PLAN,ROOT,FEATURE_CODE
from research.neural_feature_cache import prepare_rows,normalized,from_snapshot,implementation_sha
from research.neural_features import build_rows,FEATURE_NAMES
from research.nightly import sha
from research.performance import Performance


class NeuralFeatureCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.plan=json.loads(PLAN.read_text())

    def games(self):
        games=[]
        for d in ('E0','E1','E2'):
            for i in range(30):
                day=date(2023,8,1)+timedelta(days=i*4)
                home,away=('A','B') if i%2==0 else ('B','A')
                games.append(DivisionGame(d,Performance(Match(day,2023,home,away,i%4,(i+1)%3),
                    10.+i%5,9.+i%4,4.+i%2,3.+i%3,issues=('missing_xg',))))
        return sorted(games,key=lambda g:(*g.observation.match.key,g.division))

    def save(self,directory,games,*,incremental=False):
        path=Path(directory);rows=build_rows(games);body=gzip.compress(json.dumps(rows).encode(),mtime=0)
        (path/'feature-rows.json.gz').write_bytes(body)
        (path/'training-data.json.gz').write_bytes(gzip.compress(json.dumps(normalized(games)).encode(),mtime=0))
        manifest={'protocol':'neural-features-1','featureNames':list(FEATURE_NAMES),
            'sourceDataSha256':sha(normalized(games)),'dataSha256':sha(rows),
            'compressedSha256':hashlib.sha256(body).hexdigest(),
            'codeHashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in FEATURE_CODE}}
        if incremental:manifest['incrementalCodeSha256']=implementation_sha()
        (path/'feature-rows-manifest.json').write_text(json.dumps(manifest))
        return rows

    def test_append_reuses_old_rows_and_rebuilds_only_affected_league(self):
        games=self.games();latest=games[-1]
        added=replace(latest,observation=replace(latest.observation,
            match=replace(latest.observation.match,day=latest.observation.match.day+timedelta(days=4))))
        with tempfile.TemporaryDirectory() as d:
            before=self.save(d,games);current=games+[added]
            rows,info=prepare_rows(current,self.plan,d)
            self.assertEqual(rows,build_rows(current));self.assertEqual(rows[:-1],before)
            self.assertEqual(info['rebuiltRows'],1);self.assertEqual(info['reusedRows'],90)
            self.assertEqual(info['invalidatedFrom'],{'E2':added.observation.match.day.isoformat()})

    def test_corrections_and_removed_matches_rebuild_future_dependencies_exactly(self):
        games=self.games();index=31;game=games[index];obs=game.observation
        changes=[games[:index]+[replace(game,observation=replace(obs,
                    match=replace(obs.match,home_goals=9,away_goals=0)))]+games[index+1:],
                 games[:index]+[replace(game,observation=replace(obs,home_shots=None,home_sot=None))]+games[index+1:],
                 games[:index]+games[index+1:]]
        with tempfile.TemporaryDirectory() as d:
            original=self.save(d,games)
            for current in changes:
                with self.subTest(size=len(current)):
                    rows,info=prepare_rows(current,self.plan,d)
                    self.assertEqual(rows,build_rows(current))
                    self.assertEqual(info['invalidatedFrom'],{'E1':obs.match.day.isoformat()})
                    self.assertEqual([r for r in rows if r['division']!='E1'],[r for r in original if r['division']!='E1'])
                    self.assertGreater(info['rebuiltRows'],1)

    def test_reschedules_renames_and_division_changes_invalidate_old_and_new_identity(self):
        games=self.games();index=31;game=games[index];obs=game.observation
        changed=[replace(game,observation=replace(obs,match=replace(obs.match,day=obs.match.day-timedelta(days=2)))),
                 replace(game,observation=replace(obs,match=replace(obs.match,day=obs.match.day+timedelta(days=2)))),
                 replace(game,observation=replace(obs,match=replace(obs.match,home='C'))),
                 replace(game,division='E0',observation=replace(obs,match=replace(obs.match,home='C',away='D')))]
        with tempfile.TemporaryDirectory() as d:
            self.save(d,games)
            for replacement in changed:
                current=games[:index]+[replacement]+games[index+1:]
                rows,info=prepare_rows(current,self.plan,d)
                self.assertEqual(rows,build_rows(current))
                self.assertEqual(set(info['invalidatedFrom']),{'E1',replacement.division})
                self.assertLessEqual(info['invalidatedFrom']['E1'],obs.match.day.isoformat())

    def test_same_day_previous_day_and_future_corrections_do_not_leak(self):
        games=self.games();day=games[-1].observation.match.day
        target=DivisionGame('E2',Performance(Match(day,2023,'C','D',1,1),12.,11.,4.,3.))
        previous=replace(target,observation=replace(target.observation,match=replace(target.observation.match,
                         day=day-timedelta(days=1),home='E',away='F',home_goals=20)))
        future=replace(target,observation=replace(target.observation,match=replace(target.observation.match,
                       day=day+timedelta(days=1),home_goals=0,away_goals=20)))
        with tempfile.TemporaryDirectory() as d:
            self.save(d,games)
            rows,_=prepare_rows(games+[target,previous,future],self.plan,d)
            self.assertEqual(rows,build_rows(games+[target,previous,future]))
            expected=next(r for r in build_rows(games+[target]) if r['home']=='C')
            actual=next(r for r in rows if r['home']=='C' and r['date']==day.isoformat())
            self.assertEqual(actual,expected)

    def test_unchanged_inputs_skip_all_feature_rebuilds(self):
        games=self.games()
        with tempfile.TemporaryDirectory() as d:
            expected=self.save(d,games,incremental=True)
            with patch('research.neural_feature_cache.selected_rows',return_value=[]) as selected:
                actual,info=prepare_rows(list(reversed(games)),self.plan,d)
            self.assertEqual(actual,expected);self.assertEqual(info['mode'],'reused')
            self.assertEqual(info['rebuiltRows'],0);self.assertEqual(selected.call_args.args[1],set())

    def test_cache_corruption_missing_files_and_code_changes_fall_back_to_full(self):
        games=self.games()
        with tempfile.TemporaryDirectory() as d:
            path=Path(d);expected=build_rows(games)
            empty,info=prepare_rows(games,self.plan,d);self.assertEqual(empty,expected);self.assertEqual(info['mode'],'full')
            for mutation in ('feature_bytes','source_bytes','manifest_list','source_digest','feature_digest','source_row',
                             'feature_code','incremental_code','missing_features'):
                with self.subTest(mutation=mutation):
                    self.save(d,games)
                    manifest=json.loads((path/'feature-rows-manifest.json').read_text())
                    if mutation=='feature_bytes':(path/'feature-rows.json.gz').write_bytes(b'broken gzip')
                    if mutation=='source_bytes':(path/'training-data.json.gz').write_bytes(b'broken gzip')
                    if mutation=='source_row':
                        records=normalized(games);records[0][6]=30
                        (path/'training-data.json.gz').write_bytes(gzip.compress(json.dumps(records).encode()))
                    if mutation=='source_digest':manifest['sourceDataSha256']='0'*64
                    if mutation=='feature_digest':manifest['dataSha256']='0'*64
                    if mutation=='feature_code':manifest['codeHashes']['research/neural_features.py']='0'*64
                    if mutation=='incremental_code':manifest['incrementalCodeSha256']='0'*64
                    if mutation=='manifest_list':manifest=[]
                    if mutation=='missing_features':(path/'feature-rows.json.gz').unlink()
                    (path/'feature-rows-manifest.json').write_text(json.dumps(manifest))
                    rows,info=prepare_rows(games,self.plan,d)
                    self.assertEqual(rows,expected);self.assertEqual(info['mode'],'full');self.assertEqual(info['reason'],'cache_rejected')

    def test_new_invalid_inputs_are_rejected_instead_of_masked_by_cache_fallback(self):
        games=self.games()
        with tempfile.TemporaryDirectory() as d:
            self.save(d,games)
            with self.assertRaises(ValueError):prepare_rows(games+[games[0]],self.plan,d)
            bad=replace(games[0],observation=replace(games[0].observation,home_sot=99.))
            with self.assertRaises(ValueError):prepare_rows([bad]+games[1:],self.plan,d)
        self.assertEqual(from_snapshot(normalized(games)),games)


if __name__=='__main__':unittest.main()
