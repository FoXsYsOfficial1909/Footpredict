"""Publish training summaries to a separate branch using checkout's Git auth."""
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

BRANCH='training-reports'
BRANCH_LEAGUES={BRANCH:'PL','championship-training-reports':'ELC'}


def merge_latest(previous,attempt,*,repository,run_id,run_attempt,commit,branch=BRANCH):
    if branch not in BRANCH_LEAGUES:raise ValueError('Unsupported training report branch')
    expected=BRANCH_LEAGUES[branch]
    if attempt.get('league','PL')!=expected or attempt.get('division', 'E0')!=('E1' if expected=='ELC' else 'E0'):
        raise ValueError('Training report league does not match the publication branch')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):raise ValueError('Invalid repository')
    if not str(run_id).isdigit() or not str(run_attempt).isdigit() or not re.fullmatch(r'[0-9a-f]{40}',commit):raise ValueError('Invalid run metadata')
    if attempt.get('schemaVersion')!=1 or attempt.get('status') not in ('success','failed') or attempt.get('promotedToDefault') is not False:
        raise ValueError('Invalid training attempt')
    datetime.fromisoformat(attempt['generatedAt'].replace('Z','+00:00'))
    if attempt['status']=='success':
        if not attempt['latestTrainingMatch']<attempt['trainingCutoffExclusive']<attempt['asOf']:
            raise ValueError('Training data fence was not preserved')
        if attempt['evaluation']['commonMatches']<60 or not re.fullmatch(r'[0-9a-f]{64}',attempt['modelSha256']):raise ValueError('Invalid candidate evaluation')
    report={**attempt,'repository':repository,'runId':str(run_id),'runAttempt':str(run_attempt),'sourceCommit':commit,
        'runUrl':f'https://github.com/{repository}/actions/runs/{run_id}',
        'artifactsUrl':f'https://github.com/{repository}/actions/runs/{run_id}#artifacts'}
    if attempt['status']=='success':
        report['modelUrl']=f"https://raw.githubusercontent.com/{repository}/{branch}/models/{attempt['modelSha256']}.json"
    previous=previous or {}
    old=previous.get('latestAttempt')
    if old and old.get('league','PL')!=expected:raise ValueError('Previous report belongs to another league')
    if old and datetime.fromisoformat(old['generatedAt'].replace('Z','+00:00'))>datetime.fromisoformat(report['generatedAt'].replace('Z','+00:00')):
        raise ValueError('Refusing to replace a newer report')
    return {'schemaVersion':1,'latestAttempt':report,'latestSuccessful':report if report['status']=='success' else previous.get('latestSuccessful')}


def git(*args,cwd=None):
    return subprocess.run(['git',*args],cwd=cwd,check=True,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout.strip()


def save_public_model(output,worktree,attempt):
    """Publish exact canonical bytes under their hash; never replace old weights."""
    checksum=attempt.get('modelSha256','')
    if attempt.get('status')!='success' or not re.fullmatch(r'[0-9a-f]{64}',checksum):
        raise ValueError('Invalid successful model checksum')
    model=json.loads((Path(output)/'model.json').read_text())
    if sha(model)!=checksum or model.get('model_version')!=attempt.get('modelVersion'):
        raise ValueError('Model checksum or version mismatch')
    if model.get('league','PL')!=attempt.get('league','PL') or model.get('division','E0')!=attempt.get('division','E0'):
        raise ValueError('Model and training report describe different leagues')
    body=json.dumps(model,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    target=Path(worktree)/'models'/(checksum+'.json');target.parent.mkdir(exist_ok=True)
    if target.exists() and target.read_bytes()!=body:raise ValueError('Immutable model archive differs')
    target.write_bytes(body)
    return target


def publish(output,repository,run_id,run_attempt,commit,*,branch=BRANCH):
    if branch not in BRANCH_LEAGUES:raise ValueError('Unsupported training report branch')
    root=Path(git('rev-parse','--show-toplevel'))
    output=Path(output).resolve();attempt=json.loads((output/'attempt.json').read_text())
    if attempt['status']=='success' and sha(json.loads((output/'model.json').read_text()))!=attempt['modelSha256']:
        raise ValueError('Model checksum mismatch')
    remote=git('ls-remote','--heads','origin','refs/heads/'+branch,cwd=root)
    if remote:git('fetch','--no-tags','origin',branch,cwd=root)
    temporary=Path(tempfile.mkdtemp(prefix='footpredict-training-publish-'));worktree=temporary/'report'
    orphan=None
    try:
        git('worktree','add','--detach',str(worktree),'FETCH_HEAD' if remote else 'HEAD',cwd=root)
        if not remote:
            orphan='training-publish-'+uuid.uuid4().hex
            git('checkout','--orphan',orphan,cwd=worktree)
            git('rm','-rf','--ignore-unmatch','.',cwd=worktree)
        latest=worktree/'latest.json'
        previous=json.loads(latest.read_text()) if latest.exists() else None
        merged=merge_latest(previous,attempt,repository=repository,run_id=run_id,run_attempt=run_attempt,commit=commit,branch=branch)
        if attempt['status']=='success':save_public_model(output,worktree,attempt)
        archive=worktree/'runs'/(str(run_id)+'-'+str(run_attempt)+'.json');archive.parent.mkdir(exist_ok=True)
        encoded=json.dumps(merged['latestAttempt'],ensure_ascii=False,indent=2)+'\n'
        if archive.exists() and archive.read_text()!=encoded:raise ValueError('Attempt archive already exists with different data')
        archive.write_text(encoded);latest.write_text(json.dumps(merged,ensure_ascii=False,indent=2)+'\n')
        git('add','latest.json','runs',*(['models'] if attempt['status']=='success' else []),cwd=worktree)
        if not git('diff','--cached','--name-only',cwd=worktree):return
        git('-c','user.name=github-actions[bot]','-c','user.email=41898282+github-actions[bot]@users.noreply.github.com',
            'commit','-m','Record training attempt '+str(run_id)+'/'+str(run_attempt),cwd=worktree)
        git('push','origin','HEAD:refs/heads/'+branch,cwd=worktree)
        print(json.dumps({'publishedBranch':branch,'status':attempt['status'],'runId':str(run_id)}))
    finally:
        if worktree.exists():git('worktree','remove','--force',str(worktree),cwd=root)
        if orphan:
            refs=git('branch','--list',orphan,cwd=root)
            if refs:git('branch','-D',orphan,cwd=root)
        temporary.rmdir()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repository',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--run-attempt',required=True)
    parser.add_argument('--commit',required=True)
    parser.add_argument('--branch',choices=tuple(BRANCH_LEAGUES),default=BRANCH)
    args=parser.parse_args()
    try:publish(args.output,args.repository,args.run_id,args.run_attempt,args.commit,branch=args.branch)
    except subprocess.CalledProcessError as error:raise SystemExit('Git publication failed (exit '+str(error.returncode)+'); the previous report is preserved.') from None
