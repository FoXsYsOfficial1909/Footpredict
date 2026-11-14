import json
from pathlib import Path
import shutil
import tempfile
import unittest

from research.championship import ROOT
from research.cross_division import PLAN, execute, load_snapshot, normalized
from research.nightly import sha


class CrossDivisionArchiveTests(unittest.TestCase):
    def test_snapshot_is_reproducible_and_byte_corruption_is_rejected(self):
        plan=json.loads(PLAN.read_text());source=ROOT/'docs/research/cross-division-2026-11-14'
        games,metadata=load_snapshot(source/'training-data.json.gz',plan)
        manifest=json.loads((source/'input-manifest.json').read_text())
        self.assertEqual(len(games),10532)
        self.assertEqual(sha(normalized(games)),manifest['dataSha256'])
        self.assertEqual(len(metadata),24)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ('training-data.json.gz','input-manifest.json'):shutil.copyfile(source/name,root/name)
            path=root/'training-data.json.gz';path.write_bytes(path.read_bytes()+b'broken')
            with self.assertRaisesRegex(ValueError,'checksum'):load_snapshot(path,plan)

    def test_experiment_cannot_replace_its_frozen_source_study(self):
        plan=json.loads(PLAN.read_text());source=ROOT/plan['sourceELCStudy']
        before=(source/'report.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'overwrite'):execute(source,e0_directory='not-used')
        self.assertEqual((source/'report.json').read_bytes(),before)


if __name__=='__main__':unittest.main()
