import test from 'node:test';
import assert from 'node:assert/strict';
import {memoryD1} from './helpers/d1.mjs';
import {automationStorage,enqueue,claimJob,finishJob,retryTime,reportWindow,recordPrimary,scoreShadow,
  runAutomation,automationStatus,dailyReport,shadowHistory,shadowEvidence} from '../worker/automation.mjs';
import {predictV2} from '../worker/model-v2.mjs';
import SOURCES from '../worker/research-sources.mjs';

const initial=Date.parse('2026-10-28T12:00:00Z');
const fixture={id:560626,utcDate:'2026-10-31T12:30:00Z',status:'TIMED',competition:{code:'PL'},season:{startDate:'2026-08-21'},
  homeTeam:{id:61,name:'Chelsea FC'},awayTeam:{id:66,name:'Manchester United FC'}};
const earlier={...fixture,id:123,utcDate:'2026-10-25T14:00:00Z',status:'FINISHED',score:{duration:'REGULAR',fullTime:{home:2,away:1}}};
const rss='<rss><channel><item><title>Chelsea prepare for United</title><link>https://www.bbc.com/sport/football/articles/one</link><pubDate>Tue, 27 Oct 2026 12:00:00 GMT</pubDate><description>Both teams have begun preparing for the next match.</description></item></channel></rss>';

function environment() {
  const db=memoryD1(),store=new Map();let clock=initial,games=[earlier,fixture];
  db.sqlite.exec('CREATE TABLE predictions (id TEXT PRIMARY KEY,snapshot TEXT,saved_at TEXT); CREATE TABLE observations (seq INTEGER PRIMARY KEY,prediction_id TEXT,payload TEXT)');
  const env={FOOTPREDICT_DB:db,FOOTPREDICT_CACHE:{get:async(key,type)=>{
    const item=store.get(key);return item&&item.expires>clock?item.value:null;
  }},AI:{run:async()=>({choices:[{finish_reason:'stop',message:{content:'{"facts":[]}'}}]})}};
  const calls=[];
  const services={now:()=>clock,fetch:async()=>new Response(rss),readLimited:async body=>new Response(body).text(),
    reserve:async()=>{},budgetUsage:async()=>0,predictCandidate:predictV2,settleV1:async()=>({processed:0}),
    cachePut:async(key,value,ttl)=>store.set(key,{value,expires:clock+ttl*1000}),
    upstream:async(provider,path)=>{
      calls.push([provider,path]);
      if(provider==='af')throw Object.assign(new Error('Free plans do not have access to this season'),{status:422});
      if(path.startsWith('/matches/'))return {data:fixture,fetchedAt:new Date(clock).toISOString()};
      return {data:{matches:games},fetchedAt:new Date(clock).toISOString()};
    }};
  return {db,env,services,calls,setTime:value=>{clock=value;},setMatches:value=>{games=value;}};
}

test('a claimed task is exclusive, recovers after worker death and rejects stale completion',async()=>{
  const db=memoryD1();await automationStorage({FOOTPREDICT_DB:db});
  await enqueue(db,'news:test','news',{name:'test'},initial);
  const one=await claimJob(db,'news',initial,{leaseMs:1000});
  assert.ok(one);assert.equal(await claimJob(db,'news',initial+500),null);
  const successor=await claimJob(db,'news',initial+1001);
  assert.ok(successor);assert.notEqual(one.lease_token,successor.lease_token);
  assert.equal(await finishJob(db,one,initial+1100,{result:{bad:'stale'}}),false);
  assert.equal(await finishJob(db,successor,initial+1200,{result:{articles:2}}),true);
  const row=await db.prepare('SELECT * FROM automation_jobs WHERE id=?1').bind('news:test').first();
  assert.equal(JSON.parse(row.result).articles,2);assert.equal(row.attempts,0);
  db.close();
});

test('retry backoff respects daily reset and does not spin on a quota',()=>{
  assert.equal(new Date(retryTime({status:429,message:'daily_budget'},initial)).toISOString(),'2026-10-29T00:01:00.000Z');
  assert.equal(retryTime(new Error('temporary'),initial,1)-initial,15*60000);
  assert.equal(retryTime(new Error('temporary'),initial,10)-initial,2*3600000);
});

test('daily report uses 07:50 Moscow including year boundaries',()=>{
  assert.equal(reportWindow(Date.parse('2027-01-01T04:49:59Z')).day,'2026-12-31');
  const window=reportWindow(Date.parse('2027-01-01T04:50:00Z'));
  assert.equal(window.day,'2027-01-01');assert.equal(window.scheduledFor,'2027-01-01T04:50:00.000Z');
});

test('persistent pipeline runs despite a settlement failure and never alters original predictions',async()=>{
  const state=environment();
  await state.db.prepare('INSERT INTO predictions VALUES (?1,?2,?3)').bind('old-v1','{"marker":"immutable-original"}','2026-10-27T10:00:00Z').run();
  state.services.settleV1=async()=>{throw new Error('Temporary provider failure');};
  const tick=await runAutomation(state.env,state.services);
  assert.equal(tick.status,'complete');assert.ok(tick.outcomes.some(row=>row.id==='system:settle-v1'&&row.outcome==='failed'));
  const status=await automationStatus(state.env,state.services);
  assert.equal(status.state,'running');assert.equal(status.news.teams,2);assert.equal(status.forecasts.total,1);
  assert.ok(status.jobs.some(row=>row.kind==='research'&&row.result.partial));
  assert.equal((await state.db.prepare('SELECT snapshot FROM predictions').first()).snapshot,'{"marker":"immutable-original"}');
  const history=await shadowHistory(state.env);
  assert.equal(history.items.length,1);assert.equal(history.items[0].latest.status,'pending');
  const report=await dailyReport(state.env);
  assert.equal(report.day,'2026-10-28');assert.ok(report.lateBySeconds>0);
  // A second tick performs no duplicate work; the first report is immutable.
  const before=JSON.stringify(report),calls=state.calls.length;
  await runAutomation(state.env,state.services);
  assert.equal(state.calls.length,calls);assert.equal(JSON.stringify(await dailyReport(state.env)),before);
  state.setTime(initial+71*60000);
  assert.equal((await automationStatus(state.env,state.services)).state,'delayed');
  state.db.close();
});

test('primary forecast is frozen across later model updates and cannot be backfilled after kickoff',async()=>{
  const state=environment();await runAutomation(state.env,state.services);
  const before=(await shadowHistory(state.env)).items[0];
  const packet={fixture:{id:560626,kickoff:fixture.utcDate,home:'Chelsea FC',away:'Manchester United FC'},
    featureCutoffAt:new Date(initial+1000).toISOString(),teams:[{id:61},{id:66}],
    candidate:{...predictV2('Chelsea FC','Manchester United FC',fixture.utcDate),generatedAt:new Date(initial+1000).toISOString(),probabilities:[.99,.005,.005]}};
  await state.db.prepare('INSERT INTO research_snapshots VALUES (?1,?2,?3,?4,?5)')
    .bind('later',560626,packet.featureCutoffAt,'2026-10-28T12',JSON.stringify(packet)).run();
  await recordPrimary(state.db,560626,initial+2000);
  assert.deepEqual((await shadowHistory(state.env)).items[0].probabilities,before.probabilities);
  assert.deepEqual((await shadowEvidence(state.env,560626)).candidate.probabilities,before.probabilities);
  await assert.rejects(()=>shadowEvidence(state.env,99999),error=>error.status===404);
  packet.fixture.id=999;packet.fixture.kickoff='2026-10-28T12:30:00Z';
  await state.db.prepare('INSERT INTO research_snapshots VALUES (?1,?2,?3,?4,?5)')
    .bind('too-late',999,packet.featureCutoffAt,'2026-10-28T12',JSON.stringify(packet)).run();
  assert.equal(await recordPrimary(state.db,999,initial+2000),false);
  state.db.close();
});

test('an existing primary snapshot is adopted even when provider quota delays refreshing its card',async()=>{
  const state=environment();await runAutomation(state.env,state.services);
  await state.db.prepare('DELETE FROM automation_predictions').run();
  await state.db.prepare("UPDATE automation_jobs SET due_at=?1 WHERE kind='research'").bind(initial).run();
  state.services.budgetUsage=async()=>3;
  const before=state.calls.length;await runAutomation(state.env,state.services);
  assert.equal(state.calls.length,before);assert.equal((await shadowHistory(state.env)).items.length,1);
  const job=await state.db.prepare("SELECT result FROM automation_jobs WHERE kind='research'").first();
  assert.equal(JSON.parse(job.result).reason,'provider_minute_budget');state.db.close();
});

test('shadow scores use frozen probabilities and exclude rescheduled or uncertain results',()=>{
  const prediction={fixture_id:560626,kickoff_at:fixture.utcDate,saved_at:new Date(initial).toISOString(),
    probabilities:'[0.4,0.3,0.3]',metadata:'{"homeId":61,"awayId":66}'};
  const finished={...fixture,status:'FINISHED',score:{duration:'REGULAR',fullTime:{home:2,away:0}}};
  const result=scoreShadow(prediction,finished,'2026-10-31T17:00:00Z');
  assert.equal(result.status,'settled');assert.ok(Math.abs(result.brier-.54)<1e-12);assert.equal(result.correct,1);
  assert.equal(scoreShadow(prediction,{...finished,utcDate:'2026-11-01T12:30:00Z'},'2026-11-01T17:00:00Z').status,'review_required');
  assert.equal(scoreShadow(prediction,finished,'2026-10-31T15:00:00Z').status,'review_required');
  assert.equal(scoreShadow(prediction,{...finished,homeTeam:{id:999}},'2026-10-31T17:00:00Z').status,'review_required');
});

test('global coordinator lease prevents overlapping scheduled ticks',async()=>{
  const state=environment();await automationStorage(state.env);
  await enqueue(state.db,'system:tick','coordinator',{},initial);
  await claimJob(state.db,'coordinator',initial,{leaseMs:240000});
  assert.equal((await runAutomation(state.env,state.services)).status,'already_running');
  assert.equal(state.calls.length,0);state.db.close();
});

test('a terminated coordinator is shown as recovering rather than a completed healthy run',async()=>{
  const state=environment();await automationStorage(state.env);
  await state.db.prepare('INSERT INTO automation_meta (key,value) VALUES (?1,?2)').bind('tick',JSON.stringify({startedAt:new Date(initial).toISOString()})).run();
  state.setTime(initial+5*60000);
  assert.equal((await automationStatus(state.env,state.services)).state,'recovering');
  await runAutomation(state.env,state.services);
  assert.equal((await automationStatus(state.env,state.services)).state,'running');state.db.close();
});

test('missing first trigger becomes delayed and old completed jobs do not hide current work',async()=>{
  const state=environment();await automationStorage(state.env);
  await state.db.prepare("UPDATE automation_meta SET value=?1 WHERE key='installed'").bind(JSON.stringify({installedAt:new Date(initial-71*60000).toISOString()})).run();
  assert.equal((await automationStatus(state.env,state.services)).state,'delayed');
  const statements=[];
  for(let i=0;i<160;i++)statements.push(state.db.prepare("INSERT INTO automation_jobs (id,kind,payload,status,due_at,last_finished_at) VALUES (?1,'research','{}','complete',?2,?3)")
    .bind('old:'+i,initial-20*86400000,new Date(initial-20*86400000).toISOString()));
  await state.db.batch(statements);
  await enqueue(state.db,'research:current','research',{fixtureId:560626,kickoff:fixture.utcDate},initial);
  const jobs=(await automationStatus(state.env,state.services)).jobs;
  assert.equal(jobs.length,1);assert.equal(jobs[0].id,'research:current');state.db.close();
});

test('a rescheduled fixture reopens collection without resetting an unrelated job',async()=>{
  const state=environment();await automationStorage(state.env);
  await enqueue(state.db,'research:1','research',{fixtureId:1,kickoff:'2026-10-27T12:00:00Z'},initial);
  const job=await claimJob(state.db,'research',initial);
  await finishJob(state.db,job,initial,{complete:true});
  await enqueue(state.db,'research:1','research',{fixtureId:1,kickoff:'2026-10-31T12:00:00Z'},initial+1000);
  assert.ok(await claimJob(state.db,'research',initial+1000));
  await enqueue(state.db,'news:one','news',{name:'Chelsea FC'},initial+900000);
  await enqueue(state.db,'news:one','news',{name:'Chelsea FC'},initial);
  assert.equal(await claimJob(state.db,'news',initial),null);state.db.close();
});

test('a complete new-season roster retires old feeds and returning clubs can resume',async()=>{
  const state=environment();await runAutomation(state.env,state.services);
  const roster=Object.keys(SOURCES.teams).filter(name=>name!=='Chelsea FC').slice(0,20);
  const games=[];
  for(let i=0;i<20;i+=2)games.push({...fixture,id:1000+i,homeTeam:{id:100+i,name:roster[i]},awayTeam:{id:101+i,name:roster[i+1]}});
  state.setMatches(games);state.setTime(initial+6*3600000+1000);
  await runAutomation(state.env,state.services);
  const old=await state.db.prepare("SELECT status FROM automation_jobs WHERE id='news:chelsea'").first();
  assert.equal(old.status,'complete');
  await enqueue(state.db,'news:chelsea','news',{name:'Chelsea FC'},initial+7*3600000);
  assert.equal((await state.db.prepare("SELECT status FROM automation_jobs WHERE id='news:chelsea'").first()).status,'waiting');
  assert.ok((await state.db.prepare('SELECT COUNT(*) AS n FROM automation_news').first()).n>0);
  state.db.close();
});

test('a failed report does not stop collection and its failure remains visible',async()=>{
  const state=environment();
  await state.db.prepare('INSERT INTO predictions VALUES (?1,?2,?3)').bind('broken-fixture','invalid-json','2026-10-27T10:00:00Z').run();
  const tick=await runAutomation(state.env,state.services,{reason:'50 4 * * *'});
  assert.equal(tick.status,'complete');assert.ok(tick.reportError);
  const status=await automationStatus(state.env,state.services);
  assert.equal(status.forecasts.total,1);assert.equal(status.news.teams,2);assert.ok(status.heartbeat.reportError);
  state.db.close();
});

test('finished games and later provider corrections append results without changing forecasts or frozen reports',async()=>{
  const state=environment();await runAutomation(state.env,state.services);
  const original=(await shadowHistory(state.env)).items[0];
  const final=score=>({...fixture,status:'FINISHED',score:{duration:'REGULAR',fullTime:score}});
  state.setMatches([earlier,final({home:2,away:0})]);state.setTime(Date.parse('2026-10-31T17:00:00Z'));
  await runAutomation(state.env,state.services);
  assert.equal((await shadowHistory(state.env)).items[0].latest.status,'settled');
  state.setTime(Date.parse('2026-11-01T04:50:00Z'));await runAutomation(state.env,state.services);
  const report=await dailyReport(state.env,'2026-11-01');
  assert.equal(report.experimentalMetrics.settled,1);assert.equal(report.lateBySeconds,0);
  const frozen=JSON.stringify(report);
  state.setMatches([earlier,final({home:0,away:2})]);state.setTime(Date.parse('2026-11-02T17:05:00Z'));
  await runAutomation(state.env,state.services);
  const corrected=(await shadowHistory(state.env)).items[0];
  assert.deepEqual(corrected.latest.score,{home:0,away:2});assert.deepEqual(corrected.probabilities,original.probabilities);
  assert.equal(JSON.stringify(await dailyReport(state.env,'2026-11-01')),frozen);
  state.setMatches([earlier,final({home:2,away:0})]);state.setTime(Date.parse('2026-11-03T17:10:00Z'));
  await runAutomation(state.env,state.services);
  const observations=await state.db.prepare('SELECT COUNT(*) AS n FROM automation_results').first();
  assert.equal(observations.n,3);assert.deepEqual((await shadowHistory(state.env)).items[0].latest.score,{home:2,away:0});
  state.db.close();
});
