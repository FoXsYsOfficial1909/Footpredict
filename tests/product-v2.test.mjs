import test from 'node:test';
import assert from 'node:assert/strict';
import {rounded,selectMatches,recentGames} from '../product-v2.js';
import {selectWindow} from '../analytics-v2.js';
import {MODEL,getRecord,assessConfidence} from '../v2-evidence.js';
import {completedBefore} from '../worker/index.mjs';
import {qualityWindows} from '../worker/quality-windows.mjs';

test('display rounds to exactly 100 without mutating probabilities',()=>{
  for(const p of [[1/3,1/3,1/3],[.52996,.28002,.19002],[1,0,0]]){
    const copy=[...p];
    assert.equal(rounded(p).reduce((s,x)=>s+Math.round(parseFloat(x)*10),0),1000);
    assert.deepEqual(copy,p);
  }
  assert.throws(()=>rounded([.8,.8,.1]));
});

const match = {id:1,utcDate:'2026-11-17T22:00:00Z',status:'TIMED',
  homeTeam:{id:1,name:'Arsenal'},awayTeam:{id:2,name:'Chelsea'}};
const now = Date.parse('2026-11-17T18:00:00Z');

test('filters respect Moscow midnight, favorites and distinct match states',()=>{
  const row={match,league:'Premier League',leagueCode:'PL',confidence:'insufficient',p:[.5,.3,.2]};
  const f={search:' arsenal ',period:'tomorrow',status:'all',sort:'time',confidence:'all',favorite:false};
  assert.equal(selectMatches([row],f,now).length,1);
  assert.equal(selectMatches([row],{...f,confidence:'high'},now).length,0);
  assert.equal(selectMatches([row],{...f,favorite:true},now,['team:2']).length,1);
  assert.equal(selectMatches([row],{...f,period:'today'},now).length,0);
  const changed={...row,match:{...match,status:'POSTPONED'}};
  assert.equal(selectMatches([changed],{...f,status:'upcoming'},now).length,0);
  assert.equal(selectMatches([changed],{...f,status:'changed'},now).length,1);
  const live={...row,match:{...match,status:'IN_PLAY'}};
  assert.equal(selectMatches([live],{...f,status:'live'},now).length,1);
  assert.equal(selectMatches([live],{...f,status:'upcoming'},now).length,0);
});

test('historical inputs deduplicate exact copies and stop on conflicting scores',()=>{
  const g={id:1,utcDate:'2026-11-01T12:00:00Z',__year:2026,status:'FINISHED',homeTeam:{id:1},awayTeam:{id:2},
    score:{duration:'REGULAR',fullTime:{home:2,away:1}}},target={id:2,utcDate:match.utcDate};
  assert.equal(completedBefore([g,structuredClone(g)],target,2026,now).length,1);
  const bad=structuredClone(g);bad.score.fullTime.home=3;
  assert.throws(()=>completedBefore([g,bad],target,2026,now));
  bad.score.fullTime.home=31;
  assert.equal(completedBefore([bad],target,2026,now).length,0);
  assert.equal(recentGames([g],1,Date.parse(g.utcDate)+3*3600000).length,0);
  assert.equal(recentGames([g,g],1,now).length,1);
});

test('quality windows compare identical fixtures and count the complete selected period',()=>{
  const row={league:'PL',kickoff:'2026-11-16T12:00:00Z',state:'settled',score:{home:2,away:1},
    models:{v1:[.6,.2,.2],base:[.4,.3,.3]}};
  const archive=Array.from({length:205},(_,i)=>({...row,fixtureId:i+1}));
  archive.push({...row,fixtureId:206,models:{...row.models,base:null}});
  archive.push({...row,kickoff:'2026-11-20T18:00:00Z',state:'pending'});
  const packet={windows:qualityWindows(archive,['v1','base'],now),fixtures:[]};
  const a=selectWindow(packet,'archive','PL',7);
  assert.equal(a.archived,206);
  assert.equal(a.paired,205);
  assert.equal(a.models.v1.n,a.models.base.n);
  assert.ok(Math.abs(a.models.v1.brier-.24)<1e-12);
  assert.equal(a.models.v1.accuracy,1);
  assert.equal(a.diagnostics[0].days,1);
  assert.equal(a.diagnostics[0].signal,false);
  assert.equal(selectWindow(packet,'archive','ELC',7),null);
});

const archiveRecord = {fixtureId:1,league:'PL',homeId:1,awayId:2,kickoff:match.utcDate,
  probabilities:[.6,.24,.16],savedAt:'2026-11-15T12:00:00Z',modelVersion:MODEL,state:'pending'};
test('saved forecasts match league, identity, status and kickoff, including candidate rollback history',()=>{
  const packet={fixtures:[{v1:archiveRecord}]};
  assert.deepEqual(getRecord(packet,match,'PL',now)?.probabilities,archiveRecord.probabilities);
  assert.equal(getRecord(packet,match,'BL1',now),null);
  assert.equal(getRecord(packet,{...match,status:'POSTPONED'},'PL',now),null);
  assert.equal(getRecord(packet,{...match,utcDate:'2026-11-18T22:00:00Z'},'PL',now),null);
  assert.equal(getRecord(packet,{...match,homeTeam:{id:3}},'PL',now),null);
  const nightly={...archiveRecord,primaryModel:'candidate',models:{
    v1:{modelVersion:MODEL,probabilities:archiveRecord.probabilities},
    candidate:{modelVersion:'ensemble-candidate-2026-11-14-aaaaaaaaaaaa',probabilities:[.44,.3,.26]}}};
  assert.deepEqual(getRecord({nightly:{fixtures:[nightly],release:{paused:true}}},match,'PL',now).probabilities,[.44,.3,.26]);
});

test('confidence depends only on comparable prior observed dates and team samples',()=>{
  const observations=Array.from({length:50},(_,i)=>{
    const kickoff=Date.UTC(2026,7,1+i,12);
    return {v1:{...archiveRecord,fixtureId:i+100,state:'settled',kickoff:new Date(kickoff).toISOString(),
      savedAt:new Date(kickoff-24*3600000).toISOString(),checkedAt:new Date(kickoff+5*3600000).toISOString(),
      score:{home:1,away:0}}};
  });
  const input={match,league:'PL',p:[.62,.24,.14],stats:{homeCount:10,awayCount:10},asOf:now};
  assert.equal(assessConfidence({},input).level,'insufficient');
  assert.equal(assessConfidence({fixtures:observations},{...input,league:'PD'}).level,'insufficient');
  assert.equal(assessConfidence({fixtures:observations},{...input,asOf:Date.UTC(2026,7,3)}).level,'insufficient');
  assert.equal(assessConfidence({fixtures:observations},{...input,stats:{homeCount:4,awayCount:10}}).level,'insufficient');
  assert.equal(assessConfidence({fixtures:observations},input).level,'low');
  for(const [i, f] of observations.entries()) if(i>=31) f.v1.score={home:1,away:1};
  assert.equal(assessConfidence({fixtures:observations},input).level,'medium');
});
