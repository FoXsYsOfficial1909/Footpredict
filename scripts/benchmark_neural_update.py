"""Reproduce the five-match neural update on two pinned, real data snapshots."""
import argparse
from datetime import date, timedelta
import gzip
import json
from pathlib import Path
import shutil
import tempfile
from time import perf_counter
from urllib.request import urlopen

from research.neural_experiment import PLAN, ROOT, train, verify_rows
from research.neural_feature_cache import from_snapshot, prepare_rows
from research.neural_features import build_rows
from research.nightly import sha

ARCHIVE = 'https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/f8230c392f96afca17e9cd6bef8c63d1d2b3fbf3'
DATA = '63097b3e330872357eef22b9145e9ac98ac39920ba8ab61d82bade714b3fe86f'
MODEL = '6cb03e572b3fd4a416d4cd4ac0ae397e3df5e56531605af1b9bd8297d4113755'


def artifact(directory, local, remote):
    if directory:
        return (directory / local).read_bytes()
    with urlopen(ARCHIVE + '/' + remote, timeout=40) as response:
        return response.read(4_000_001)


def benchmark(directory=None):
    plan = json.loads(PLAN.read_text())
    records = json.loads(gzip.decompress(artifact(directory, 'training-data.json.gz', 'datasets/' + DATA + '.json.gz')))
    model = json.loads(artifact(directory, 'model.json', 'models/' + MODEL + '.json'))
    if sha(records) != DATA or sha(model) != MODEL:
        raise ValueError('Pinned dataset or published model checksum differs')
    games = from_snapshot(records)
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        for name in ('feature-rows.json.gz', 'feature-rows-manifest.json'):
            shutil.copyfile(ROOT / 'docs/research/neural-2026-11-16' / name, cache / name)
        shutil.copyfile(ROOT / plan['sourceSnapshot'], cache / 'training-data.json.gz')
        start = perf_counter()
        full = build_rows(games)
        verify_rows(full, games, plan)
        full_time = perf_counter() - start
        start = perf_counter()
        rows, preparation = prepare_rows(games, plan, cache)
        incremental_time = perf_counter() - start
    if rows != full or preparation['reusedRows'] != 14002 or preparation['rebuiltRows'] != 5:
        raise ValueError('Incremental rows differ from reference or unexpected reuse')
    cutoff = (date.fromisoformat(model['asOf']) - timedelta(days=1)).isoformat()
    eligible = [r for r in rows if r['season'] >= 2016 and r['date'] < cutoff]
    start = perf_counter()
    fitted = train(eligible, cutoff, plan)
    fit_time = perf_counter() - start
    if fitted != model['network']:
        raise ValueError('Fitted network differs from the published model')
    return {'dataSha256': DATA, 'modelSha256': MODEL, 'sourceRows': len(records),
            'featureSha256': sha(rows), 'trainingRows': len(eligible), 'preparation': preparation,
            'fullPreparationSeconds': full_time, 'incrementalPreparationSeconds': incremental_time,
            'speedupPreparation': full_time / incremental_time, 'fitSeconds': fit_time,
            'featuresIdentical': True, 'modelWeightsIdenticalToPublished': True}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, help='Read the two pinned artifacts from a local trainer archive')
    print(json.dumps(benchmark(parser.parse_args().state_dir), indent=2))
