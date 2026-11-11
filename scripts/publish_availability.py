"""Publish availability science separately, preserving original observations and forecasts."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from research.availability_experiment import PROTOCOL, canonical
from research.features import digest
from scripts.publish_training import git

BRANCH = 'availability-reports'


@contextmanager
def branch_tree(create=False):
    root = Path(git('rev-parse','--show-toplevel'))
    remote = git('ls-remote','--heads','origin','refs/heads/'+BRANCH,cwd=root)
    if not remote and not create:
        yield None
        return
    if remote:
        git('fetch','--no-tags','origin',BRANCH,cwd=root)
    with tempfile.TemporaryDirectory(prefix='footpredict-availability-') as temporary:
        tree = Path(temporary)/'branch';orphan = None
        try:
            git('worktree','add','--detach',str(tree),'FETCH_HEAD' if remote else 'HEAD',cwd=root)
            if not remote:
                orphan = 'availability-publish-'+uuid.uuid4().hex
                git('checkout','--orphan',orphan,cwd=tree)
                git('rm','-rf','--ignore-unmatch','.',cwd=tree)
            yield tree
        finally:
            if tree.exists():
                git('worktree','remove','--force',str(tree),cwd=root)
            if orphan:
                git('branch','-D',orphan,cwd=root)


def validate_state(state):
    if (set(state)!={'schemaVersion','protocol','cursor','forecasts'} or state.get('schemaVersion')!=1
        or state.get('protocol')!=PROTOCOL or not isinstance(state['forecasts'],dict)):
        raise ValueError('Invalid availability state')
    if state['cursor'] is not None and not re.fullmatch(r'[1-9]\d{0,14}',state['cursor']):
        raise ValueError('Invalid archive cursor')
    return state


def immutable(path, body):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and path.read_bytes()!=body:
        raise ValueError('Immutable availability archive changed')
    path.write_bytes(body)


def merge_state(old, new):
    validate_state(new)
    if old:
        validate_state(old)
        for key,value in old['forecasts'].items():
            if new['forecasts'].get(key)!=value:
                raise ValueError('An original prospective forecast cannot be overwritten or removed')
        if old['cursor'] and not new['cursor']:
            raise ValueError('Archive cursor cannot be reset')
        if old['cursor'] and int(new['cursor'])<int(old['cursor']):
            raise ValueError('Archive cursor cannot move backwards')
    return new


def restore(directory):
    with branch_tree() as tree:
        if tree is None:
            return False
        source = tree/'state'
        validate_state(json.loads((source/'state.json').read_text()))
        shutil.copytree(source,directory,dirs_exist_ok=True)
        return True


def publish(output,directory,repository,run_id,attempt,commit):
    if (not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository)
        or not str(run_id).isdigit() or not str(attempt).isdigit() or not re.fullmatch('[0-9a-f]{40}',commit)):
        raise ValueError('Invalid publication metadata')
    report = json.loads((output/'report.json').read_text())
    if report.get('protocol')!=PROTOCOL or report.get('promotedToDefault') is not False or report.get('status') not in ('candidate','collecting','failed'):
        raise ValueError('Invalid availability report')
    success = report['status']!='failed'
    with branch_tree(create=True) as tree:
        previous = json.loads((tree/'latest.json').read_text()) if (tree/'latest.json').exists() else {}
        old = previous.get('latestAttempt')
        if old and datetime.fromisoformat(old['generatedAt'])>datetime.fromisoformat(report['generatedAt']):
            raise ValueError('Cannot replace a newer report')
        if success:
            state = validate_state(json.loads((directory/'state.json').read_text()))
            old_path = tree/'state/state.json'
            merge_state(json.loads(old_path.read_text()) if old_path.exists() else None,state)
            for folder in ('snapshots','models'):
                for source in (directory/folder).glob('*.json'):
                    if not re.fullmatch(r'[0-9a-f]{64}\.json',source.name):
                        raise ValueError('Unexpected archive filename')
                    immutable(tree/'state'/folder/source.name,source.read_bytes())
            old_path.parent.mkdir(exist_ok=True)
            old_path.write_bytes(canonical(state))
            if report['status']=='candidate':
                model = json.loads((output/'model.json').read_text())
                if digest(model)!=report['modelSha256']:
                    raise ValueError('Model report checksum mismatch')
                immutable(tree/'state/models'/(report['modelSha256']+'.json'),canonical(model))
        value = {**report,'sourceCommit':commit,'runId':str(run_id),'runAttempt':str(attempt),
            'runUrl':f'https://github.com/{repository}/actions/runs/{run_id}'}
        latest = {'schemaVersion':1,'protocol':PROTOCOL,'latestAttempt':value,
            'latestSuccessful':value if success else previous.get('latestSuccessful')}
        immutable(tree/'runs'/f'{run_id}-{attempt}.json',canonical(value))
        (tree/'latest.json').write_bytes(canonical(latest))
        git('add','latest.json','runs',*(['state'] if (tree/'state').exists() else []),cwd=tree)
        if git('diff','--cached','--name-only',cwd=tree):
            git('-c','user.name=github-actions[bot]','-c','user.email=41898282+github-actions[bot]@users.noreply.github.com',
                'commit','-m',f'Record availability check {run_id}/{attempt}',cwd=tree)
            git('push','origin','HEAD:refs/heads/'+BRANCH,cwd=tree)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('restore','publish'))
    parser.add_argument('--state-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--repository');parser.add_argument('--run-id');parser.add_argument('--run-attempt');parser.add_argument('--commit')
    args = parser.parse_args()
    if args.command=='restore':
        print(json.dumps({'restored':restore(args.state_dir)}))
    else:
        publish(args.output,args.state_dir,args.repository,args.run_id,args.run_attempt,args.commit)
