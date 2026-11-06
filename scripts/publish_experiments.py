"""Publish experiment checks and immutable results without changing forecasts."""
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
from scripts.publish_training import git

BRANCH = 'experiment-reports'
PROTOCOL = 'automatic-experiments-1'
STATE_FILE = 'last-success.json'


def instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('An explicit report timezone is required')
    return result


def validate_report(report):
    if (report.get('schemaVersion') != 1 or report.get('protocol') != PROTOCOL
        or report.get('status') not in ('success', 'unchanged', 'failed')
        or report.get('promotedToDefault') is not False
        or report.get('prospectiveValidationRequired') is not True):
        raise ValueError('Invalid experiment report')
    checked = instant(report['generatedAt'])
    if report['status'] != 'failed':
        for key in ('inputFingerprint', 'dataSha256', 'planSha256', 'codeSha256'):
            if not re.fullmatch(r'[0-9a-f]{64}', report.get(key, '')):
                raise ValueError('Missing experiment identity: ' + key)
        if instant(report['evaluatedAt']) > checked:
            raise ValueError('Experiment evaluation postdates its report')
    return report


def read_state(directory, *, optional=False):
    path = Path(directory) / STATE_FILE
    if optional and not path.exists():
        return None
    state = json.loads(path.read_text())
    report = validate_report(state['report'])
    if (state.get('schemaVersion') != 1 or report['status'] != 'success'
        or state.get('inputFingerprint') != report['inputFingerprint']
        or state.get('reportSha256') != sha(report)):
        raise ValueError('Invalid successful experiment state')
    return state


def merge_latest(previous, report, state, *, repository, run_id, run_attempt, commit):
    validate_report(report)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Invalid repository')
    if not str(run_id).isdigit() or not str(run_attempt).isdigit() or not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Invalid workflow metadata')
    previous = previous or {}
    old_attempt = previous.get('latestAttempt')
    if old_attempt and instant(old_attempt['generatedAt']) > instant(report['generatedAt']):
        raise ValueError('Refusing to replace a newer experiment report')
    last = previous.get('latestSuccessful')
    if report['status'] == 'success' and last is not None and instant(report['evaluatedAt']) < instant(last['evaluatedAt']):
        raise ValueError('Refusing to replace a newer scientific evaluation')
    if report['status'] in ('success', 'unchanged'):
        if state is None or state['inputFingerprint'] != report['inputFingerprint']:
            raise ValueError('Experiment report has no matching successful state')
        if report['status'] == 'success' and state['reportSha256'] != sha(report):
            raise ValueError('Successful state differs from the published result')
        if report['status'] == 'unchanged' and report['evaluatedAt'] != state['report']['evaluatedAt']:
            raise ValueError('An unchanged check cannot change the evaluation timestamp')
        if report['status'] == 'unchanged' and last is not None:
            if (last['inputFingerprint'] != state['inputFingerprint']
                or last['reportSha256'] != state['reportSha256']):
                raise ValueError('An unchanged check cannot replace the last evaluation')
    checksum = sha(report)
    raw = f'https://raw.githubusercontent.com/{repository}/{BRANCH}/'
    attempt = {**report, 'repository': repository, 'runId': str(run_id),
        'runAttempt': str(run_attempt), 'sourceCommit': commit,
        'runUrl': f'https://github.com/{repository}/actions/runs/{run_id}',
        'artifactsUrl': f'https://github.com/{repository}/actions/runs/{run_id}#artifacts',
        'reportSha256': checksum, 'reportUrl': raw + f'reports/{checksum}.json'}
    if report['status'] == 'success':
        last = attempt
    elif report['status'] == 'unchanged' and last is None:
        # A manually restored valid state can bootstrap publication, but its
        # original scientific timestamp is preserved and no run is invented.
        checksum = state['reportSha256']
        last = {**state['report'], 'reportSha256': checksum,
                'reportUrl': raw + f'reports/{checksum}.json'}
    return {'schemaVersion': 1, 'protocol': PROTOCOL,
            'latestAttempt': attempt, 'latestSuccessful': last}


def restore(state_dir):
    root = Path(git('rev-parse', '--show-toplevel'))
    if not git('ls-remote', '--heads', 'origin', 'refs/heads/' + BRANCH, cwd=root):
        return False
    git('fetch', '--no-tags', 'origin', BRANCH, cwd=root)
    paths = git('ls-tree', '--name-only', 'FETCH_HEAD', '--', 'state/' + STATE_FILE, cwd=root)
    if not paths:
        return False  # First attempted run may have failed before evaluation.
    body = subprocess.run(['git', 'show', 'FETCH_HEAD:state/' + STATE_FILE], cwd=root,
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    with tempfile.TemporaryDirectory(prefix='footpredict-experiment-restore-') as temporary:
        staging = Path(temporary)
        (staging / STATE_FILE).write_bytes(body)
        read_state(staging)
    destination = Path(state_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / STATE_FILE
    path.with_suffix('.tmp').write_bytes(body)
    path.with_suffix('.tmp').replace(path)
    return True


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode()


def save_immutable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != body:
        raise ValueError('Immutable experiment archive differs')
    path.write_bytes(body)


def publish(output, state_dir, repository, run_id, run_attempt, commit):
    output, state_dir = Path(output).resolve(), Path(state_dir).resolve()
    report = validate_report(json.loads((output / 'report.json').read_text()))
    state = read_state(state_dir, optional=True)
    root = Path(git('rev-parse', '--show-toplevel'))
    remote = git('ls-remote', '--heads', 'origin', 'refs/heads/' + BRANCH, cwd=root)
    if remote:
        git('fetch', '--no-tags', 'origin', BRANCH, cwd=root)
    temporary = Path(tempfile.mkdtemp(prefix='footpredict-experiment-publish-'))
    worktree = temporary / 'report'
    orphan = None
    try:
        git('worktree', 'add', '--detach', str(worktree), 'FETCH_HEAD' if remote else 'HEAD', cwd=root)
        if not remote:
            orphan = 'experiment-publish-' + uuid.uuid4().hex
            git('checkout', '--orphan', orphan, cwd=worktree)
            git('rm', '-rf', '--ignore-unmatch', '.', cwd=worktree)
        latest = worktree / 'latest.json'
        previous = json.loads(latest.read_text()) if latest.exists() else None
        merged = merge_latest(previous, report, state, repository=repository,
            run_id=run_id, run_attempt=run_attempt, commit=commit)
        if report['status'] != 'success':
            old_state = read_state(worktree / 'state', optional=True)
            if old_state is not None and state is not None and old_state != state:
                raise ValueError('A failed or unchanged run cannot alter successful state')
        for value in (report, state['report'] if state else None):
            if value is not None:
                save_immutable(worktree / 'reports' / (sha(value) + '.json'), canonical(value))
        save_immutable(worktree / 'runs' / f'{run_id}-{run_attempt}.json',
                       canonical(merged['latestAttempt']))
        latest.write_text(json.dumps(merged, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
        if state is not None and report['status'] in ('success', 'unchanged'):
            path = worktree / 'state' / STATE_FILE
            path.parent.mkdir(exist_ok=True)
            path.write_bytes((state_dir / STATE_FILE).read_bytes())
        git('add', 'latest.json', 'reports', 'runs', *(['state'] if (worktree / 'state').exists() else []), cwd=worktree)
        if not git('diff', '--cached', '--name-only', cwd=worktree):
            return
        git('-c', 'user.name=github-actions[bot]', '-c',
            'user.email=41898282+github-actions[bot]@users.noreply.github.com',
            'commit', '-m', f'Record automatic experiment {run_id}/{run_attempt}', cwd=worktree)
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
    publish_parser.add_argument('--state-dir', required=True, type=Path)
    for field in ('repository', 'run-id', 'run-attempt', 'commit'):
        publish_parser.add_argument('--' + field, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'restore':
            print(json.dumps({'restored': restore(args.state_dir)}))
        else:
            publish(args.output, args.state_dir, args.repository, args.run_id, args.run_attempt, args.commit)
    except subprocess.CalledProcessError as error:
        raise SystemExit('Experiment Git operation failed (exit ' + str(error.returncode)
                         + '); previously published results are preserved.') from None
