"""Persist validated xG snapshots and refresh attempts on a dedicated branch."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from research.xg_data import load_xg_matches
from scripts.publish_training import git

BRANCH = 'xg-data-reports'


def read_state(directory):
    directory = Path(directory)
    body = (directory / 'xg-matches.json').read_bytes()
    success = json.loads((directory / 'success.json').read_text())
    rows = load_xg_matches(directory / 'xg-matches.json')
    keys = {(r['date'], r['home'], r['away']) for r in rows}
    digest = hashlib.sha256(body).hexdigest()
    if (success.get('schemaVersion') != 1 or success.get('status') != 'success'
        or success.get('normalizedMatchesSha256') != digest
        or success.get('matchCount') != len(rows) or len(keys) != len(rows)
        or len(rows) < 100 or success.get('dataLastMatchDate') != max(r['date'] for r in rows)):
        raise ValueError('Invalid accepted xG snapshot')
    instant(success['acceptedAt'])
    return body, success


def instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Expected an explicit report timezone')
    return result


def merge_latest(previous, report, success, *, repository, run_id, run_attempt, commit):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    if not str(run_id).isdigit() or not str(run_attempt).isdigit() or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid workflow metadata')
    digest = success.get('normalizedMatchesSha256', '')
    if (report.get('schemaVersion') != 1 or report.get('status') not in ('success', 'stale', 'failed')
        or report.get('promotedToDefault') is not False or not re.fullmatch(r'[0-9a-f]{64}', digest)
        or report.get('normalizedMatchesSha256') != digest
        or report.get('dataLastMatchDate') != success.get('dataLastMatchDate')):
        raise ValueError('Refresh report does not identify the accepted dataset')
    generated = instant(report['generatedAt'])
    if instant(success['acceptedAt']) > generated:
        raise ValueError('Accepted snapshot postdates the report')
    if previous and instant(previous['latestAttempt']['generatedAt']) > generated:
        raise ValueError('Refusing to replace a newer refresh report')
    accepted = (previous or {}).get('lastAccepted')
    if accepted and (success['matchCount'] < accepted['matchCount']
        or success['dataLastMatchDate'] < accepted['dataLastMatchDate']
        or instant(success['acceptedAt']) < instant(accepted['acceptedAt'])):
        raise ValueError('Refusing to regress the accepted xG history')
    raw = f'https://raw.githubusercontent.com/{repository}/{BRANCH}/'
    attempt = {**report, 'repository': repository, 'sourceCommit': commit,
        'runId': str(run_id), 'runAttempt': str(run_attempt),
        'runUrl': f'https://github.com/{repository}/actions/runs/{run_id}',
        'reportUrl': raw + f'runs/{run_id}-{run_attempt}.json'}
    return {'schemaVersion': 1, 'latestAttempt': attempt,
        'lastAccepted': {**success, 'datasetUrl': raw + f'datasets/{digest}.json'}}


def restore(state_dir):
    root = Path(git('rev-parse', '--show-toplevel'))
    if not git('ls-remote', '--heads', 'origin', 'refs/heads/' + BRANCH, cwd=root):
        return False
    git('fetch', '--no-tags', 'origin', BRANCH, cwd=root)
    with tempfile.TemporaryDirectory(prefix='footpredict-xg-restore-') as temporary:
        staging = Path(temporary)
        for name in ('xg-matches.json', 'success.json'):
            body = subprocess.run(['git', 'show', 'FETCH_HEAD:state/' + name], cwd=root,
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
            (staging / name).write_bytes(body)
        read_state(staging)
        destination = Path(state_dir)
        destination.mkdir(parents=True, exist_ok=True)
        for name in ('xg-matches.json', 'success.json'):
            target = destination / name
            target.with_suffix('.tmp').write_bytes((staging / name).read_bytes())
            target.with_suffix('.tmp').replace(target)
    return True


def publish(output, repository, run_id, run_attempt, commit):
    output = Path(output).resolve()
    body, success = read_state(output)
    report = json.loads((output / 'report.json').read_text())
    root = Path(git('rev-parse', '--show-toplevel'))
    remote = git('ls-remote', '--heads', 'origin', 'refs/heads/' + BRANCH, cwd=root)
    if remote:
        git('fetch', '--no-tags', 'origin', BRANCH, cwd=root)
    temporary = Path(tempfile.mkdtemp(prefix='footpredict-xg-publish-'))
    worktree = temporary / 'report'
    orphan = None
    try:
        git('worktree', 'add', '--detach', str(worktree), 'FETCH_HEAD' if remote else 'HEAD', cwd=root)
        if not remote:
            orphan = 'xg-publish-' + uuid.uuid4().hex
            git('checkout', '--orphan', orphan, cwd=worktree)
            git('rm', '-rf', '--ignore-unmatch', '.', cwd=worktree)
        latest = worktree / 'latest.json'
        previous = json.loads(latest.read_text()) if latest.exists() else None
        if previous:
            old_body, _ = read_state(worktree / 'state')
            current_rows = {(r['date'], r['home'], r['away']): r for r in json.loads(body)}
            for old_row in json.loads(old_body):
                if current_rows.get((old_row['date'], old_row['home'], old_row['away'])) != old_row:
                    raise ValueError('Previously accepted xG observation changed or disappeared')
        merged = merge_latest(previous, report, success, repository=repository,
            run_id=run_id, run_attempt=run_attempt, commit=commit)
        archive = worktree / 'datasets' / (success['normalizedMatchesSha256'] + '.json')
        archive.parent.mkdir(exist_ok=True)
        if archive.exists() and archive.read_bytes() != body:
            raise ValueError('Immutable xG snapshot differs')
        archive.write_bytes(body)
        runs = worktree / 'runs'; runs.mkdir(exist_ok=True)
        encoded = json.dumps(merged['latestAttempt'], ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        attempt = runs / f'{run_id}-{run_attempt}.json'
        if attempt.exists() and attempt.read_text() != encoded:
            raise ValueError('Immutable refresh attempt differs')
        attempt.write_text(encoded)
        latest.write_text(json.dumps(merged, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        state = worktree / 'state'; state.mkdir(exist_ok=True)
        (state / 'xg-matches.json').write_bytes(body)
        (state / 'success.json').write_bytes((output / 'success.json').read_bytes())
        git('add', 'latest.json', 'datasets', 'runs', 'state', cwd=worktree)
        if not git('diff', '--cached', '--name-only', cwd=worktree):
            return
        git('-c', 'user.name=github-actions[bot]', '-c',
            'user.email=41898282+github-actions[bot]@users.noreply.github.com',
            'commit', '-m', f'Record xG data check {run_id}/{run_attempt}', cwd=worktree)
        git('push', 'origin', 'HEAD:refs/heads/' + BRANCH, cwd=worktree)
    finally:
        if worktree.exists():
            git('worktree', 'remove', '--force', str(worktree), cwd=root)
        if orphan and git('branch', '--list', orphan, cwd=root):
            git('branch', '-D', orphan, cwd=root)
        temporary.rmdir()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    restore_parser = commands.add_parser('restore')
    restore_parser.add_argument('--state-dir', required=True, type=Path)
    publish_parser = commands.add_parser('publish')
    publish_parser.add_argument('--output', required=True, type=Path)
    for name in ('repository', 'run-id', 'run-attempt', 'commit'):
        publish_parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'restore':
            print(json.dumps({'restored': restore(args.state_dir)}))
        else:
            publish(args.output, args.repository, args.run_id, args.run_attempt, args.commit)
    except subprocess.CalledProcessError as error:
        raise SystemExit('xG Git operation failed (exit ' + str(error.returncode) + '); previous published data are preserved.') from None
