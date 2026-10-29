import test from 'node:test';
import assert from 'node:assert/strict';
import {buildQuality,qualityReport} from '../worker/quality.mjs';
import {modelV2Info} from '../worker/model-v2.mjs';
import worker from '../worker/index.mjs';
import {memoryD1} from './helpers/d1.mjs';

const now=Date.parse('2026-11-03T12:00:00Z'),kickoff='2026-10-31T12:30:00Z';
function row(key,changes={}){
  return {fixture_id:560626,prediction_id:'fd:560626:poisson-1.0.0',model_version:key==='v1'?'poisson-1.0.0':modelV2Info().modelVersion,
    kickoff_at:kickoff,saved_at:'2026-10-28T12:00:00Z',provider:'football-data.org',trust:'server-recorded',market:'1X2_REGULATION',
    home_id:61,away_id:66,home_name:'Chelsea FC',away_name:'Manchester United FC',league:'PL',probabilities:'[0.4,0.3,0.3]',base_probabilities:'[4,3,3]',
    latest:JSON.stringify({status:'settled',result:{home:2,away:0},score:{home:2,away:0},kickoffAt:kickoff,checkedAt:'2026-10-31T17:00:00Z',
      metrics:{brier:999,logLoss:999},brier:999,logLoss:999,correct:0}),...changes};
}

test('scores are recomputed from frozen probabilities, with equal paired denominators and no mutation',()=>{
  const a=row('v1'),b=row('v2',{probabilities:'[0.8,0.1,0.1]'}),before=JSON.stringify([a,b]);
  const q=buildQuality([a],[b],now);
  assert.equal(q.cohorts.v1.metrics.n,1);assert.ok(Math.abs(q.cohorts.v1.metrics.brier-.54)<1e-12);
  assert.ok(Math.abs(q.cohorts.v2.metrics.brier-.06)<1e-12);assert.equal(q.paired.models.v1.n,1);
  assert.equal(q.paired.models.v2.n,1);assert.equal(q.paired.models.base.n,1);
  assert.equal(q.paired.models.v2.correct,1);assert.ok(Math.abs(q.paired.models.v1.logLoss+Math.log(.4))<1e-12);
  assert.equal(JSON.stringify([a,b]),before);
});

test('pending and empty cohorts have null metrics, not zero error or perfect accuracy',()=>{
  const q=buildQuality([row('v1',{latest:null})],[row('v2',{latest:'{"status":"pending"}'})],now);
  assert.equal(q.cohorts.v1.pending,1);assert.equal(q.cohorts.v2.pending,1);assert.equal(q.paired.pending,1);
  assert.equal(q.paired.models.v1.n,0);assert.equal(q.paired.models.v2.brier,null);assert.equal(q.cohorts.v1.metrics.accuracy,null);
  assert.equal(buildQuality([],[],now).fixtureScope.total,0);
});

test('ties contribute probability errors but do not count as correct or wrong top choices',()=>{
  const q=buildQuality([row('v1',{probabilities:'[0.4,0.4,0.2]'})],[row('v2')],now);
  const m=q.cohorts.v1.metrics;
  assert.equal(m.n,1);assert.equal(m.topEligible,0);assert.equal(m.ties,1);assert.equal(m.accuracy,null);
  assert.equal(q.fixtures[0].v1.metrics.correct,null);assert.equal(q.paired.models.v2.accuracy,1);
});

test('outcome corrections, reschedules and identity differences exclude paired comparisons',()=>{
  for(const [change,reason] of [
    [{away_id:1},'fixture_mismatch'],[{league:'BL1'},'fixture_mismatch'],
    [{kickoff_at:'2026-10-31T13:30:00Z'},'fixture_mismatch'],
    [{latest:JSON.stringify({status:'settled',score:{home:0,away:2},checkedAt:'2026-10-31T17:00:00Z'})},'score_disagreement'],
    [{latest:'{"status":"postponed"}'},'result_requires_review']]){
    const q=buildQuality([row('v1')],[row('v2',change)],now);
    assert.equal(q.paired.models.v1.n,0);assert.equal(q.paired.review,1);assert.equal(q.paired.reasons[reason],1);
  }
});

test('a short-horizon v1 is retained in its archive but excluded from the 24-hour comparison',()=>{
  const q=buildQuality([row('v1',{saved_at:'2026-10-31T09:00:00Z'})],[row('v2')],now);
  assert.equal(q.cohorts.v1.metrics.n,1);assert.equal(q.paired.excluded,1);assert.equal(q.paired.reasons.v1_less_than_24h,1);
  assert.equal(q.paired.models.v2.n,0);
});

test('invalid probability vectors and unverified results never enter metrics',()=>{
  for(const changes of [
    {probabilities:'[0.5,0.5,0.5]'},{probabilities:'[1.1,-0.1,0]'},{probabilities:'[0,0,0]'},
    {trust:'browser-unverified'},{market:'EXTRA_TIME'},{home_id:null},
    {saved_at:'2026-10-31T12:29:59Z'},
    {latest:JSON.stringify({status:'settled',result:{home:2,away:0},kickoffAt:kickoff,checkedAt:'2026-10-31T13:00:00Z'})},
    {latest:JSON.stringify({status:'settled',result:{home:2,away:0},kickoffAt:kickoff,checkedAt:'2026-12-01T13:00:00Z'})},
    {latest:JSON.stringify({status:'settled',result:{home:-1,away:0},kickoffAt:kickoff,checkedAt:'2026-10-31T17:00:00Z'})}]){
    const q=buildQuality([row('v1',changes)],[],now);
    assert.equal(q.cohorts.v1.metrics.n,0);assert.equal(q.cohorts.v1.excluded,1);
  }
});

test('missing baseline excludes the paired table without hiding valid individual predictions',()=>{
  const q=buildQuality([row('v1',{base_probabilities:null})],[row('v2')],now);
  assert.equal(q.cohorts.v1.metrics.n,1);assert.equal(q.cohorts.v2.metrics.n,1);
  assert.equal(q.paired.reasons.missing_base,1);assert.equal(q.paired.models.base.n,0);
});

test('list limit does not truncate aggregate metrics and other model versions stay outside the cohort',()=>{
  const rows=Array.from({length:205},(_,i)=>row('v1',{fixture_id:1000+i,prediction_id:`fd:${1000+i}:poisson-1.0.0`}));
  rows.push(row('v1',{model_version:'a-different-model'}));
  const q=buildQuality(rows,[],now);
  assert.equal(q.cohorts.v1.metrics.n,205);assert.equal(q.fixtureScope.total,205);assert.equal(q.fixtures.length,200);
  assert.equal(q.historical.common_fixtures,369);
});

function ledger(){
  const db=memoryD1();
  db.sqlite.exec(`CREATE TABLE predictions(id TEXT PRIMARY KEY,fixture_id INTEGER,model_version TEXT,kickoff_at TEXT,saved_at TEXT,snapshot TEXT);
    CREATE TABLE observations(seq INTEGER PRIMARY KEY,prediction_id TEXT,payload TEXT);
    CREATE TABLE automation_predictions(fixture_id INTEGER PRIMARY KEY,model_version TEXT,kickoff_at TEXT,saved_at TEXT,probabilities TEXT,metadata TEXT);
    CREATE TABLE automation_results(seq INTEGER PRIMARY KEY,fixture_id INTEGER,payload TEXT);`);
  const a=row('v1'),b=row('v2'),snapshot={provider:a.provider,trust:a.trust,market:a.market,league:a.league,homeId:61,awayId:66,homeName:a.home_name,awayName:a.away_name,
    probabilities:JSON.parse(a.probabilities),stats:{baseCounts:[4,3,3]},inputs:[{marker:'large-private-inputs-not-exported'}]};
  db.sqlite.prepare('INSERT INTO predictions VALUES (?,?,?,?,?,?)').run(a.prediction_id,a.fixture_id,a.model_version,kickoff,a.saved_at,JSON.stringify(snapshot));
  db.sqlite.prepare('INSERT INTO observations VALUES (?,?,?)').run(1,a.prediction_id,a.latest);
  db.sqlite.prepare('INSERT INTO automation_predictions VALUES (?,?,?,?,?,?)').run(b.fixture_id,b.model_version,kickoff,b.saved_at,b.probabilities,JSON.stringify({homeId:61,awayId:66,home:b.home_name,away:b.away_name}));
  db.sqlite.prepare('INSERT INTO automation_results VALUES (?,?,?)').run(1,b.fixture_id,b.latest);
  return db;
}

test('real SQLite projections use the latest correction, preserve all rows and expose no raw inputs',async()=>{
  const db=ledger();
  db.sqlite.prepare('INSERT INTO observations VALUES (?,?,?)').run(2,'fd:560626:poisson-1.0.0','{"status":"review_required"}');
  const before=db.sqlite.prepare('SELECT total_changes() AS n').get().n;
  const q=await qualityReport(db,now);
  assert.equal(q.cohorts.v1.review,1);assert.equal(q.cohorts.v1.metrics.n,0);assert.equal(q.cohorts.v2.metrics.n,1);
  assert.equal(q.paired.review,1);assert.equal(db.sqlite.prepare('SELECT total_changes() AS n').get().n,before);
  assert.ok(!JSON.stringify(q).includes('large-private-inputs'));db.close();
});

test('public quality route is read-only, validates queries and retains origin protection',async()=>{
  const db=ledger(),env={FOOTPREDICT_DB:db,ALLOWED_ORIGIN:'https://foxsysofficial1909.github.io'};
  const before=db.sqlite.prepare('SELECT total_changes() AS n').get().n;
  const request=(path,origin=env.ALLOWED_ORIGIN)=>new Request('https://worker.test'+path,{headers:{Origin:origin}});
  assert.equal((await worker.fetch(request('/v1/quality'),env)).status,200);
  assert.equal((await worker.fetch(request('/v1/quality?refresh=1'),env)).status,400);
  assert.equal((await worker.fetch(request('/v1/quality','https://example.org'),env)).status,403);
  assert.equal(db.sqlite.prepare('SELECT total_changes() AS n').get().n,before);db.close();
});
