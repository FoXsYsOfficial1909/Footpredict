import copy
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
import gzip
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.nightly import sha
from research.quality_diagnostic import metrics
from research.trainer_agents import PROTOCOL,POLICY,new_state
from scripts.publish_trainer_agents import prospective_dashboard,publish,canonical


class TrainerDashboardTests(unittest.TestCase):
    def fixture(self,key):
        kickoff=datetime(2026,11,21,15,tzinfo=timezone.utc)+timedelta(days=key)
        return {'protocol':'neural-agent-future-1','fixtureId':9000+key,'home':'Home '+str(key),
            'away':'Away '+str(key),'kickoff':kickoff.isoformat(),
            'registeredAt':(kickoff-timedelta(days=3)).isoformat(),'modelVersion':'neural-test',
            'candidate':[.6,.2,.2],'base':[.5,.25,.25],'frequency':[.4,.3,.3],
            'v1_available':True,'promotedToDefault':False}

    def state(self,n=5):
        state=new_state()
        for key in range(n):
            f=self.fixture(key);state['forecasts'][str(f['fixtureId'])]=f
        for key,score in [(0,{'home':2,'away':1}),(1,{'home':0,'away':0})]:
            f=state['forecasts'][str(9000+key)]
            state['observations'][str(9000+key)]={'status':'settled','score':score,
                'actual':key,'checkedAt':(datetime.fromisoformat(f['kickoff'])+timedelta(hours=5)).isoformat()}
        state['forecasts']['9001']['candidate']=[.4,.4,.2]
        return state

    def report(self,state):
        counts=dict.fromkeys(('registered','settled','pending','review','unavailable'),0);scored=[]
        for key,f in state['forecasts'].items():
            obs=state['observations'].get(key,{});status=obs.get('status','pending')
            counts['registered']+=1;counts[status]+=1
            if status=='settled':scored.append({**f,**obs})
        return {'schemaVersion':1,'protocol':PROTOCOL,'status':'success','policy':POLICY,
            'checkedAt':'2027-12-01T00:00:00+00:00','promotedToDefault':False,
            'independentAdvantageEstablished':False,
            'roles':{'trainer':{'fits':0},'examiner':{'publicPromotionAllowed':False}},
            'modelSha256':'a'*64,'dataSha256':'b'*64,'inputFingerprint':'c'*64,'planSha256':'d'*64,'codeSha256':'e'*64,
            'prospective':{**counts,'metrics':{k:metrics(scored,k) for k in ('candidate','base','frequency')}}}

    def test_pending_review_and_missing_results_do_not_inflate_metrics(self):
        state=self.state()
        state['observations']['9003']={'status':'review','checkedAt':'2026-12-01T00:00:00Z'}
        state['observations']['9004']={'status':'unavailable','checkedAt':'2026-12-01T00:00:00Z'}
        original=copy.deepcopy(state);view=prospective_dashboard(state,self.report(state))
        self.assertEqual(view['counts'],dict(registered=5,settled=2,pending=1,review=1,unavailable=1))
        self.assertEqual(state,original)
        m=view['metrics']['candidate'];self.assertEqual(m['n'],2)
        self.assertAlmostEqual(m['logLoss'],-math.log(.6*.4)/2,places=14)
        self.assertAlmostEqual(m['brier'],.4,places=14)
        self.assertEqual(m['topEligible'],1);self.assertEqual(m['topTies'],1)
        self.assertEqual(m['uniqueTopAccuracy'],1.)
        self.assertTrue(all('score' not in r for r in view['rows'] if r['status']!='settled'))
        self.assertTrue(all('features' not in r for r in view['rows']))

    def test_display_limit_does_not_limit_metric_sample_or_replace_first_predictions(self):
        state=self.state(103);original=copy.deepcopy(state['forecasts'])
        view=prospective_dashboard(state,self.report(state))
        self.assertEqual(len(view['rows']),100);self.assertEqual(view['rows'][0]['fixtureId'],9003)
        self.assertTrue(all(r['status']=='pending' for r in view['rows']))
        self.assertEqual(view['counts']['registered'],103);self.assertEqual(view['metrics']['base']['n'],2)
        self.assertEqual(original,state['forecasts'])

    def test_empty_metrics_and_inconsistent_summary(self):
        state=new_state();report=self.report(state);view=prospective_dashboard(state,report)
        self.assertEqual(view['rows'],[]);self.assertIsNone(view['metrics']['candidate']['logLoss'])
        for key,value in [('registered',1),('settled',1)]:
            bad=copy.deepcopy(report);bad['prospective'][key]=value
            with self.assertRaises(ValueError):prospective_dashboard(state,bad)
        bad=copy.deepcopy(report);bad['prospective']['metrics']['base']['logLoss']=0.
        with self.assertRaises(ValueError):prospective_dashboard(state,bad)

    def test_fallback_is_explicit_and_unchanged_checks_share_one_content_digest(self):
        state=self.state();state['forecasts']['9001'].update(v1_available=False,base=[.4,.3,.3])
        report=self.report(state);view=prospective_dashboard(state,report)
        self.assertEqual(view['v1FallbackSettled'],1)
        later={**report,'checkedAt':'2027-12-02T00:00:00+00:00'}
        self.assertEqual(sha(view),sha(prospective_dashboard(state,later)))
        self.assertFalse(view['rows'][1]['v1_available'])

    def test_publisher_links_verified_immutable_view_and_failure_retains_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);saved=root/'saved';output=root/'output';tree=root/'branch'
            for p in (saved,output,tree):p.mkdir()
            state=self.state();model={'test':'model'};data=[['a','fixture']];quality={'test':'source'}
            report=self.report(state);report.update(modelSha256=sha(model),dataSha256=sha(data))
            state['training']={'modelSha256':sha(model)}
            for f in state['forecasts'].values():
                f.update(modelSha256=sha(model),dataSha256=sha(data),sourceQualitySha256=sha(quality))
            values={'state.json':state,'model.json':model,'monitoring.json':{},'feature-rows-manifest.json':{}}
            for name,value in values.items():(saved/name).write_text(json.dumps(value))
            (saved/'training-data.json.gz').write_bytes(gzip.compress(canonical(data),mtime=0))
            (saved/'feature-rows.json.gz').write_bytes(gzip.compress(b'[]',mtime=0))
            (output/'quality-source.json').write_text(json.dumps(quality));(output/'report.json').write_text(json.dumps(report))
            @contextmanager
            def local_branch(*args,**kwargs):yield tree
            args=dict(repository='FoXsYsOfficial1909/Footpredict',run_id='1',attempt='1',commit='f'*40)
            with patch('scripts.publish_trainer_agents.branch_tree',local_branch),patch('scripts.publish_trainer_agents.git',return_value=''):
                publish(saved,output,**args)
                packet=json.loads((tree/'latest.json').read_text());success=packet['latestSuccessful']
                target=tree/'dashboards'/(success['prospectiveDashboardSha256']+'.json')
                self.assertEqual(sha(json.loads(target.read_text())),success['prospectiveDashboardSha256'])
                original=target.read_bytes();first=(tree/'forecasts/9000.json').read_bytes()
                (output/'report.json').write_text(json.dumps({**report,'status':'failed'}))
                publish(saved,output,**{**args,'run_id':'2'})
                after=json.loads((tree/'latest.json').read_text())
                self.assertEqual(after['latestSuccessful'],success);self.assertEqual(after['latestAttempt']['status'],'failed')
                self.assertEqual(target.read_bytes(),original);self.assertEqual((tree/'forecasts/9000.json').read_bytes(),first)


if __name__=='__main__':unittest.main()
