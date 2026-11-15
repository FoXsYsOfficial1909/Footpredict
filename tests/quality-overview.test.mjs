import test from 'node:test';
import assert from 'node:assert/strict';
import {describeQualityOverview} from '../quality-ui.js';

const metrics=(n,correct)=>({n,correct,topEligible:n,ties:0,accuracy:n?correct/n:null,brier:n?0.6:null,logLoss:n?1:null});
function packet(n=2){return {schemaVersion:1,cohorts:{v1:{metrics:metrics(12,2)}},nightly:{
  protocol:'nightly-live-1',status:'active',totals:{registered:n+1,settled:n,pending:1,review:0,excluded:0},
  models:{v1:metrics(n,n?1:0),candidate:metrics(n,n?2:0),base:metrics(n,n?1:0)},
  policy:{minimumHorizonHours:24,sharedRegistrationTime:true,automaticPromotion:false},
  fixtureScope:{total:n+1,returned:n+1},fixtures:[...Array.from({length:n},(_,i)=>({state:'settled',kickoff:`2026-11-0${i+1}T15:00:00Z`})),{state:'pending',kickoff:'2026-11-21T15:00:00Z'}],
  versions:[{totals:{settled:n}}]}};}

test('overview compares exactly the shared cohort and keeps the old percentage out',()=>{
  const data=packet(),before=structuredClone(data),view=describeQualityOverview(data);
  assert.equal(view.state,'scored');assert.equal(view.totals.settled,2);
  assert.deepEqual(view.models.map(m=>m.key),['v1','candidate','base']);
  assert.deepEqual(view.models.map(m=>m.metrics.n),[2,2,2]);
  assert.equal(view.models[0].metrics.accuracy,.5);assert.equal(view.models[1].metrics.accuracy,1);
  assert.equal(view.settledVersionCount,1);assert.equal(view.period.from,'2026-11-01T15:00:00.000Z');
  assert.deepEqual(data,before);
});

test('no future results is waiting with null metrics, never a legacy fallback',()=>{
  const view=describeQualityOverview(packet(0));assert.equal(view.state,'waiting');
  assert.deepEqual(view.models.map(m=>m.metrics.n),[0,0,0]);
  assert.ok(view.models.every(m=>m.metrics.accuracy===null));assert.equal(view.period,null);
});

test('inconsistent counts, invalid metrics or changed protocol do not produce a comparison',()=>{
  for(const change of [d=>d.nightly.models.candidate.n++,d=>d.nightly.models.base.brier=NaN,
    d=>d.nightly.models.v1.accuracy=.9,d=>d.nightly.totals.registered++,
    d=>d.nightly.policy.sharedRegistrationTime=false,d=>d.nightly.protocol='other',d=>d.nightly=null]){
    const data=packet();change(data);assert.equal(describeQualityOverview(data).state,'unavailable');
  }
});

test('older reports can show the explicitly named v1/v2 shared series',()=>{
  const data=packet();data.prospective={...data.nightly,protocol:'paired-live-1'};
  data.prospective.models.v2=data.prospective.models.candidate;delete data.nightly;
  const view=describeQualityOverview(data);assert.equal(view.nightly,false);
  assert.deepEqual(view.models.map(m=>m.key),['v1','v2','base']);
  data.nightly=null;assert.equal(describeQualityOverview(data).state,'unavailable');
});

test('ties stay in probability errors and shortened lists cannot imply a full period',()=>{
  const data=packet();data.nightly.models.candidate={...metrics(2,1),topEligible:1,ties:1,accuracy:1};
  data.nightly.fixtureScope.returned=1;data.nightly.fixtures=data.nightly.fixtures.slice(0,1);
  const view=describeQualityOverview(data);assert.equal(view.state,'scored');assert.equal(view.listTruncated,true);
  assert.equal(view.period,null);assert.equal(view.models[1].metrics.n,2);assert.equal(view.models[1].metrics.topEligible,1);
  data.nightly.models.candidate.accuracy=.5;assert.equal(describeQualityOverview(data).state,'unavailable');
});
