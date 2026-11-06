"""Publish corner models separately from existing goal and xG protocols."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from research.nightly import sha
from scripts.publish_training import git, save_public_model

BRANCH = 'corner-reports'
PROTOCOL = 'corner-candidate-1'


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('A corner report timezone is required')
    return parsed


def merge_latest(previous, attempt, *, repository, run_id, run_attempt, commit):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    if not str(run_id).isdigit() or not str(run_attempt).isdigit() or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid corner workflow metadata')
    if (attempt.get('schemaVersion') != 1 or attempt.get('protocol') != PROTOCOL
        or attempt.get('status') not in ('success', 'failed') or attempt.get('promotedToDefault') is not False
        or attempt.get('prospectiveValidationRequired') is not True):
        raise ValueError('Invalid corner attempt')
    generated = instant(attempt['generatedAt'])
    if attempt['status'] == 'success':
        if not attempt['latestTrainingMatch'] < attempt['trainingCutoffExclusive'] < attempt['asOf']:
            raise ValueError('Corner training data fence was not preserved')
        if (not re.fullmatch(r'[0-9a-f]{64}', attempt.get('modelSha256', ''))
            or not isinstance(attempt.get('modelVersion'), str)
            or attempt.get('evaluation', {}).get('commonMatches', 0) < 60):
            raise ValueError('Incomplete successful corner model evidence')
    previous = previous or {}
    if previous.get('latestAttempt') and instant(previous['latestAttempt']['generatedAt']) > generated:
        raise ValueError('Refusing to replace a newer corner attempt')
    report = {**attempt, 'repository': repository, 'runId': str(run_id), 'runAttempt': str(run_attempt),
              'sourceCommit': commit, 'runUrl': f'https://github.com/{repository}/actions/runs/{run_id}',
              'artifactsUrl': f'https://github.com/{repository}/actions/runs/{run_id}#artifacts'}
    if attempt['status'] == 'success':
        report['modelUrl'] = f'https://raw.githubusercontent.com/{repository}/{BRANCH}/models/{attempt["modelSha256"]}.json'
    return {'schemaVersion': 1, 'protocol': PROTOCOL, 'latestAttempt': report,
            'latestSuccessful': report if attempt['status'] == 'success' else previous.get('latestSuccessful')}


def publish(output, repository, run_id, run_attempt, commit):
    root = Path(git('rev-parse', '--show-toplevel'))
    output = Path(output).resolve()
    attempt = json.loads((output / 'attempt.json').read_text())
    if attempt['status'] == 'success':
        model = json.loads((output / 'model.json').read_text())
        if (sha(model) != attempt['modelSha256'] or model.get('target') != 'corners_total_regulation'
            or model.get('model_version') != attempt.get('modelVersion')
            or model.get('as_of') != attempt['trainingCutoffExclusive']):
            raise ValueError('Corner model does not match its published evidence')
    remote = git('ls-remote', '--heads', 'origin', 'refs/heads/' + BRANCH, cwd=root)
    if remote:
        git('fetch', '--no-tags', 'origin', BRANCH, cwd=root)
    temporary = Path(tempfile.mkdtemp(prefix='footpredict-corner-publish-'))
    worktree = temporary / 'report'; orphan = None
    try:
        git('worktree', 'add', '--detach', str(worktree), 'FETCH_HEAD' if remote else 'HEAD', cwd=root)
        if not remote:
            orphan = 'corner-publish-' + uuid.uuid4().hex
            git('checkout', '--orphan', orphan, cwd=worktree)
            git('rm', '-rf', '--ignore-unmatch', '.', cwd=worktree)
        latest = worktree / 'latest.json'
        previous = json.loads(latest.read_text()) if latest.exists() else None
        merged = merge_latest(previous, attempt, repository=repository,
                              run_id=run_id, run_attempt=run_attempt, commit=commit)
        if attempt['status'] == 'success':
            save_public_model(output, worktree, attempt)
        archive = worktree / 'runs' / f'{run_id}-{run_attempt}.json'
        archive.parent.mkdir(exist_ok=True)
        encoded = json.dumps(merged['latestAttempt'], ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        if archive.exists() and archive.read_text() != encoded:
            raise ValueError('Immutable corner attempt differs')
        archive.write_text(encoded)
        latest.write_text(json.dumps(merged, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        git('add', 'latest.json', 'runs', *(['models'] if attempt['status'] == 'success' else []), cwd=worktree)
        if not git('diff', '--cached', '--name-only', cwd=worktree):
            return
        git('-c', 'user.name=github-actions[bot]', '-c',
            'user.email=41898282+github-actions[bot]@users.noreply.github.com',
            'commit', '-m', f'Record corner candidate {run_id}/{run_attempt}', cwd=worktree)
        git('push', 'origin', 'HEAD:refs/heads/' + BRANCH, cwd=worktree)
    finally:
        if worktree.exists():
            git('worktree', 'remove', '--force', str(worktree), cwd=root)
        if orphan and git('branch', '--list', orphan, cwd=root):
            git('branch', '-D', orphan, cwd=root)
        temporary.rmdir()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    for name in ('repository', 'run-id', 'run-attempt', 'commit'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try:
        publish(args.output, args.repository, args.run_id, args.run_attempt, args.commit)
    except subprocess.CalledProcessError as error:
        raise SystemExit('Corner Git publication failed (exit ' + str(error.returncode) + '); previous public weights remain available.') from None
