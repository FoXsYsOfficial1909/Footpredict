"""Publish the frozen ELC comparison separately; never rewrite its forecasts."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from research.elc_future import PROTOCOL, REGISTERED_SHA, ORIGINAL_SHA, verify_files
from research.hundred import instant
from scripts.publish_hundred import git

BRANCH = 'elc-comparison-results'


def identity(comparison):
    if comparison == 'shots':return PROTOCOL, REGISTERED_SHA, BRANCH, verify_files, {'candidate','v1','base'}
    if comparison == 'transfer':
        from research.transfer_future import PROTOCOL as protocol, REGISTERED_SHA as checksum, verify_files as verify
        return protocol, checksum, 'elc-transfer-results', verify, {'candidate','v1','base','previousShots'}
    raise ValueError('Unknown frozen comparison')


def compact(report, *, comparison='shots'):
    protocol, checksum, _, verify, methods = identity(comparison)
    if (report.get('schemaVersion') != 1 or report.get('protocol') != protocol
            or report.get('packetSha256') != checksum or report.get('sourceCohortSha256') != ORIGINAL_SHA
            or report.get('forecastsChanged') is not False or report.get('status') not in ('active', 'complete', 'failed')):
        raise ValueError('Different ELC cohort or invalid comparison report')
    instant(report['checkedAt'])
    result = {key: value for key, value in report.items() if key != 'fixtures'}
    if report['status'] != 'failed':
        packet = verify(); ids = {f['fixtureId'] for f in packet['fixtures']}
        rows = report.get('fixtures', [])
        if (len(rows) != len(ids) or len({r['fixtureId'] for r in rows}) != len(ids)
                or {r['fixtureId'] for r in rows} != ids or sum(report['counts'].values()) != len(ids)
                or set(report['counts']) != {'pending', 'settled', 'review', 'unavailable'}
                or set(report['metrics']) != methods
                or {m['n'] for m in report['metrics'].values()} != {report['counts']['settled']}):
            raise ValueError('ELC comparison cohort or paired metric count changed')
        if comparison == 'transfer':
            if (report['applicableMatches'] != packet['applicableMatches'] or report['changedMatches'] != packet['changedMatches']
                    or sum(report['applicableCounts'].values()) != packet['applicableMatches']
                    or set(report['applicableMetrics']) != methods
                    or {m['n'] for m in report['applicableMetrics'].values()} != {report['applicableCounts']['settled']}):
                raise ValueError('Conditional comparison applicability or paired count changed')
        result['fixtures'] = [{key: r[key] for key in ('fixtureId', 'status', 'outcome', 'score', 'reason')} for r in rows]
    return result


def merge_latest(previous, report, *, repository, run_id, attempt, commit, comparison='shots'):
    protocol, checksum, _, _, _ = identity(comparison)
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or not str(run_id).isdigit() or not str(attempt).isdigit() or not re.fullmatch('[0-9a-f]{40}', commit)):
        raise ValueError('Invalid comparison publication metadata')
    view = compact(report, comparison=comparison); previous = previous or {}
    old = previous.get('latestSuccessful')
    if previous and (previous.get('protocol') != protocol or previous.get('packetSha256') != checksum):
        raise ValueError('Cannot replace another cohort archive')
    if old and view['status'] != 'failed' and instant(old['checkedAt']) > instant(view['checkedAt']):
        raise ValueError('Cannot replace a newer verified source observation')
    view.update(runId=str(run_id), runAttempt=str(attempt), sourceCommit=commit,
                runUrl=f'https://github.com/{repository}/actions/runs/{run_id}',
                publishedAt=datetime.now(timezone.utc).isoformat())
    return {'schemaVersion': 1, 'protocol': protocol, 'packetSha256': checksum,
            'latestAttempt': view, 'latestSuccessful': view if view['status'] != 'failed' else old}


def publish(output, repository, run_id, attempt, commit, *, comparison='shots'):
    _, _, branch, _, _ = identity(comparison)
    report = json.loads((Path(output)/'report.json').read_text()); compact(report, comparison=comparison)
    root = Path(git('rev-parse', '--show-toplevel'))
    remote = git('ls-remote', '--heads', 'origin', 'refs/heads/'+branch, cwd=root)
    if remote:git('fetch', '--no-tags', 'origin', branch, cwd=root)
    with tempfile.TemporaryDirectory(prefix='footpredict-elc-comparison-') as temporary:
        tree = Path(temporary)/'branch'; orphan = None
        try:
            git('worktree', 'add', '--detach', str(tree), 'FETCH_HEAD' if remote else 'HEAD', cwd=root)
            if not remote:
                orphan = 'elc-publish-'+uuid.uuid4().hex
                git('checkout', '--orphan', orphan, cwd=tree); git('rm', '-rf', '--ignore-unmatch', '.', cwd=tree)
            latest = tree/'latest.json'
            previous = json.loads(latest.read_text()) if latest.exists() else None
            result = merge_latest(previous, report, repository=repository, run_id=run_id, attempt=attempt, commit=commit, comparison=comparison)
            encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            checksum = hashlib.sha256(encoded).hexdigest(); archive = tree/'reports'/(checksum+'.json')
            archive.parent.mkdir(exist_ok=True)
            if archive.exists() and archive.read_bytes() != encoded:raise ValueError('Immutable ELC observation changed')
            archive.write_bytes(encoded)
            latest.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
            git('add', 'latest.json', 'reports', cwd=tree)
            if git('diff', '--cached', '--name-only', cwd=tree):
                git('-c', 'user.name=github-actions[bot]', '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                    'commit', '-m', f'Check frozen ELC {run_id}/{attempt}', cwd=tree)
                git('push', 'origin', 'HEAD:refs/heads/'+branch, cwd=tree)
        finally:
            if tree.exists():git('worktree', 'remove', '--force', str(tree), cwd=root)
            if orphan:git('branch', '-D', orphan, cwd=root)
    print(json.dumps({'branch': branch, 'status': report['status'], 'runId': str(run_id)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True); parser.add_argument('--repository', required=True)
    parser.add_argument('--run-id', required=True); parser.add_argument('--run-attempt', required=True)
    parser.add_argument('--commit', required=True);parser.add_argument('--comparison',choices=('shots','transfer'),default='shots')
    args = parser.parse_args()
    try:publish(args.output, args.repository, args.run_id, args.run_attempt, args.commit, comparison=args.comparison)
    except subprocess.CalledProcessError as error:
        parser.exit(1, 'ELC result publication failed; the previous successful report is retained.\n')
