import test from 'node:test';
import assert from 'node:assert/strict';
import {hundredSummary,verifiedHundredResults} from '../hundred-ui.js';
import {PACKET,PACKET_SHA256} from '../hundred-cohort-data.js';
import {createHash} from 'node:crypto';

function observation(){return {schemaVersion:1,protocol:'hundred-future-1',packetSha256:PACKET_SHA256,
 latestAttempt:{protocol:'hundred-future-1',packetSha256:PACKET_SHA256,status:'active',total:100,forecastsChanged:false,
  checkedAt:'2026-11-11T18:00:00Z',fixtures:PACKET.fixtures.map(f=>({fixtureId:f.fixtureId,status:'pending',score:null,outcome:null,reason:null}))}};}
test('the public data is the exact immutable hundred and empty results do not claim perfect or zero accuracy',()=>{
 assert.equal(createHash('sha256').update(JSON.stringify(PACKET)).digest('hex'),PACKET_SHA256);
 assert.equal(PACKET.fixtures.length,100);
 assert.deepEqual(hundredSummary(PACKET),{total:100,settled:0,correct:0,eligible:0,review:0,accuracy:null});
 const data=observation(),first=PACKET.fixtures[0],index=first.probabilities.indexOf(Math.max(...first.probabilities));
 data.latestAttempt.fixtures[0]={fixtureId:first.fixtureId,status:'settled',score:{home:1,away:0},outcome:0,reason:null};
 const {report}=verifiedHundredResults(data,PACKET,PACKET_SHA256);
 const summary=hundredSummary(PACKET,report.fixtures);
 assert.equal(summary.settled,1);assert.equal(summary.correct,index===0?1:0);
});
test('different cohorts, duplicate fixtures and contradictory final scores cannot enter the public summary',()=>{
 const wrong=observation();wrong.packetSha256='x';assert.throws(()=>verifiedHundredResults(wrong,PACKET,PACKET_SHA256));
 const duplicate=observation();duplicate.latestAttempt.fixtures[1]=duplicate.latestAttempt.fixtures[0];
 assert.throws(()=>verifiedHundredResults(duplicate,PACKET,PACKET_SHA256));
 const bad=observation();Object.assign(bad.latestAttempt.fixtures[0],{status:'settled',score:{home:1,away:1},outcome:0});
 assert.throws(()=>verifiedHundredResults(bad,PACKET,PACKET_SHA256));
 const failed=observation();failed.latestSuccessful=failed.latestAttempt;failed.latestAttempt={status:'failed'};
 assert.equal(verifiedHundredResults(failed,PACKET,PACKET_SHA256).failed,true);
});
