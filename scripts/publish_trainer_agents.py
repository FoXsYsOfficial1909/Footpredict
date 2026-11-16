"""Publish autonomous trainer reports and immutable models/forecasts to their own branch."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import gzip
import json
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from research.nightly import sha
from research.trainer_agents import BRANCH,PROTOCOL,POLICY,validate_state
from research.quality_diagnostic import metrics
from scripts.publish_training import git

STATE_FILES=('state.json','model.json','monitoring.json','training-data.json.gz',
             'feature-rows.json.gz','feature-rows-manifest.json')
DASHBOARD_PROTOCOL='neural-agent-dashboard-1'
DASHBOARD_LIMIT=100


def prospective_dashboard(state,report):
    """A bounded display of frozen forecasts; metrics always cover the full series."""
    validate_state(state)
    rows=[];scored=[];counts=dict.fromkeys(('registered','settled','pending','review','unavailable'),0)
    fallback=0
    for key,f in state['forecasts'].items():
        if (type(f.get('v1_available')) is not bool
                or any(not isinstance(f.get(k),str) or not 1<=len(f[k])<=200 for k in ('home','away','modelVersion'))):
            raise ValueError('Incomplete neural display provenance')
        observed=state['observations'].get(key,{});status=observed.get('status','pending')
        counts['registered']+=1;counts[status]+=1
        row={k:f[k] for k in ('fixtureId','home','away','kickoff','registeredAt','modelVersion',
                              'candidate','base','frequency','v1_available')}
        row['status']=status
        if status=='settled':
            row.update({k:observed[k] for k in ('score','actual','checkedAt')})
            scored.append({**f,**observed});fallback+=not f['v1_available']
        rows.append(row)
    scores={method:metrics(scored,method) for method in ('candidate','base','frequency')}
    summary=report['prospective']
    if any(summary[k]!=v for k,v in counts.items()) or summary['metrics']!=scores:
        raise ValueError('Neural display cannot disagree with the full saved series')
    return {'schemaVersion':1,'protocol':DASHBOARD_PROTOCOL,'league':'PL',
        'counts':counts,'metrics':scores,'v1FallbackSettled':fallback,
        'selection':'latest_kickoff_then_fixture_id','maximumShown':DASHBOARD_LIMIT,
        'rows':sorted(rows,key=lambda r:(datetime.fromisoformat(r['kickoff'].replace('Z','+00:00')),r['fixtureId']))[-DASHBOARD_LIMIT:]}


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def immutable(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and path.read_bytes()!=raw:raise ValueError('An immutable trainer artifact changed')
    path.write_bytes(raw)


@contextmanager
def branch_tree(create=False):
    root=Path(git('rev-parse','--show-toplevel'));remote=git('ls-remote','--heads','origin','refs/heads/'+BRANCH,cwd=root)
    if not remote and not create:yield None;return
    if remote:git('fetch','--no-tags','origin',BRANCH,cwd=root)
    with tempfile.TemporaryDirectory(prefix='footpredict-trainer-agents-') as directory:
        tree=Path(directory)/'branch';orphan=None
        try:
            git('worktree','add','--detach',str(tree),'FETCH_HEAD' if remote else 'HEAD',cwd=root)
            if not remote:
                orphan='trainer-agents-'+uuid.uuid4().hex
                git('checkout','--orphan',orphan,cwd=tree);git('rm','-rf','--ignore-unmatch','.',cwd=tree)
            yield tree
        finally:
            if tree.exists():git('worktree','remove','--force',str(tree),cwd=root)
            if orphan:git('branch','-D',orphan,cwd=root)


def restore(directory):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    with branch_tree() as tree:
        if tree is None:return False
        source=tree/'state';state=validate_state(json.loads((source/'state.json').read_text()))
        model=json.loads((source/'model.json').read_text())
        if sha(model)!=state['training']['modelSha256']:raise ValueError('Restored trainer model checksum changed')
        for name in STATE_FILES:shutil.copyfile(source/name,directory/name)
        return True


def merge_state(old,new):
    validate_state(new)
    if old:
        validate_state(old)
        for key,value in old['forecasts'].items():
            if new['forecasts'].get(key)!=value:raise ValueError('A first neural forecast cannot be replaced or removed')
        for key,value in old['observations'].items():
            if value['status']=='review' and new['observations'].get(key)!=value:
                raise ValueError('A reviewed identity cannot be silently reinstated')
    return new


def validate_report(report):
    if (report.get('schemaVersion')!=1 or report.get('protocol')!=PROTOCOL
            or report.get('status') not in ('success','failed') or report.get('policy')!=POLICY
            or report.get('promotedToDefault') is not False or report.get('independentAdvantageEstablished') is not False):
        raise ValueError('Invalid trainer report or changed automatic promotion policy')
    checked=datetime.fromisoformat(report['checkedAt'])
    if checked.tzinfo is None:raise ValueError('Trainer check needs its actual timezone')
    if report['status']=='success':
        for key in ('modelSha256','dataSha256','inputFingerprint','planSha256','codeSha256'):
            if not re.fullmatch('[0-9a-f]{64}',report.get(key,'')):raise ValueError('Missing trainer provenance')
        if report['roles']['trainer']['fits'] not in (0,2):raise ValueError('Exceeded the neural fit budget')
        if report['roles']['examiner']['publicPromotionAllowed'] is not False:raise ValueError('Unapproved neural promotion')
        if ('prospectiveDashboardSha256' in report
                and not re.fullmatch('[0-9a-f]{64}',report['prospectiveDashboardSha256'])):
            raise ValueError('Invalid neural display checksum')
    return report


def merge_latest(previous,report,*,repository,run_id,attempt,commit):
    validate_report(report)
    if (not re.fullmatch('[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository) or not str(run_id).isdigit()
            or not str(attempt).isdigit() or not re.fullmatch('[0-9a-f]{40}',commit)):
        raise ValueError('Invalid trainer run metadata')
    previous=previous or {}
    if previous and previous.get('protocol')!=PROTOCOL:raise ValueError('Another trainer archive cannot be overwritten')
    old=previous.get('latestAttempt')
    if old and datetime.fromisoformat(old['checkedAt'])>datetime.fromisoformat(report['checkedAt']):
        raise ValueError('Refusing an older trainer observation')
    view={**report,'repository':repository,'runId':str(run_id),'runAttempt':str(attempt),'sourceCommit':commit,
        'runUrl':f'https://github.com/{repository}/actions/runs/{run_id}'}
    if report['status']=='success':view['modelUrl']=f'https://raw.githubusercontent.com/{repository}/{BRANCH}/models/{report["modelSha256"]}.json'
    return {'schemaVersion':1,'protocol':PROTOCOL,'latestAttempt':view,
        'latestSuccessful':view if report['status']=='success' else previous.get('latestSuccessful')}


def publish(directory,output,*,repository,run_id,attempt,commit):
    directory=Path(directory);output=Path(output);report=validate_report(json.loads((output/'report.json').read_text()))
    state=None;dashboard=None
    if report['status']=='success':
        state=validate_state(json.loads((directory/'state.json').read_text()))
        dashboard=prospective_dashboard(state,report)
        report={**report,'prospectiveDashboardSha256':sha(dashboard)}
    with branch_tree(create=True) as tree:
        latest=tree/'latest.json';previous=json.loads(latest.read_text()) if latest.exists() else None
        result=merge_latest(previous,report,repository=repository,run_id=run_id,attempt=attempt,commit=commit)
        if report['status']=='success':
            old=tree/'state/state.json'
            merge_state(json.loads(old.read_text()) if old.exists() else None,state)
            immutable(tree/'dashboards'/(report['prospectiveDashboardSha256']+'.json'),canonical(dashboard))
            model=json.loads((directory/'model.json').read_text())
            if sha(model)!=report['modelSha256'] or state['training']['modelSha256']!=report['modelSha256']:
                raise ValueError('Published trainer model or state does not match its report')
            immutable(tree/'models'/(report['modelSha256']+'.json'),canonical(model))
            data=(directory/'training-data.json.gz').read_bytes()
            if sha(json.loads(gzip.decompress(data)))!=report['dataSha256']:raise ValueError('Trainer dataset checksum changed')
            immutable(tree/'datasets'/(report['dataSha256']+'.json.gz'),data)
            quality=json.loads((output/'quality-source.json').read_text())
            immutable(tree/'sources'/(sha(quality)+'.json'),canonical(quality))
            for key,forecast in state['forecasts'].items():
                if not (tree/'models'/(forecast['modelSha256']+'.json')).exists():raise ValueError('A forecast lost its exact model')
                if not (tree/'datasets'/(forecast['dataSha256']+'.json.gz')).exists():raise ValueError('A forecast lost its input dataset')
                if not (tree/'sources'/(forecast['sourceQualitySha256']+'.json')).exists():raise ValueError('A forecast lost its calendar source')
                immutable(tree/'forecasts'/(key+'.json'),canonical(forecast))
            destination=tree/'state';destination.mkdir(exist_ok=True)
            for name in STATE_FILES:shutil.copyfile(directory/name,destination/name)
        immutable(tree/'reports'/(str(run_id)+'-'+str(attempt)+'.json'),canonical(result['latestAttempt']))
        latest.write_bytes(canonical(result));git('add','.',cwd=tree)
        if git('diff','--cached','--name-only',cwd=tree):
            git('-c','user.name=github-actions[bot]','-c','user.email=41898282+github-actions[bot]@users.noreply.github.com',
                'commit','-m',f'Record trainer agents {run_id}/{attempt}',cwd=tree)
            git('push','origin','HEAD:refs/heads/'+BRANCH,cwd=tree)
    print(json.dumps({'branch':BRANCH,'status':report['status'],'runId':str(run_id)}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=('restore','publish'))
    parser.add_argument('--state-dir',type=Path,required=True);parser.add_argument('--output',type=Path)
    parser.add_argument('--repository');parser.add_argument('--run-id');parser.add_argument('--run-attempt');parser.add_argument('--commit')
    args=parser.parse_args()
    if args.command=='restore':print(json.dumps({'restored':restore(args.state_dir)}))
    else:publish(args.state_dir,args.output,repository=args.repository,run_id=args.run_id,attempt=args.run_attempt,commit=args.commit)
