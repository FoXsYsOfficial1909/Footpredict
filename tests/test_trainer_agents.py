import copy
from datetime import date,datetime,timedelta,timezone
import gzip
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from research.neural_experiment import PLAN,source_games
from research.neural_residual import MODEL_VERSION
from research.nightly import sha
from research.trainer_agents import (ROOT,POLICY,PROTOCOL,new_state,forecast_features,
                                    observe_and_register,validate_state,run)
from scripts.publish_trainer_agents import merge_state,merge_latest,validate_report


class TrainerAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan=json.loads(PLAN.read_text());cls.games=source_games(cls.plan)

    def setUp(self):self.now=datetime(2026,11,16,12,tzinfo=timezone.utc)

    def network(self,n=1):
        return {'schemaVersion':1,'modelVersion':MODEL_VERSION,'settings':self.plan['settings'],
            'architecture':{'inputFeatures':56,'hiddenUnits':16,'outputs':3,'activation':'tanh','residualBase':'log_probability'},
            'featureMean':[0.]*56,'featureScale':[1.]*56,
            'weights':{'input':[[0.]*16 for _ in range(56)],'hiddenBias':[0.]*16,
                       'output':[[0.]*3 for _ in range(16)],'outputBias':[0.]*3},
            'fit':{'parameterCount':963,'converged':False,'stopReason':'iteration_limit','trainingRows':n},
            'promotedToDefault':False}

    def model(self):return {'network':self.network(),'blendWeight':1.,'modelVersion':'test-neural',
                            'dataSha256':'a'*64,'promotedToDefault':False}

    def quality(self,now=None,count=1):
        now=now or self.now
        pairs=[('Arsenal FC','Chelsea FC'),('Everton FC','Liverpool FC'),
               ('Manchester City FC','Manchester United FC'),('Aston Villa FC','Brentford FC'),
               ('Newcastle United FC','Fulham FC'),('Brighton & Hove Albion FC','Crystal Palace FC'),
               ('Leeds United FC','Sunderland AFC')]
        return {'schemaVersion':1,'generatedAt':now.isoformat(),'nightly':{
            'protocol':'nightly-live-1','policy':{'minimumHorizonHours':24,'automaticPromotion':False},
            'fixtures':[{'fixtureId':9000+i,'home':h,'away':a,'kickoff':'2026-11-21T15:00:00Z',
                         'state':'pending','latest':{'status':'pending'}} for i,(h,a) in enumerate(pairs[:count])]}}

    def test_registration_budget_first_forecasts_and_future_outcomes_stay_distinct(self):
        state=new_state();model=self.model();source=self.quality(count=7)
        result=observe_and_register(state,model,sha(model),self.games,source,self.now)
        self.assertEqual(result['newlyRegistered'],5);self.assertEqual(result['settled'],0)
        self.assertTrue(all(m['n']==0 and m['logLoss'] is None for m in result['metrics'].values()))
        first=copy.deepcopy(state['forecasts'])
        changed=self.model();changed['modelVersion']='later-version'
        result=observe_and_register(state,changed,sha(changed),self.games,source,self.now)
        self.assertEqual(result['newlyRegistered'],2)
        for key,value in first.items():self.assertEqual(state['forecasts'][key],value)
        self.assertEqual(len(state['forecasts']),7);validate_state(state)

    def test_results_corrections_and_rescheduling_never_rewrite_probabilities(self):
        state=new_state();model=self.model();source=self.quality()
        observe_and_register(state,model,sha(model),self.games,source,self.now)
        original=copy.deepcopy(state['forecasts']);later=datetime(2026,11,21,20,tzinfo=timezone.utc)
        source['generatedAt']=later.isoformat();f=source['nightly']['fixtures'][0]
        f.update(state='settled',latest={'status':'settled','providerStatus':'FINISHED',
            'checkedAt':later.isoformat(),'score':{'home':1,'away':0}})
        result=observe_and_register(state,model,sha(model),self.games,source,later)
        self.assertEqual(result['settled'],1);self.assertEqual({m['n'] for m in result['metrics'].values()},{1})
        f['latest']['score']={'home':0,'away':2}
        observe_and_register(state,model,sha(model),self.games,source,later)
        self.assertEqual(state['observations']['9000']['actual'],2);self.assertEqual(original,state['forecasts'])
        f['kickoff']='2026-11-21T16:00:00Z'
        result=observe_and_register(state,model,sha(model),self.games,source,later)
        self.assertEqual(result['review'],1);self.assertEqual(result['settled'],0)
        f['kickoff']=original['9000']['kickoff']
        result=observe_and_register(state,model,sha(model),self.games,source,later)
        self.assertEqual(result['review'],1);self.assertEqual(original,state['forecasts'])

    def test_stale_sources_invalid_scores_and_cutoffs_are_rejected(self):
        model=self.model()
        with self.assertRaises(ValueError):
            observe_and_register(new_state(),model,sha(model),self.games,self.quality(self.now-timedelta(hours=2)),self.now)
        source=self.quality();source['nightly']['fixtures'][0]['kickoff']=(self.now+timedelta(hours=23)).isoformat()
        result=observe_and_register(new_state(),model,sha(model),self.games,source,self.now)
        self.assertEqual(result['registered'],0)
        source=self.quality();source['nightly']['fixtures'][0]['fixtureId']=True
        with self.assertRaises(ValueError):observe_and_register(new_state(),model,sha(model),self.games,source,self.now)
        state=new_state();observe_and_register(state,model,sha(model),self.games,self.quality(),self.now)
        later=datetime(2026,11,21,20,tzinfo=timezone.utc);source=self.quality(later)
        source['nightly']['fixtures'][0].update(state='settled',latest={'status':'settled','providerStatus':'FINISHED',
            'checkedAt':later.isoformat(),'score':{'home':True,'away':0}})
        with self.assertRaises(ValueError):observe_and_register(state,model,sha(model),self.games,source,later)

    def test_actual_registration_after_calculation_must_still_be_24_hours_early(self):
        source=self.quality();source['nightly']['fixtures'][0]['kickoff']=(self.now+timedelta(hours=25)).isoformat()
        moments=iter([self.now,self.now+timedelta(hours=2)])
        state=new_state();model=self.model()
        result=observe_and_register(state,model,sha(model),self.games,source,self.now,
                                    registration_clock=lambda:next(moments))
        self.assertEqual(result['registered'],0);self.assertEqual(state['forecasts'],{})

    def test_prospective_features_reproduce_historical_features_at_the_same_cutoff(self):
        source=ROOT/'docs/research/neural-2026-11-16/feature-rows.json.gz'
        rows=json.loads(gzip.decompress(source.read_bytes()))
        row=next(r for r in reversed(rows) if r['division']=='E0' and r['v1_available'])
        f={'home':row['home'],'away':row['away'],'kickoff':row['date']+'T15:00:00Z'}
        actual=forecast_features(self.games,f,date.fromisoformat(row['cutoff_date']))
        np.testing.assert_allclose(actual['features'],row['features'],atol=2e-15,rtol=0)
        self.assertEqual(actual['base'],row['base'])
        self.assertLess(actual['latestInputMatch'],actual['inputCutoffExclusive'])

    def test_unchanged_cycle_skips_fit_and_failed_observation_preserves_saved_state(self):
        rows=[{'date':(date(2024,1,1)+timedelta(days=i)).isoformat(),
            'cutoff_date':(date(2024,1,1)+timedelta(days=i-1)).isoformat(),'division':d,'season':2024,
            'home':'A','away':'B','actual':i%3,'features':[float(i%5)]*56,'base':[.4,.3,.3],
            'frequency':[1/3]*3,'v1':None,'v1_available':False} for i in range(600) for d in ('E0','E1','E2')]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);state=root/'state';source=root/'quality.json'
            source.write_text(json.dumps(self.quality()))
            with patch('research.trainer_agents.prepare_rows',return_value=(rows,{'mode':'full'})), \
                 patch('research.trainer_agents.train',side_effect=lambda rows,*args:self.network(len(rows))) as fit:
                first=run(state,root/'first',self.now.date(),snapshot=True,quality_path=source,now=self.now)
            self.assertEqual(fit.call_count,2);self.assertEqual(first['roles']['trainer']['fits'],2)
            self.assertGreater(first['timingsSeconds']['total'],0)
            self.assertTrue(all(v>=0 for v in first['timingsSeconds'].values()))
            original_model=(state/'model.json').read_bytes();original_forecasts=json.loads((state/'state.json').read_text())['forecasts']
            later=self.now+timedelta(minutes=20);source.write_text(json.dumps(self.quality(later)))
            with patch('research.trainer_agents.train') as fit:
                second=run(state,root/'second',self.now.date(),snapshot=True,quality_path=source,now=later)
            fit.assert_not_called();self.assertEqual(second['roles']['trainer']['fits'],0)
            self.assertEqual(second['featurePreparation']['mode'],'not_needed')
            self.assertEqual(second['timingsSeconds']['features'],0)
            self.assertEqual(second['timingsSeconds']['finalFit'],0)
            self.assertEqual(second['trainedAt'],first['trainedAt']);self.assertEqual(second['modelSha256'],first['modelSha256'])
            self.assertEqual(original_forecasts,json.loads((state/'state.json').read_text())['forecasts'])
            saved=(state/'state.json').read_bytes();source.write_text(json.dumps(self.quality(self.now-timedelta(hours=2))))
            with self.assertRaises(ValueError):run(state,root/'failed',self.now.date(),snapshot=True,quality_path=source,now=later)
            self.assertEqual((state/'model.json').read_bytes(),original_model);self.assertEqual((state/'state.json').read_bytes(),saved)
            self.assertEqual(json.loads((root/'failed/report.json').read_text())['status'],'failed')

    def test_publisher_rejects_overwrites_and_preserves_last_success_on_failure(self):
        state=new_state();model=self.model();observe_and_register(state,model,sha(model),self.games,self.quality(),self.now)
        changed=copy.deepcopy(state);changed['forecasts']['9000']['candidate']=[.9,.05,.05]
        with self.assertRaises(ValueError):merge_state(state,changed)
        removed=copy.deepcopy(state);removed['forecasts'].clear()
        with self.assertRaises(ValueError):merge_state(state,removed)
        report={'schemaVersion':1,'protocol':PROTOCOL,'policy':POLICY,'status':'success','checkedAt':self.now.isoformat(),
            'roles':{'trainer':{'fits':0},'examiner':{'publicPromotionAllowed':False}},
            'modelSha256':'a'*64,'dataSha256':'b'*64,'inputFingerprint':'c'*64,'planSha256':'d'*64,'codeSha256':'e'*64,
            'promotedToDefault':False,'independentAdvantageEstablished':False}
        args={'repository':'FoXsYsOfficial1909/Footpredict','run_id':'123','attempt':'1','commit':'f'*40}
        old=merge_latest(None,report,**args)
        failed={**report,'status':'failed','checkedAt':(self.now+timedelta(hours=1)).isoformat()}
        after=merge_latest(old,failed,**args);self.assertEqual(after['latestSuccessful'],old['latestSuccessful'])
        with self.assertRaises(ValueError):validate_report({**report,'policy':{**POLICY,'automaticPromotion':True}})
        with self.assertRaises(ValueError):validate_report({**report,'roles':{**report['roles'],'trainer':{'fits':3}}})


if __name__=='__main__':unittest.main()
