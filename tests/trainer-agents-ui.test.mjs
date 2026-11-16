import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {describeTrainerAgents,validateTrainerDashboard,verifyTrainerDashboard,probabilityPercentages} from '../trainer-agents-ui.js';

const success={schemaVersion:1,protocol:'trainer-agents-1',status:'success',checkedAt:'2026-11-16T12:00:00Z',
  trainedAt:'2026-11-16T06:00:00Z',trainingMatches:13070,modelVersion:'neural-agent-example',promotedToDefault:false,
  policy:{automaticPromotion:false,paidAIRequests:0},roles:{trainer:{parameterCount:963,fits:0,status:'unchanged',
    converged:false,stopReason:'iteration_limit'},examiner:{publicPromotionAllowed:false}},
  prospective:{registered:5,pending:5,settled:0,review:0,unavailable:0}};
const packet=()=>({schemaVersion:1,protocol:'trainer-agents-1',latestAttempt:structuredClone(success),latestSuccessful:structuredClone(success)});

test('trainer checks do not pretend to be new fits or future wins',()=>{
  const result=describeTrainerAgents(packet(),Date.parse('2026-11-16T12:30:00Z'));
  assert.match(result,/повторная подгонка не требовалась/);assert.match(result,/завершено 0/);
  assert.match(result,/сходимость не заявляется/);assert.match(result,/автоматически не заменяется/);
});
test('failed cycle preserves the old model and first failure has no invented weights',()=>{
  const p=packet();p.latestAttempt.status='failed';
  assert.match(describeTrainerAgents(p),/Предыдущие веса и прогнозы сохранены/);
  assert.match(describeTrainerAgents(p),/neural-agent-example/);
  p.latestSuccessful=null;assert.match(describeTrainerAgents(p),/Первый успешный цикл ещё не опубликован/);
});
test('wrong scope, automatic deployment and mismatched paired counts are rejected',()=>{
  for(const change of [p=>p.protocol='other',p=>p.latestAttempt.promotedToDefault=true,
    p=>p.latestAttempt.policy.paidAIRequests=10,p=>p.latestSuccessful.prospective.settled=1,
    p=>p.latestSuccessful.roles.trainer.fits=3]){
    const p=packet();change(p);assert.throws(()=>describeTrainerAgents(p));
  }
});

const fixture=(id=9000)=>({fixtureId:id,home:'Home '+id,away:'Away '+id,kickoff:'2026-11-21T15:00:00Z',
  registeredAt:'2026-11-16T13:45:00Z',modelVersion:'neural-test',status:'pending',
  candidate:[.6,.2,.2],base:[.5,.25,.25],frequency:[.4,.3,.3],v1_available:true});
const empty=()=>({n:0,brier:null,logLoss:null,accuracy:null,correct:0,topEligible:0,topTies:0,uniqueTopAccuracy:null});
const view=()=>({schemaVersion:1,protocol:'neural-agent-dashboard-1',league:'PL',maximumShown:100,
  selection:'latest_kickoff_then_fixture_id',counts:{registered:1,settled:0,pending:1,review:0,unavailable:0},
  v1FallbackSettled:0,metrics:{candidate:empty(),base:empty(),frequency:empty()},rows:[fixture()]});

test('pending forecasts have probabilities and no invented quality',()=>{
  const v=view();assert.equal(validateTrainerDashboard(v),v);
  assert.equal(v.metrics.candidate.logLoss,null);assert.equal(v.rows[0].status,'pending');
  assert.equal(v.rows[0].score,undefined);
});

test('confirmed scores and equal metric cohorts are required',()=>{
  const v=view();v.counts.pending=0;v.counts.settled=1;
  Object.assign(v.rows[0],{status:'settled',score:{home:2,away:1},actual:0,checkedAt:'2026-11-21T20:00:00Z'});
  for(const [key,loss,brier] of [['candidate',-Math.log(.6),.24],['base',-Math.log(.5),.375],['frequency',-Math.log(.4),.54]])
    v.metrics[key]={n:1,logLoss:loss,brier,accuracy:1,correct:1,topEligible:1,topTies:0,uniqueTopAccuracy:1};
  validateTrainerDashboard(v);
  for(const change of [x=>x.rows[0].actual=2,x=>x.rows[0].score.home=true,
    x=>x.rows[0].checkedAt='2026-11-21T16:00:00Z',x=>x.metrics.base.n=0]){
    const bad=structuredClone(v);change(bad);assert.throws(()=>validateTrainerDashboard(bad));
  }
});

test('wrong identities, probabilities, scope and late registration are rejected',()=>{
  for(const change of [x=>x.protocol='other',x=>x.league='ELC',x=>x.rows[0].candidate=[.7,.3,.2],
    x=>x.rows[0].registeredAt='2026-11-21T12:00:00Z',x=>x.rows[0].fixtureId=true,
    x=>x.rows[0].status='won',x=>x.rows[0].score={home:1,away:0},x=>x.rows=[],x=>x.v1FallbackSettled=1]){
    const bad=view();change(bad);assert.throws(()=>validateTrainerDashboard(bad));
  }
});

test('bounded display retains full series coverage and chronological identities',()=>{
  const v=view();v.counts.registered=v.counts.pending=103;v.rows=Array.from({length:100},(_,i)=>fixture(9003+i));
  validateTrainerDashboard(v);
  v.rows[99]=fixture(9003);assert.throws(()=>validateTrainerDashboard(v));
});

test('dashboard hash binds immutable bytes to the same report counts and metrics',async()=>{
  const v=view(),raw=JSON.stringify(v),digest=createHash('sha256').update(raw).digest('hex');
  const reference={prospective:{...v.counts,metrics:v.metrics}};
  assert.deepEqual(await verifyTrainerDashboard(raw,digest,reference),v);
  await assert.rejects(()=>verifyTrainerDashboard(raw+' ',digest,reference));
  await assert.rejects(()=>verifyTrainerDashboard(raw,digest,{prospective:{...reference.prospective,pending:2}}));
  await assert.rejects(()=>verifyTrainerDashboard(raw,'../unexpected',reference));
  const other=structuredClone(reference);other.prospective.metrics.base.logLoss=0;
  await assert.rejects(()=>verifyTrainerDashboard(raw,digest,other));
});

test('rounded display sums to 100 percent without changing saved probabilities',()=>{
  for(const values of [[1/3,1/3,1/3],[.123456,.456789,.419755],[1,0,0],[0,.0000001,.9999999]]){
    const original=[...values],shown=probabilityPercentages(values).map(v=>Number(v.replace('%','').replace(',','.')));
    assert.ok(Math.abs(shown.reduce((a,b)=>a+b,0)-100)<1e-12);
    shown.forEach((v,i)=>assert.ok(Math.abs(v-values[i]*100)<=.1));assert.deepEqual(values,original);
  }
  for(const bad of [[0,0,0],[50,25,25],[NaN,.5,.5],['.5',.25,.25]])assert.throws(()=>probabilityPercentages(bad));
});
