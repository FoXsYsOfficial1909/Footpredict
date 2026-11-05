import test from 'node:test';
import assert from 'node:assert/strict';
import {nightlyFixture,readLimited} from './helpers/nightly.mjs';
import {loadNightlyModel,verifyNightlyArtifact,modelDigest,TRAINING_BASE} from '../worker/nightly-model.mjs';
import {predictEnsemble} from '../worker/model-v2.mjs';

const now=Date.parse('2030-01-01T12:00:00Z');

test('only fixed public paths are fetched and the exact model bytes match the report hash',async()=>{
  const bundle=await nightlyFixture(),calls=[];
  const report={...bundle.report,modelUrl:'https://untrusted.test/replacement.json'};
  const services={now:()=>now,readLimited,fetch:async(url,options)=>{
    calls.push({url,options});return new Response(url.endsWith('latest.json')?JSON.stringify({schemaVersion:1,latestSuccessful:report}):bundle.modelJson);
  }};
  const loaded=await loadNightlyModel(services);
  assert.equal(loaded.report.modelSha256,await modelDigest(loaded.modelJson));
  assert.deepEqual(loaded.model,bundle.model);assert.equal(calls.length,2);
  assert.ok(calls.every(c=>c.url.startsWith(TRAINING_BASE)&&c.options.credentials==='omit'&&c.options.redirect==='error'));
  assert.ok(calls.every(c=>Object.keys(c.options.headers).join(',')==='Accept'));
  assert.equal(calls[1].url,bundle.modelUrl);
  const prediction=predictEnsemble(loaded.model,'Chelsea FC','Manchester United FC','2030-01-05T15:00:00Z');
  assert.equal(prediction.modelVersion,bundle.report.modelVersion);assert.ok(Math.abs(prediction.probabilities.reduce((a,b)=>a+b,0)-1)<1e-12);
});

test('the last successful training is usable after a failed attempt only while still fresh',async()=>{
  const bundle=await nightlyFixture();let clock=now;
  const services={now:()=>clock,readLimited,fetch:async url=>new Response(url.endsWith('latest.json')?
    JSON.stringify({schemaVersion:1,latestAttempt:{status:'failed'},latestSuccessful:bundle.report}):bundle.modelJson)};
  assert.equal((await loadNightlyModel(services)).report.modelVersion,bundle.report.modelVersion);
  clock=Date.parse(bundle.report.generatedAt)+36*3600000+1;
  await assert.rejects(()=>loadNightlyModel(services),/старше 36 часов/);
});

test('future training, leakage, changed settings and a mismatched file fail closed',async()=>{
  const bundle=await nightlyFixture();
  for(const change of [
    {generatedAt:'2030-01-01T12:00:01Z'}, {latestTrainingMatch:'2029-12-31'},
    {trainingCutoffExclusive:'2030-01-01'}, {settingsSha256:'0'.repeat(64)},
    {sourceCommit:'invalid'}, {modelVersion:'different-candidate'}, {promotedToDefault:true}
  ])await assert.rejects(()=>verifyNightlyArtifact({...bundle.report,...change},bundle.modelJson,now),{status:503});
  await assert.rejects(()=>verifyNightlyArtifact(bundle.report,bundle.modelJson+' ',now),/Контрольная сумма/);
  const model=structuredClone(bundle.model);model.elo.latest_training_date='2030-01-01';
  const json=JSON.stringify(model),checksum=await modelDigest(json);
  await assert.rejects(()=>verifyNightlyArtifact({...bundle.report,modelSha256:checksum},json,now),/Параметры/);
});

test('bad HTTP, oversized data, broken JSON and invalid parameters never yield a candidate',async()=>{
  const bundle=await nightlyFixture();
  for(const reply of [new Response('unavailable',{status:503}),new Response('x'.repeat(100001)),new Response('not JSON')]){
    await assert.rejects(()=>loadNightlyModel({now:()=>now,readLimited,fetch:async()=>reply}),{status:503});
  }
  const model=structuredClone(bundle.model);model.glm.intercept=999;
  const json=JSON.stringify(model),checksum=await modelDigest(json);
  await assert.rejects(()=>verifyNightlyArtifact({...bundle.report,modelSha256:checksum},json,now),/Параметры/);
});
