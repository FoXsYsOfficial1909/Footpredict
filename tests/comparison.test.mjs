import test from 'node:test';
import assert from 'node:assert/strict';
import {memoryD1} from './helpers/d1.mjs';
import {comparisonSchema,registerComparison,observeComparison,settleComparisons,comparisonReport,comparisonEvidence} from '../worker/comparison.mjs';
import {predictV2} from '../worker/model-v2.mjs';
import worker from '../worker/index.mjs';
import {runAutomation} from '../worker/automation.mjs';

const start=Date.parse('2030-01-01T12:00:00Z'),kickoff='2030-01-05T15:00:00Z';
const fixture={id:900001,utcDate:kickoff,status:'TIMED',competition:{code:'PL'},season:{startDate:'2029-08-01'},homeTeam:{id:61,name:'Chelsea FC'},awayTeam:{id:66,name:'Manchester United FC'}};
const payload={fixtureId:fixture.id,kickoff};
async function setup(){
  const db=memoryD1();await db.batch(comparisonSchema().map(sql=>db.prepare(sql)));
  let now=start,game=fixture;
  const calls=[],services={now:()=>now,budgetUsage:async()=>0,predictCandidate:predictV2,
    predictV1:()=>({modelVersion:'poisson-1.0.0',probabilities:[.4,.3,.3],baseline:[.5,.25,.25],inputs:[{id:1,utcDate:'2029-12-28T12:00:00Z'}],stats:{baseCounts:[.5,.25,.25]},parameters:{finishSafetyHours:4}}),
    upstream:async(provider,path,options)=>{calls.push({provider,path,options});return {data:path.startsWith('/matches/')?game:{matches:[game]},fetchedAt:new Date(now).toISOString()};}};
  return {db,services,calls,setTime:t=>{now=t;},setGame:g=>{game=g;}};
}

test('three models are frozen in one row before kickoff without touching the original v1 ledger',async()=>{
  const state=await setup(),{db,services,calls}=state;
  db.sqlite.exec("CREATE TABLE predictions(id TEXT,payload TEXT); INSERT INTO predictions VALUES('original','unchanged')");
  const result=await registerComparison(db,services,payload);assert.equal(result.result.registered,true);
  const packet=await comparisonEvidence(db,fixture.id);
  assert.equal(packet.protocol,'paired-live-1');assert.equal(packet.savedAt,new Date(start).toISOString());
  assert.deepEqual(Object.keys(packet.models),['v1','v2','base']);assert.equal(packet.policy.minimumHorizonHours,24);
  assert.ok(calls.at(-1).options.fresh);assert.ok(calls.every(c=>c.provider==='fd'));
  assert.equal(db.sqlite.prepare('SELECT payload FROM predictions').get().payload,'unchanged');
  const report=await comparisonReport(db,start);assert.equal(report.totals.pending,1);assert.equal(report.models.v1.brier,null);db.close();
});

test('registration is immutable and an existing forecast spends no further provider quota',async()=>{
  const state=await setup();await registerComparison(state.db,state.services,payload);
  const before=JSON.stringify(await comparisonEvidence(state.db,fixture.id)),count=state.calls.length;
  state.services.predictV1=()=>{throw new Error('Must not recalculate');};state.setTime(Date.parse(kickoff)+1000);
  assert.equal((await registerComparison(state.db,state.services,payload)).result.reason,'already_registered');
  assert.equal(state.calls.length,count);assert.equal(JSON.stringify(await comparisonEvidence(state.db,fixture.id)),before);state.db.close();
});

test('late registration and minute quota defer before any upstream operation',async()=>{
  const state=await setup();state.setTime(Date.parse(kickoff)-12*3600000);
  assert.equal((await registerComparison(state.db,state.services,payload)).result.reason,'prediction_window_closed');
  state.setTime(start);state.services.budgetUsage=async()=>3;
  assert.equal((await registerComparison(state.db,state.services,payload)).result.deferred,true);
  assert.equal(state.calls.length,0);assert.equal((await comparisonReport(state.db,start)).totals.registered,0);state.db.close();
});

test('a final fixture change or an invalid model prevents all three forecasts from being saved',async()=>{
  for(const change of ['identity','probabilities','version']){
    const state=await setup(),original=state.services.upstream;
    if(change==='identity')state.services.upstream=async(provider,path,options)=>path.startsWith('/matches/')?{data:{...fixture,awayTeam:{id:1,name:'Other'}},fetchedAt:new Date(start).toISOString()}:original(provider,path,options);
    else state.services.predictV1=()=>({modelVersion:change==='version'?'different-v1':'poisson-1.0.0',probabilities:change==='probabilities'?[2,0,0]:[.4,.3,.3],baseline:[.5,.25,.25]});
    await assert.rejects(()=>registerComparison(state.db,state.services,payload));
    assert.equal((await comparisonReport(state.db,start)).totals.registered,0);state.db.close();
  }
});

test('parallel registration cannot split models into different observations',async()=>{
  const state=await setup();await Promise.all([registerComparison(state.db,state.services,payload),registerComparison(state.db,state.services,payload)]);
  assert.equal(state.db.sqlite.prepare('SELECT COUNT(*) AS n FROM comparison_predictions').get().n,1);
  const packet=await comparisonEvidence(state.db,fixture.id);assert.equal(Object.keys(packet.models).length,3);state.db.close();
});

test('settlement corrections append observations, preserve forecasts and respect report cutoffs',async()=>{
  const state=await setup();await registerComparison(state.db,state.services,payload);
  const before=JSON.stringify(await comparisonEvidence(state.db,fixture.id)),firstCheck=Date.parse(kickoff)+5*3600000;
  state.setTime(firstCheck);state.setGame({...fixture,status:'FINISHED',score:{duration:'REGULAR',fullTime:{home:2,away:0}}});
  await settleComparisons(state.db,state.services);
  let report=await comparisonReport(state.db,firstCheck);
  for(const key of ['v1','v2','base'])assert.equal(report.models[key].n,1);
  assert.equal(report.models.v1.correct,1);assert.ok(Math.abs(report.models.v1.brier-.54)<1e-12);
  state.setTime(firstCheck+86400001);state.setGame({...fixture,status:'FINISHED',score:{duration:'REGULAR',fullTime:{home:0,away:2}}});
  await settleComparisons(state.db,state.services);
  assert.equal(state.db.sqlite.prepare('SELECT COUNT(*) AS n FROM comparison_results').get().n,2);
  report=await comparisonReport(state.db,firstCheck+86400001);assert.equal(report.models.v1.correct,0);
  assert.equal((await comparisonReport(state.db,firstCheck+1000)).models.v1.correct,1);
  assert.equal(JSON.stringify(await comparisonEvidence(state.db,fixture.id)),before);state.db.close();
});

test('rescheduled, cancelled and unverified regulation scores never become settled results',async()=>{
  const state=await setup();await registerComparison(state.db,state.services,payload);const packet=await comparisonEvidence(state.db,fixture.id);
  assert.equal(observeComparison(packet,{...fixture,utcDate:'2030-01-06T15:00:00Z'},'2030-01-06T20:00:00Z').status,'review_required');
  assert.equal(observeComparison(packet,{...fixture,status:'CANCELLED'},'2030-01-06T20:00:00Z').status,'cancelled');
  assert.equal(observeComparison(packet,{...fixture,status:'FINISHED',score:{duration:'EXTRA_TIME',fullTime:{home:2,away:0}}},'2030-01-06T20:00:00Z').status,'review_required');
  assert.equal(observeComparison(packet,{...fixture,status:'FINISHED',score:{duration:'REGULAR',fullTime:{home:2,away:0}}},'2030-01-05T16:00:00Z').status,'review_required');state.db.close();
});

test('missing any one distribution excludes a fixture from every comparison denominator',async()=>{
  const state=await setup();await registerComparison(state.db,state.services,payload);
  const packet=await comparisonEvidence(state.db,fixture.id);delete packet.models.base;
  state.db.sqlite.prepare('UPDATE comparison_predictions SET payload=?').run(JSON.stringify(packet));
  const report=await comparisonReport(state.db,start);assert.equal(report.totals.excluded,1);
  for(const key of ['v1','v2','base'])assert.equal(report.models[key].n,0);state.db.close();
});

test('comparison viewing is public and read-only, including before the first scheduled migration',async()=>{
  const db=memoryD1(),env={FOOTPREDICT_DB:db,ALLOWED_ORIGIN:'https://foxsysofficial1909.github.io'};
  const req=path=>new Request('https://worker.test'+path,{headers:{Origin:env.ALLOWED_ORIGIN}});
  const before=db.sqlite.prepare('SELECT total_changes() AS n').get().n;
  const result=await worker.fetch(req('/v1/comparison'),env);assert.equal(result.status,200);assert.equal((await result.json()).status,'not_started');
  assert.equal((await worker.fetch(req('/v1/comparison?refresh=1'),env)).status,400);
  assert.equal((await worker.fetch(req('/v1/comparison/123/evidence'),env)).status,404);
  assert.equal(db.sqlite.prepare('SELECT total_changes() AS n').get().n,before);db.close();
});

test('the persistent scheduler seeds and runs a future comparison independently of research collection',async()=>{
  const state=await setup();
  state.db.sqlite.exec('CREATE TABLE predictions(id TEXT PRIMARY KEY,snapshot TEXT,saved_at TEXT); CREATE TABLE observations(seq INTEGER PRIMARY KEY,prediction_id TEXT,payload TEXT)');
  Object.assign(state.services,{fetch:async()=>new Response('<rss><channel></channel></rss>'),readLimited:async body=>new Response(body).text(),
    reserve:async()=>{},cachePut:async()=>{},settleV1:async()=>({processed:0})});
  const result=await runAutomation({FOOTPREDICT_DB:state.db,FOOTPREDICT_CACHE:{get:async()=>null}},state.services);
  assert.equal(result.status,'complete');assert.ok(result.outcomes.some(row=>row.id==='comparison:'+fixture.id&&row.outcome==='ok'));
  assert.equal((await comparisonReport(state.db,start)).totals.registered,1);
  assert.equal(state.db.sqlite.prepare('SELECT COUNT(*) AS n FROM predictions').get().n,0);state.db.close();
});
