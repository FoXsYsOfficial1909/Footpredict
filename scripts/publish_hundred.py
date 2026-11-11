"""Publish result observations for the fixed hundred, keeping all forecasts in main immutable."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from research.hundred import REGISTERED_SHA,PROTOCOL

BRANCH='hundred-results'


def git(*args,cwd=None):
    return subprocess.run(['git',*args],cwd=cwd,check=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout.strip()


def compact(report):
    if report.get('protocol')!=PROTOCOL or report.get('packetSha256')!=REGISTERED_SHA or report.get('forecastsChanged') is not False:
        raise ValueError('Result report refers to a changed forecast packet')
    result={k:v for k,v in report.items() if k!='fixtures'}
    if report['status']!='failed':
        if len(report.get('fixtures',[]))!=100 or len({f['fixtureId'] for f in report['fixtures']})!=100 or sum(report['counts'].values())!=100:
            raise ValueError('Result cohort is not the complete registered hundred')
        result['fixtures']=[{k:f[k] for k in ('fixtureId','status','outcome','score','reason')} for f in report['fixtures']]
    return result


def restore(output):
    root=Path(git('rev-parse','--show-toplevel'))
    if not git('ls-remote','--heads','origin','refs/heads/'+BRANCH,cwd=root):return False
    git('fetch','--no-tags','origin',BRANCH,cwd=root)
    raw=git('show','FETCH_HEAD:latest.json',cwd=root);packet=json.loads(raw)
    previous=packet.get('latestSuccessful')
    if previous:
        if previous['packetSha256']!=REGISTERED_SHA:raise ValueError('Different previous forecast cohort')
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(previous,ensure_ascii=False))
    return previous is not None


def publish(output,repository,run_id,attempt,commit):
    if not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository) or not str(run_id).isdigit() or not str(attempt).isdigit() or not re.fullmatch('[0-9a-f]{40}',commit):
        raise ValueError('Invalid publication metadata')
    report=json.loads((output/'report.json').read_text());view=compact(report)
    root=Path(git('rev-parse','--show-toplevel'))
    remote=git('ls-remote','--heads','origin','refs/heads/'+BRANCH,cwd=root)
    if remote:git('fetch','--no-tags','origin',BRANCH,cwd=root)
    with tempfile.TemporaryDirectory(prefix='footpredict-hundred-') as temporary:
        tree=Path(temporary)/'branch';orphan=None
        try:
            git('worktree','add','--detach',str(tree),'FETCH_HEAD' if remote else 'HEAD',cwd=root)
            if not remote:
                orphan='hundred-publish-'+uuid.uuid4().hex
                git('checkout','--orphan',orphan,cwd=tree);git('rm','-rf','--ignore-unmatch','.',cwd=tree)
            path=tree/'latest.json';previous=json.loads(path.read_text()) if path.exists() else {}
            old=previous.get('latestAttempt')
            if old and datetime.fromisoformat(old['checkedAt'])>datetime.fromisoformat(view['checkedAt']):
                raise ValueError('Cannot replace a newer result observation')
            view.update(runId=str(run_id),runAttempt=str(attempt),sourceCommit=commit,
                runUrl=f'https://github.com/{repository}/actions/runs/{run_id}')
            latest={'schemaVersion':1,'protocol':PROTOCOL,'packetSha256':REGISTERED_SHA,
                'latestAttempt':view,'latestSuccessful':view if view['status']!='failed' else previous.get('latestSuccessful')}
            encoded=json.dumps(report,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
            checksum=hashlib.sha256(encoded).hexdigest();archive=tree/'reports'/(checksum+'.json');archive.parent.mkdir(exist_ok=True)
            if archive.exists() and archive.read_bytes()!=encoded:raise ValueError('Immutable result observation changed')
            archive.write_bytes(encoded)
            path.write_text(json.dumps(latest,ensure_ascii=False,separators=(',',':'),allow_nan=False))
            git('add','latest.json','reports',cwd=tree)
            if git('diff','--cached','--name-only',cwd=tree):
                git('-c','user.name=github-actions[bot]','-c','user.email=41898282+github-actions[bot]@users.noreply.github.com',
                    'commit','-m',f'Check frozen hundred {run_id}/{attempt}',cwd=tree)
                git('push','origin','HEAD:refs/heads/'+BRANCH,cwd=tree)
        finally:
            if tree.exists():git('worktree','remove','--force',str(tree),cwd=root)
            if orphan:git('branch','-D',orphan,cwd=root)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=('restore','publish'))
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--repository');parser.add_argument('--run-id')
    parser.add_argument('--run-attempt');parser.add_argument('--commit');args=parser.parse_args()
    if args.command=='restore':print(json.dumps({'restored':restore(args.output)}))
    else:publish(args.output,args.repository,args.run_id,args.run_attempt,args.commit)
