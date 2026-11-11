import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker/index.mjs';
import {predictV2,modelV2Info} from '../worker/model-v2.mjs';

const site='https://foxsysofficial1909.github.io';
const env={ALLOWED_ORIGIN:site,ADMIN_TOKEN:'unit-test-only-not-a-production-secret'.repeat(2)};
const request=(path,method='GET',origin=site)=>new Request('https://worker.test'+path,{method,headers:origin?{Origin:origin}:{}});

test('production and exact local origins retain working CORS',async()=>{
  for(const origin of [site,'http://127.0.0.1:8000']){
    const response=await worker.fetch(request('/v1/model-v2','OPTIONS',origin),env);
    assert.equal(response.status,204);assert.equal(response.headers.get('Access-Control-Allow-Origin'),origin);
  }
});

test('foreign origins, suffix attacks and different local ports are rejected',async()=>{
  for(const origin of ['https://example.org',site+'.example.org','http://127.0.0.1:8001']){
    const response=await worker.fetch(request('/v1/model-v2','GET',origin),env);
    assert.equal(response.status,403);assert.equal(response.headers.get('Access-Control-Allow-Origin'),null);
  }
});

test('owner authentication still protects both prediction writes and research refresh',async()=>{
  for(const path of ['/v1/predictions','/v1/research/560626/refresh']){
    const response=await worker.fetch(request(path,'POST'),env);
    assert.equal(response.status,401);
  }
});

test('model information is honest about experimental status and unused news',async()=>{
  const response=await worker.fetch(request('/v1/model-v2'),env);
  assert.equal(response.status,200);
  const body=await response.json();assert.equal(body.promotedToDefault,false);assert.equal(body.newsUsedInProbabilities,false);
  assert.equal(body.benchmark.common_fixtures,369);
});

test('experimental inference is normalized, preserves unknown teams and rejects backwards prediction',()=>{
  const result=predictV2('Chelsea FC','Manchester United FC','2026-10-31T12:30:00Z');
  assert.equal(result.probabilities.length,3);
  assert.ok(Math.abs(result.probabilities.reduce((a,b)=>a+b,0)-1)<1e-12);
  assert.ok(result.probabilities.every(value=>value>=0&&value<=1));
  assert.deepEqual(predictV2('Unknown FC','Chelsea FC','2026-10-31T12:30:00Z').unseenTeams,['Unknown FC']);
  assert.throws(()=>predictV2('Chelsea FC','Manchester United FC','2026-10-01T12:00:00Z'));
});

test('health does not expose secret values',async()=>{
  const response=await worker.fetch(request('/v1/health'),env);const text=await response.text();
  assert.ok(!text.includes(env.ADMIN_TOKEN));assert.equal(JSON.parse(text).research.aiBinding,false);
});

test('the public Worker site serves only bundled assets and supports same-origin API reads',async()=>{
  const index=await worker.fetch(request('/app/'),env);
  assert.equal(index.status,200);assert.match(index.headers.get('Content-Type'),/^text\/html/);
  assert.match(await index.text(),/FootPredict 1\.10/);
  const script=await worker.fetch(request('/app/research-ui.js?v=1.9'),env);
  assert.equal(script.status,200);assert.match(await script.text(),/\/v1\/player-context\//);
  const head=await worker.fetch(request('/app/','HEAD'),env);
  assert.equal(head.status,200);assert.equal(await head.text(),'');
  const redirect=await worker.fetch(request('/app'),env);
  assert.equal(redirect.status,308);assert.equal(redirect.headers.get('Location'),'/app/');
  for(const path of ['/app/.env','/app/worker/index.mjs','/app/tests/worker.test.mjs'])
    assert.equal((await worker.fetch(request(path),env)).status,404);
  assert.equal((await worker.fetch(request('/app/','POST'),env)).status,405);
  const ownOrigin='https://worker.test';
  const api=await worker.fetch(request('/v1/model-v2','GET',ownOrigin),env);
  assert.equal(api.status,200);assert.equal(api.headers.get('Access-Control-Allow-Origin'),ownOrigin);
});
