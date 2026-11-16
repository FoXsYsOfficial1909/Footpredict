from datetime import date
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from research.league_one_data import (CUTOFF, DIRECTORY, ROOT, REGISTERED_DATA_SHA256,
    audit, canonical_bytes, check_club_memberships, load_snapshot, normalized, parse_source, validate_rows)


class LeagueOneDataTests(unittest.TestCase):
    def test_registered_archive_keeps_all_results_and_one_incomplete_shot_observation(self):
        games, sources = load_snapshot()
        manifest = json.loads((DIRECTORY/'input-manifest.json').read_text())
        self.assertEqual(len(games),3470)
        self.assertEqual([s['sourceRows'] for s in sources],[552]*6+[158])
        self.assertEqual([s['acceptedRows'] for s in sources],[552]*6+[158])
        self.assertEqual(sum(g.observation.home_sot is not None and g.observation.away_sot is not None for g in games),3469)
        fallback=[g.observation for g in games if g.observation.home_sot is None or g.observation.away_sot is None]
        self.assertEqual(len(fallback),1);self.assertTrue(fallback[0].issues)
        self.assertTrue(all(type(v) is int for v in (fallback[0].match.home_goals,fallback[0].match.away_goals)))
        self.assertEqual(hashlib.sha256(canonical_bytes(normalized(games))).hexdigest(),REGISTERED_DATA_SHA256)
        self.assertEqual(max(g.observation.match.day for g in games),date(2026,11,4))
        self.assertTrue(all(g.observation.match.day<CUTOFF for g in games))
        self.assertEqual(check_club_memberships(games)['sameSeasonDivisionConflicts'],0)
        self.assertEqual(manifest['sparseELCClubHistory']['Bolton']['E2GamesIn1095DayWindow'],122)
        self.assertEqual(manifest['sparseELCClubHistory']['Lincoln']['E2GamesIn1095DayWindow'],122)

    def test_source_identity_score_and_duplicates_are_checked_before_statistics(self):
        header='Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HS,AS,HST,AST\n'
        valid='E2,04/11/2026,Bolton,Lincoln,2,1,H,12,8,4,3\n'
        observations,_=parse_source((header+valid).encode(),2026)
        self.assertEqual(len(observations),1)
        for changed in (valid.replace('E2','E1'),valid.replace(',H,',',D,'),valid.replace('2026','2024'),
                        valid+valid,valid+valid.replace('04/11','05/11')):
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                parse_source((header+changed).encode(),2026)
        with self.assertRaises(ValueError):parse_source((header+valid).encode(),2025)

    def test_inconsistent_and_missing_shots_preserve_actual_scores_and_missing_markers(self):
        body=b'Div,Date,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HS,AS,HST,AST\nE2,04/11/2026,Bolton,Lincoln,2,1,H,8,9,10,\n'
        rows,_=parse_source(body,2026);row=rows[0]
        self.assertEqual((row.match.home_goals,row.match.away_goals),(2,1))
        self.assertIsNone(row.home_shots);self.assertIsNone(row.home_sot);self.assertIsNone(row.away_sot)
        self.assertIn('home_shots:inconsistent',row.issues)

    def test_changed_snapshot_is_rejected_even_with_rewritten_local_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'study';shutil.copytree(DIRECTORY,root)
            raw=gzip.decompress((root/'training-data.json.gz').read_bytes())
            rows=json.loads(raw);rows[-1][5]+=1
            body=json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode();compressed=gzip.compress(body,mtime=0)
            manifest=json.loads((root/'input-manifest.json').read_text())
            manifest.update(compressedSha256=hashlib.sha256(compressed).hexdigest(),
                            uncompressedSha256=hashlib.sha256(body).hexdigest(),
                            dataSha256=hashlib.sha256(canonical_bytes(rows)).hexdigest())
            (root/'training-data.json.gz').write_bytes(compressed)
            (root/'input-manifest.json').write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):load_snapshot(root/'training-data.json.gz')

    def test_normalized_fixture_fences_and_protected_outputs(self):
        row=['E2','2026-11-04',2026,'Bolton','Lincoln',2,1,12.,8.,4.,3.,[]]
        self.assertEqual(len(validate_rows([row])),1)
        for changed in ([row,row],[['E1',*row[1:]]],[[row[0],'2026-11-14',*row[2:]]],
                        [[*row[:9],13.,row[10],row[11]]]):
            with self.subTest(changed=changed),self.assertRaises(ValueError):validate_rows(changed)
        for target in (ROOT/'docs/research',ROOT/'docs/research/hundred-2026-11-11'):
            with self.subTest(target=target),self.assertRaises(ValueError):audit('/missing/source',target)


if __name__=='__main__':unittest.main()
