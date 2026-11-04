import test from 'node:test';
import assert from 'node:assert/strict';
import {memoryD1} from './helpers/d1.mjs';
import {aiCacheSchema,evidenceCache,evidenceKey,warmEvidenceCache} from '../worker/ai-cache.mjs';
import {extractFacts,AI_MODEL} from '../worker/research.mjs';
import {buildEvidence} from '../worker/availability.mjs';

const time=Date.parse('2026-11-04T04:00:00Z');
const article=(changes={})=>({id:'club:0',team:'Example FC',source:'BBC Sport',url:'https://www.bbc.co.uk/sport/football/articles/one?at_medium=RSS',
  title:'Alex Smith injury update',summary:'Alex Smith will not miss Saturday through injury.',publishedAt:'2026-11-03T12:00:00Z',fetchedAt:'2026-11-04T03:00:00Z',...changes});
async function setup(){const db=memoryD1();await db.batch(aiCacheSchema().map(sql=>db.prepare(sql)));return {db,cache:evidenceCache(db),now:()=>time};}
const answer={response:{facts:[{evidenceId:'e0',subject:'Alex Smith'}]}};

test('a repeated article, changed RSS position and tracking URL reuse a grounded decision without another AI call',async()=>{
  const state=await setup();let calls=0,reserved=0;
  const binding={run:async()=>{calls++;return answer;}},reserve=async()=>{reserved++;};
  const first=await extractFacts(binding,[article()],reserve,state);
  assert.equal(first.cache.called,true);assert.equal(first.facts[0].extractionMethod,'ai_selection');
  const second=await extractFacts(binding,[article({id:'club:8',url:'https://www.bbc.co.uk/sport/football/articles/one?at_campaign=rss',fetchedAt:'2026-11-04T04:00:00Z'})],reserve,state);
  assert.equal(calls,1);assert.equal(reserved,1);assert.equal(second.cache.hits,1);assert.equal(second.cache.called,false);
  assert.equal(second.facts[0].sourceId,'club:8');assert.equal(second.facts[0].extractionMethod,'ai_selection_cached');
  assert.ok(second.facts[0].quote.includes('will not miss'));state.db.close();
});

test('a changed source text or policy/model namespace cannot reuse the old decision',async()=>{
  const state=await setup();let calls=0;
  const binding={run:async()=>{calls++;return answer;}};
  await extractFacts(binding,[article()],async()=>{},state);
  await extractFacts(binding,[article({summary:'Alex Smith will miss Saturday through injury.'})],async()=>{},state);
  assert.equal(calls,2);
  const item=buildEvidence([article()])[0];assert.notEqual(await evidenceKey(item,AI_MODEL),await evidenceKey(item,'another-model'));state.db.close();
});

test('valid empty selection is cached briefly, while malformed/refused output is not cached',async()=>{
  const state=await setup();let emptyCalls=0;
  const binding={run:async()=>{emptyCalls++;return {response:{facts:[]}};}};
  await extractFacts(binding,[article()],async()=>{},state);
  const cached=await extractFacts(binding,[article()],async()=>{throw new Error('Must not reserve');},state);
  assert.equal(cached.status,'complete');assert.equal(cached.facts.length,0);assert.equal(emptyCalls,1);
  const later={...state,now:()=>time+7*3600000};await extractFacts(binding,[article()],async()=>{},later);assert.equal(emptyCalls,2);
  const broken=article({url:'https://www.bbc.co.uk/sport/football/articles/broken'});let attempts=0;
  const bad={run:async()=>{attempts++;return {response:'not JSON'};}};
  await extractFacts(bad,[broken],async()=>{},state);await extractFacts(bad,[broken],async()=>{},state);
  assert.equal(attempts,2);state.db.close();
});

test('parallel requests share an exclusive lease and recover after a dead worker',async()=>{
  const state=await setup(),item=buildEvidence([article()])[0],key=await evidenceKey(item,AI_MODEL);
  const token=await state.cache.claim(key,time);assert.ok(token);assert.equal(await state.cache.claim(key,time),null);
  const busy=await extractFacts({run:async()=>{throw new Error('Must not call');}},[article()],async()=>{throw new Error('Must not reserve');},state);
  assert.equal(busy.status,'pending');assert.equal(busy.cache.called,false);
  const next=await state.cache.claim(key,time+120001);assert.ok(next);
  assert.equal(await state.cache.put(key,token,{bad:true},time+1000000,time+120002),false);
  await state.cache.release(key,token);assert.equal(await state.cache.claim(key,time+120003),null);state.db.close();
});

test('cached facts remain available with an exhausted quota; only unseen evidence waits',async()=>{
  const state=await setup();await extractFacts({run:async()=>answer},[article()],async()=>{},state);
  const result=await extractFacts({run:async()=>{throw new Error('Must not call');}},[article(),article({id:'club:new',url:'https://www.bbc.co.uk/sport/football/articles/new'})],async()=>{throw new Error('quota');},state);
  assert.equal(result.status,'partial');assert.equal(result.deferredReason,'budget_reached');assert.equal(result.facts.length,1);assert.equal(result.cache.called,false);state.db.close();
});

test('positive selections can be warmed from existing snapshots without modifying their archived payload',async()=>{
  const state=await setup(),a=article(),evidence=buildEvidence([a]);
  state.db.sqlite.exec('CREATE TABLE research_snapshots(id TEXT PRIMARY KEY,collected_at TEXT,payload TEXT)');
  const packet={version:'research-0.6.0',collectedAt:'2026-11-04T03:30:00Z',articles:[a],ai:{model:AI_MODEL,status:'complete',facts:[{
    sourceId:a.id,team:a.team,url:a.url,quote:evidence[0].quote,subject:'Alex Smith',extractionMethod:'ai_selection'}]}};
  const original=JSON.stringify(packet);state.db.sqlite.prepare('INSERT INTO research_snapshots VALUES(?,?,?)').run('original',packet.collectedAt,original);
  const warmed=await warmEvidenceCache(state.db,{model:AI_MODEL,now:time});assert.equal(warmed.imported,1);
  const result=await extractFacts(null,[a],async()=>{throw new Error('Must not reserve');},state);
  assert.equal(result.status,'complete');assert.equal(result.facts.length,1);assert.equal(result.cache.called,false);
  assert.equal(state.db.sqlite.prepare('SELECT payload FROM research_snapshots').get().payload,original);state.db.close();
});

test('a damaged cached subject is invalidated instead of turning into a persistent false fact',async()=>{
  const state=await setup();await extractFacts({run:async()=>answer},[article()],async()=>{},state);
  const row=state.db.sqlite.prepare('SELECT payload FROM ai_evidence_cache').get(),value=JSON.parse(row.payload);value.subject='Invented Player';
  state.db.sqlite.prepare('UPDATE ai_evidence_cache SET payload=?').run(JSON.stringify(value));
  let calls=0;const result=await extractFacts({run:async()=>{calls++;return answer;}},[article()],async()=>{},state);
  assert.equal(calls,1);assert.equal(result.facts[0].subject,'Alex Smith');state.db.close();
});
