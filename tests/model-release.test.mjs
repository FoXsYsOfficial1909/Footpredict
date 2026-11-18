import test from 'node:test';
import assert from 'node:assert/strict';
import {MODEL_RELEASE,evaluateRelease,checkRelease,releaseStatus,publishedModel,eligibleRecipe} from '../worker/model-release.mjs';
import {comparisonSchema, NIGHTLY_COMPARISON_PROTOCOL} from '../worker/comparison.mjs';
import {memoryD1} from './helpers/d1.mjs';

const deadline=Date.parse(MODEL_RELEASE.decisionAt);
const recipe={codeSha256:MODEL_RELEASE.trainingCodeSha256,settingsSha256:MODEL_RELEASE.settingsSha256,
  modelSha256:'a'.repeat(64),unseenTeams:[],modelVersion:'ensemble-candidate-2026-12-01-aaaaaaaaaaaa'};
const date=t=>new Date(t).toISOString();
function cohort(){
  return Array.from({length:190},(_,i)=>{
    const saved=Date.UTC(2026,11,1)+Math.floor(i/10)*6*86400000;
    const kickoff=date(saved+48*3600000);
    const index=i%3,vector=p=>[0,1,2].map(j=>j===index?p:(1-p)/2);
    return {fixtureId:MODEL_RELEASE.firstFixture+i,kickoff,observedKickoff:kickoff,savedAt:date(saved),
      checkedAt:date(saved+54*3600000),state:'settled',season:2026,league:'PL',
      score:index===0?{home:1,away:0}:index===1?{home:0,away:0}:{home:0,away:1},
      models:{candidate:{...recipe,probabilities:vector(.71),trainingGeneratedAt:date(saved-3600000)},
        v1:{modelVersion:'poisson-1.0.0',probabilities:vector(.55)},
        base:{modelVersion:'league-frequencies-v1',probabilities:vector(.42)}}};
  });
}

test('only the fixed forward cohort decides promotion on the specified date',()=>{
  const rows=cohort();
  assert.equal(evaluateRelease(rows,deadline-1).status,'collecting');
  const decision=evaluateRelease(rows,deadline);
  assert.equal(decision.status,'approved');
  assert.equal(decision.rounds,19);
  assert.equal(decision.settled,190);
  assert.equal(decision.coverage,1);
  assert.ok(decision.upperLogLossDifference.v1<0);
  assert.deepEqual(evaluateRelease(rows.toReversed(),deadline),decision);
  assert.equal(evaluateRelease(rows.slice(0,170),deadline).reason,'insufficient_coverage');
});

test('bad performance, new recipes, changed kickoffs and late information are not accepted',()=>{
  const rows=cohort();
  for(const row of rows)row.models.candidate.probabilities=[.33,.34,.33];
  assert.equal(evaluateRelease(rows,deadline).reason,'advantage_not_established');
  const good=cohort();
  good[0].checkedAt=date(deadline+1);
  good[1].observedKickoff=date(Date.parse(good[1].kickoff)+3600000);
  good[2].models.candidate.codeSha256='different';
  good[3].savedAt='2026-11-18T13:00:00Z';
  good.push({...good[4]}); // reject the ambiguous fixture
  assert.equal(evaluateRelease(good,deadline).settled,185);
  assert.equal(eligibleRecipe({...recipe,unseenTeams:['Unknown']}),false);
});

test('scheduled decisions are written once, immutable after corrections and reversible for new forecasts',async()=>{
  const db=memoryD1();
  try{
    for(const sql of comparisonSchema(NIGHTLY_COMPARISON_PROTOCOL))db.sqlite.exec(sql);
    db.sqlite.exec('CREATE TABLE automation_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)');
    const insert=db.sqlite.prepare('INSERT INTO nightly_predictions VALUES (?,?,?,?,?)');
    const observe=db.sqlite.prepare('INSERT INTO nightly_results (fixture_id,checked_at,payload) VALUES (?,?,?)');
    for(const r of cohort()){
      insert.run(r.fixtureId,r.kickoff,r.savedAt,NIGHTLY_COMPARISON_PROTOCOL,JSON.stringify({year:r.season,league:r.league,models:r.models}));
      observe.run(r.fixtureId,r.checkedAt,JSON.stringify({status:r.state,checkedAt:r.checkedAt,kickoff:r.kickoff,score:r.score}));
    }
    assert.equal(await checkRelease(db,deadline-1),null);
    assert.equal(await publishedModel(db,recipe,false,deadline-1),'v1');
    assert.equal((await releaseStatus(db,[],deadline)).status,'awaiting_review');
    const decision=await checkRelease(db,deadline);
    assert.equal(decision.status,'approved');
    assert.equal(await publishedModel(db,recipe,false,deadline+1),'candidate');
    assert.equal(await publishedModel(db,recipe,true,deadline+1),'v1');
    assert.equal(await publishedModel(db,{...recipe,settingsSha256:'changed'},false,deadline+1),'v1');
    const first=cohort()[0];
    observe.run(first.fixtureId,date(deadline+1),JSON.stringify({status:'review_required',checkedAt:date(deadline+1)}));
    assert.deepEqual(await checkRelease(db,deadline+2),decision);
    assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM nightly_predictions').get().n,190);
  }finally{db.close();}
});
