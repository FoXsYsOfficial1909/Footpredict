import test from 'node:test';
import assert from 'node:assert/strict';
import {memoryD1} from './helpers/d1.mjs';
import {nightlyFixture,readLimited} from './helpers/nightly.mjs';
import {comparisonSchema,registerComparison,settleComparisons,comparisonReport,comparisonEvidence,NIGHTLY_COMPARISON_PROTOCOL as PROTOCOL} from '../worker/comparison.mjs';
import {predictV2} from '../worker/model-v2.mjs';
import {runAutomation,automationStatus,claimJob,enqueue,automationStorage,dailyReport} from '../worker/automation.mjs';
import {modelDigest} from '../worker/nightly-model.mjs';
import worker from '../worker/index.mjs';

const start=Date.parse('2030-01-01T12:00:00Z'),DAY=86400000;
const fixture={id:900001,utcDate:'2030-01-05T15:00:00Z',status:'TIMED',competition:{code:'PL'},season:{startDate:'2029-08-01'},homeTeam:{id:61,name:'Chelsea FC'},awayTeam:{id:66,name:'Manchester United FC'}};
const payload={fixtureId:fixture.id,kickoff:fixture.utcDate};
const register=state=>registerComparison(state.db,state.services,payload,PROTOCOL);
const evidence=state=>comparisonEvidence(state.db,fixture.id,PROTOCOL);
const report=(state,now=state.services.now())=>comparisonReport(state.db,now,PROTOCOL);

async function environment(){
  const db=memoryD1();await db.batch([...comparisonSchema(),...comparisonSchema(PROTOCOL)].map(sql=>db.prepare(sql)));
  let now=start,bundle=await nightlyFixture(),games=[fixture],loads=0;
  const calls=[],services={now:()=>now,readLimited,budgetUsage:async()=>0,predictCandidate:predictV2,
    loadNightly:async()=>{loads++;return bundle;},
    predictV1:()=>({modelVersion:'poisson-1.0.0',probabilities:[.4,.3,.3],baseline:[.5,.25,.25],
      inputs:[{id:1,utcDate:'2029-12-28T12:00:00Z'}],stats:{baseCounts:[.5,.25,.25]},parameters:{finishSafetyHours:4}}),
    upstream:async(provider,path,options)=>{calls.push({provider,path,options});return {data:path.startsWith('/matches/')?
      games.find(g=>g.id===Number(path.split('/').at(-1))):{matches:games},fetchedAt:new Date(now).toISOString()};},
    fetch:async()=>new Response('<rss><channel></channel></rss>'),reserve:async()=>{},cachePut:async()=>{},settleV1:async()=>({processed:0})};
  const state={db,services,calls,setTime:value=>{now=value;},setGames:value=>{games=value;},setBundle:value=>{bundle=value;},loadCount:()=>loads};
  return state;
}

test('four forecasts and the exact trained weights are frozen atomically in their own ledger',async()=>{
  const state=await environment();
  await registerComparison(state.db,state.services,payload);
  const previous=JSON.stringify(await comparisonEvidence(state.db,fixture.id));
  assert.equal((await register(state)).result.registered,true);
  const packet=await evidence(state);
  assert.equal(packet.protocol,PROTOCOL);assert.deepEqual(Object.keys(packet.models),['v1','v2','base','candidate']);
  assert.equal(await modelDigest(packet.candidateArtifact.modelJson),packet.models.candidate.modelSha256);
  assert.equal(JSON.parse(packet.candidateArtifact.modelJson).model_version,packet.models.candidate.modelVersion);
  assert.equal(packet.candidateArtifact.trainingReport.modelSha256,packet.models.candidate.modelSha256);
  assert.equal(packet.policy.metricsScope,'nightly_refresh_procedure');assert.equal(packet.policy.automaticPromotion,false);
  assert.equal(JSON.stringify(await comparisonEvidence(state.db,fixture.id)),previous);
  const result=await report(state);assert.equal(result.totals.pending,1);assert.equal(result.versions.length,1);
  for(const m of Object.values(result.models)){assert.equal(m.n,0);assert.equal(m.brier,null);}
  state.db.close();
});

test('a new nightly model or a repeated task cannot replace the first four-way prediction',async()=>{
  const state=await environment();await register(state);
  const before=JSON.stringify(await evidence(state)),calls=state.calls.length,loads=state.loadCount();
  state.services.loadNightly=async()=>{throw new Error('Must not refetch a registered forecast');};
  state.setTime(Date.parse(fixture.utcDate)+1000);
  assert.equal((await register(state)).result.reason,'already_registered');
  assert.equal(state.calls.length,calls);assert.equal(state.loadCount(),loads);assert.equal(JSON.stringify(await evidence(state)),before);state.db.close();
});

test('late forecasts and a saturated minute budget do not fetch weights or provider data',async()=>{
  const state=await environment();state.setTime(Date.parse(fixture.utcDate)-DAY+1);
  assert.equal((await register(state)).result.reason,'prediction_window_closed');
  state.setTime(start);state.services.budgetUsage=async()=>3;
  assert.equal((await register(state)).result.deferred,true);assert.equal(state.loadCount(),0);assert.equal(state.calls.length,0);state.db.close();
});

test('an unverified or stale candidate, future inputs and a changed fixture leave no partial row',async()=>{
  for(const scenario of ['hash','stale','input','reschedule']){
    const state=await environment(),bundle=await nightlyFixture();
    if(scenario==='hash')state.setBundle({...bundle,modelJson:bundle.modelJson+' '});
    if(scenario==='stale')state.setTime(start+2*DAY);
    if(scenario==='input')state.services.predictV1=()=>({modelVersion:'poisson-1.0.0',probabilities:[.4,.3,.3],baseline:[.4,.3,.3],inputs:[{id:fixture.id,utcDate:fixture.utcDate}]});
    if(scenario==='reschedule'){
      const upstream=state.services.upstream;
      state.services.upstream=async(p,path,o)=>path.startsWith('/matches/')?{data:{...fixture,utcDate:'2030-01-06T15:00:00Z'},fetchedAt:new Date(start).toISOString()}:upstream(p,path,o);
    }
    await assert.rejects(()=>register(state));assert.equal((await report(state)).totals.registered,0);
    if(['hash','stale'].includes(scenario))assert.equal(state.calls.length,0);
    state.db.close();
  }
});

test('parallel workers and the SQL clock cannot create duplicate or late forecasts',async()=>{
  const state=await environment();await Promise.all([register(state),register(state)]);
  assert.equal((await report(state)).totals.registered,1);state.db.close();
  const late=await environment();
  late.db.sqlite.function('unixepoch',value=>Math.floor((Date.parse(fixture.utcDate)-DAY)/1000));
  assert.equal((await register(late)).result.registered,false);assert.equal((await report(late)).totals.registered,0);late.db.close();
});

test('settlement uses one common result for all four models and corrections preserve the forecast',async()=>{
  const state=await environment();await register(state);const frozen=JSON.stringify(await evidence(state));
  const first=Date.parse(fixture.utcDate)+5*3600000;
  state.setTime(first);state.setGames([{...fixture,status:'FINISHED',score:{duration:'REGULAR',fullTime:{home:2,away:0}}}]);
  await settleComparisons(state.db,state.services,PROTOCOL);
  const before=await report(state);for(const m of Object.values(before.models))assert.equal(m.n,1);
  assert.ok(Math.abs(before.models.v1.brier-.54)<1e-12);assert.equal(before.versions[0].models.candidate.n,1);
  state.setTime(first+DAY+1);state.setGames([{...fixture,status:'FINISHED',score:{duration:'REGULAR',fullTime:{home:0,away:2}}}]);
  await settleComparisons(state.db,state.services,PROTOCOL);
  assert.equal((await report(state)).models.v1.correct,0);assert.equal((await report(state,first+1000)).models.v1.correct,1);
  assert.equal(state.db.sqlite.prepare('SELECT COUNT(*) AS n FROM nightly_results').get().n,2);
  assert.equal(JSON.stringify(await evidence(state)),frozen);state.db.close();
});

test('versions are separate cohorts and an incomplete candidate excludes a fixture for every model',async()=>{
  const state=await environment();await register(state);
  const other={...fixture,id:900002};state.setGames([fixture,other]);state.setBundle(await nightlyFixture({tail:'bbbbbbbbbbbb',movement:.01}));
  await registerComparison(state.db,state.services,{fixtureId:other.id,kickoff:other.utcDate},PROTOCOL);
  assert.equal((await report(state)).versions.length,2);
  const first=await evidence(state);delete first.models.candidate.probabilities;
  state.db.sqlite.prepare('UPDATE nightly_predictions SET payload=? WHERE fixture_id=?').run(JSON.stringify(first),fixture.id);
  const r=await report(state);assert.equal(r.totals.excluded,1);assert.equal(r.totals.pending,1);
  for(const version of r.versions)for(const m of Object.values(version.models))assert.equal(m.n,0);
  state.db.close();
});

test('public viewing never starts training, collects evidence or migrates the database',async()=>{
  const db=memoryD1(),env={FOOTPREDICT_DB:db,ALLOWED_ORIGIN:'https://foxsysofficial1909.github.io'};
  const request=(path,origin=env.ALLOWED_ORIGIN)=>new Request('https://worker.test'+path,{headers:{Origin:origin}});
  const before=db.sqlite.prepare('SELECT total_changes() AS n').get().n;
  const response=await worker.fetch(request('/v1/candidate-comparison'),env);assert.equal(response.status,200);
  assert.equal((await response.json()).status,'not_started');
  assert.equal((await worker.fetch(request('/v1/candidate-comparison?refresh=1'),env)).status,400);
  assert.equal((await worker.fetch(request('/v1/candidate-comparison/123/evidence'),env)).status,404);
  assert.equal((await worker.fetch(request('/v1/candidate-comparison','https://foreign.test'),env)).status,403);
  assert.equal(db.sqlite.prepare('SELECT total_changes() AS n').get().n,before);db.close();
});

test('the scheduler prioritizes the nearest deadline and persists a nightly task independently of research',async()=>{
  const state=await environment();
  state.db.sqlite.exec('CREATE TABLE predictions(id TEXT PRIMARY KEY,snapshot TEXT,saved_at TEXT); CREATE TABLE observations(seq INTEGER PRIMARY KEY,prediction_id TEXT,payload TEXT)');
  const soon={...fixture,id:900002,utcDate:'2030-01-03T15:00:00Z'};state.setGames([fixture,soon]);
  await runAutomation({FOOTPREDICT_DB:state.db,FOOTPREDICT_CACHE:{get:async()=>null}},state.services);
  const r=await report(state);assert.equal(r.totals.registered,1);assert.equal(r.fixtures[0].fixtureId,soon.id);
  const status=await automationStatus({FOOTPREDICT_DB:state.db},state.services);assert.equal(status.nightlyComparison.total,1);
  const pending=await claimJob(state.db,'candidate-comparison',start+1000);assert.equal(pending.payload.fixtureId,fixture.id);
  assert.equal(state.db.sqlite.prepare('SELECT COUNT(*) AS n FROM predictions').get().n,0);state.db.close();
});

test('a changed kickoff can reopen a closed collection job without rewriting a registered forecast',async()=>{
  const state=await environment();await automationStorage({FOOTPREDICT_DB:state.db});
  await enqueue(state.db,'candidate-comparison:'+fixture.id,'candidate-comparison',payload,start);
  state.db.sqlite.prepare("UPDATE automation_jobs SET status='complete' WHERE kind='candidate-comparison'").run();
  const changed={...payload,kickoff:'2030-01-06T15:00:00Z'};
  await enqueue(state.db,'candidate-comparison:'+fixture.id,'candidate-comparison',changed,start+1000);
  const job=await claimJob(state.db,'candidate-comparison',start+1000);assert.equal(job.payload.kickoff,changed.kickoff);
  await register(state);const before=JSON.stringify(await evidence(state));
  assert.equal((await registerComparison(state.db,state.services,changed,PROTOCOL)).result.reason,'already_registered');
  assert.equal(JSON.stringify(await evidence(state)),before);state.db.close();
});

test('daily summaries include only candidate registrations known at their cutoff and stay frozen',async()=>{
  const state=await environment(),env={FOOTPREDICT_DB:state.db,FOOTPREDICT_CACHE:{get:async()=>null}};
  state.db.sqlite.exec('CREATE TABLE predictions(id TEXT PRIMARY KEY,snapshot TEXT,saved_at TEXT); CREATE TABLE observations(seq INTEGER PRIMARY KEY,prediction_id TEXT,payload TEXT)');
  await runAutomation(env,state.services);
  const first=await dailyReport(env,'2030-01-01');
  assert.equal(first.nightlyComparison.totals.registered,0);
  assert.equal(first.currentHealth.nightlyComparison.total,1);
  state.setTime(Date.parse('2030-01-02T04:50:00Z'));
  await runAutomation(env,state.services,{reason:'50 4 * * *'});
  const second=await dailyReport(env,'2030-01-02');assert.equal(second.nightlyComparison.totals.registered,1);
  const saved=JSON.stringify(second);state.setTime(Date.parse(fixture.utcDate)+5*3600000);
  state.setGames([{...fixture,status:'FINISHED',score:{duration:'REGULAR',fullTime:{home:2,away:0}}}]);
  await settleComparisons(state.db,state.services,PROTOCOL);
  assert.equal((await report(state)).totals.settled,1);assert.equal(JSON.stringify(await dailyReport(env,'2030-01-02')),saved);state.db.close();
});

test('a failed nightly archive does not stop other scheduled work',async()=>{
  const state=await environment();
  state.db.sqlite.exec('CREATE TABLE predictions(id TEXT PRIMARY KEY,snapshot TEXT,saved_at TEXT); CREATE TABLE observations(seq INTEGER PRIMARY KEY,prediction_id TEXT,payload TEXT)');
  state.services.loadNightly=async()=>{throw Object.assign(new Error('Training archive unavailable'),{status:503});};
  const tick=await runAutomation({FOOTPREDICT_DB:state.db,FOOTPREDICT_CACHE:{get:async()=>null}},state.services);
  assert.ok(tick.outcomes.some(o=>o.id==='candidate-comparison:'+fixture.id&&o.outcome==='failed'));
  assert.ok(tick.outcomes.some(o=>o.id==='comparison:'+fixture.id&&o.outcome==='ok'));
  assert.equal((await report(state)).totals.registered,0);state.db.close();
});
