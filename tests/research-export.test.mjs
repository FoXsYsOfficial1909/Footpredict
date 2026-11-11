import test from 'node:test';
import assert from 'node:assert/strict';
import {researchTrainingPage} from '../worker/research-export.mjs';
import {memoryD1} from './helpers/d1.mjs';
import worker from '../worker/index.mjs';

test('archive pages retain exact bytes and IDs, make no writes and handle timestamp ties',async()=>{
 const db=memoryD1();
 try{
  db.sqlite.exec('CREATE TABLE research_snapshots(id TEXT,collected_at TEXT,payload TEXT)');
  const time='2026-11-07T12:00:00.000Z';
  for(let n=0;n<6;n++)db.sqlite.prepare('INSERT INTO research_snapshots VALUES(?,?,?)').run('id-'+n,time,JSON.stringify({n,text:'Цитата unchanged'}));
  const readOnly={prepare:sql=>{assert.match(sql.trim(),/^SELECT/);return db.prepare(sql);}};
  const one=await researchTrainingPage({FOOTPREDICT_DB:readOnly});
  assert.equal(one.snapshots.length,4);assert.ok(one.nextCursor);
  assert.deepEqual(await researchTrainingPage({FOOTPREDICT_DB:readOnly}),one);
  for(const row of one.snapshots){
   assert.equal(row.payload,db.sqlite.prepare('SELECT payload FROM research_snapshots WHERE id=?').get(row.id).payload);
   assert.match(row.sha256,/^[a-f0-9]{64}$/);
  }
  const two=await researchTrainingPage({FOOTPREDICT_DB:readOnly},one.nextCursor);
  assert.deepEqual(two.snapshots.map(r=>r.id),['id-4','id-5']);assert.equal(two.nextCursor,null);
  db.sqlite.prepare('INSERT INTO research_snapshots VALUES(?,?,?)').run('later','2026-11-06T12:00:00.000Z','{}');
  const next=await researchTrainingPage({FOOTPREDICT_DB:readOnly},two.cursor);
  assert.equal(next.snapshots[0].id,'later','a later insert with an earlier timestamp must not be lost');
 }finally{db.close();}
});

test('invalid pagination, methods and arbitrary parameters cannot invoke database operations',async()=>{
 for(const after of ['not+a+cursor','x'.repeat(513),btoa('{}'),btoa(JSON.stringify({t:'x',id:'x'}))])
  await assert.rejects(()=>researchTrainingPage({},after),e=>e.status===400);
 for(const [path,method] of [['/v1/research/training','POST'],['/v1/research/training?url=x','GET']]){
  const r=await worker.fetch(new Request('https://worker.test'+path,{method}),{});
  assert.equal(r.status,method==='POST'?405:400);
 }
});
