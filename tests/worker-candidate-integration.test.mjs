import test from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker/index.mjs';
import {memoryD1} from './helpers/d1.mjs';
import {nightlyFixture} from './helpers/nightly.mjs';
import {modelDigest,TRAINING_BASE} from '../worker/nightly-model.mjs';
import {spawnSync} from 'node:child_process';

test('the deployed Worker adapter registers real v1 math with verified nightly weights through its scheduled entrypoint',async()=>{
  const OriginalDate=Date,originalFetch=globalThis.fetch;
  let clock=Date.parse('2030-01-01T12:00:00Z');
  const bundle=await nightlyFixture(),db=memoryD1(),cache=new Map(),calls=[];
  const fixture={id:900001,utcDate:'2030-01-05T15:00:00Z',status:'TIMED',competition:{code:'PL'},season:{startDate:'2029-08-01'},
    homeTeam:{id:61,name:'Chelsea FC'},awayTeam:{id:66,name:'Manchester United FC'}};
  const past=Array.from({length:24},(_,i)=>({...fixture,id:100+i,status:'FINISHED',
    utcDate:new OriginalDate(clock-(i+5)*86400000).toISOString(),
    score:{duration:'REGULAR',fullTime:{home:[2,1,0][i%3],away:[0,1,1][i%3]}}}));
  db.sqlite.exec(`CREATE TABLE budgets(name TEXT PRIMARY KEY,n INTEGER);
    CREATE TABLE predictions(id TEXT PRIMARY KEY,fixture_id INTEGER,model_version TEXT,kickoff_at TEXT,snapshot TEXT,saved_at TEXT);
    CREATE TABLE observations(seq INTEGER PRIMARY KEY,prediction_id TEXT,state_key TEXT,payload TEXT);
    CREATE TABLE checks(prediction_id TEXT PRIMARY KEY,next_check INTEGER)`);
  db.sqlite.function('unixepoch',value=>Math.floor((value==='now'?clock:OriginalDate.parse(value))/1000));
  const env={FOOTPREDICT_DB:db,ALLOWED_ORIGIN:'https://foxsysofficial1909.github.io',
    FOOTBALL_DATA_KEY:'offline-test-only',API_FOOTBALL_KEY:'offline-test-only',
    FOOTPREDICT_CACHE:{get:async key=>{const item=cache.get(key);return item&&item.expires>clock?JSON.parse(item.value):null;},
      put:async(key,value,options)=>cache.set(key,{value,expires:clock+options.expirationTtl*1000})}};
  try{
    globalThis.Date=class extends OriginalDate{constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}};
    globalThis.fetch=async(input,options)=>{
      if(options?.redirect==='error')throw new TypeError('Invalid redirect value, must be one of "follow" or "manual" ("error" is unsupported by workerd)');
      const url=new URL(typeof input==='string'?input:input.url);calls.push(url.href);
      if(url.href===TRAINING_BASE+'latest.json')return Response.json({schemaVersion:1,latestSuccessful:bundle.report});
      if(url.href===bundle.modelUrl)return new Response(bundle.modelJson);
      if(url.hostname==='api.football-data.org'){
        assert.equal(options.headers['X-Auth-Token'],'offline-test-only');
        if(url.pathname==='/v4/matches/'+fixture.id)return Response.json(fixture);
        if(url.pathname==='/v4/competitions/PL/matches')return Response.json({matches:url.searchParams.get('season')==='2029'?[...past,fixture]:[]});
        throw new Error('Unexpected test provider path '+url.pathname);
      }
      if(url.hostname==='v3.football.api-sports.io')return new Response('{}',{status:403});
      if(url.pathname==='/robots.txt')return new Response('User-agent: *\nDisallow: /');
      if(['feeds.bbci.co.uk','feeds.bbc.co.uk'].includes(url.hostname))return new Response('<rss><channel></channel></rss>');
      throw new Error('Unexpected network request in offline integration: '+url.origin);
    };
    const run=async()=>{
      const pending=[];
      await worker.scheduled({cron:'7,37 * * * *',scheduledTime:clock},env,{waitUntil:promise=>pending.push(promise)});
      await Promise.all(pending);
    };
    await run();
    const row=db.sqlite.prepare('SELECT payload FROM nightly_predictions WHERE fixture_id=?').get(fixture.id);
    assert.ok(row,JSON.stringify(db.sqlite.prepare('SELECT id,result FROM automation_jobs').all()));
    const packet=JSON.parse(row.payload);
    assert.equal(packet.inputs.length,past.length);
    assert.equal(packet.models.v1.modelVersion,'poisson-1.0.0');
    assert.ok(packet.models.v1.probabilities.every(p=>Number.isFinite(p)&&p>0&&p<1));
    assert.equal(await modelDigest(packet.candidateArtifact.modelJson),packet.models.candidate.modelSha256);
    assert.equal(packet.models.candidate.modelVersion,bundle.report.modelVersion);
    assert.equal(packet.savedAt,new Date(clock).toISOString());
    // Independently reproduce all four distributions in Python, through the
    // public verifier. Normalized but incorrect probabilities must be rejected.
    const verify=value=>spawnSync('python3',['-m','scripts.verify_candidate','-'],{cwd:new URL('..',import.meta.url),input:JSON.stringify(value),encoding:'utf8'});
    const verified=verify(packet);assert.equal(verified.status,0,verified.stderr);
    assert.equal(JSON.parse(verified.stdout).allFourReproduced,true);
    const changed=structuredClone(packet);changed.models.candidate.probabilities[0]+=.01;changed.models.candidate.probabilities[1]-=.01;
    assert.notEqual(verify(changed).status,0);
    assert.equal(db.sqlite.prepare('SELECT COUNT(*) AS n FROM predictions').get().n,0);
    const snapshot=row.payload,loads=calls.filter(url=>url.startsWith(TRAINING_BASE)).length;
    clock+=3600000;await run();
    assert.equal(db.sqlite.prepare('SELECT payload FROM nightly_predictions WHERE fixture_id=?').get(fixture.id).payload,snapshot);
    assert.equal(calls.filter(url=>url.startsWith(TRAINING_BASE)).length,loads);
    const response=await worker.fetch(new Request('https://worker.test/v1/candidate-comparison',{headers:{Origin:env.ALLOWED_ORIGIN}}),env);
    assert.equal(response.status,200);const report=await response.json();assert.equal(report.totals.registered,1);
    assert.equal(report.models.candidate.n,0);assert.equal(report.models.candidate.brier,null);
    const before=db.sqlite.prepare('SELECT total_changes() AS n').get().n;
    const current=await worker.fetch(new Request('https://worker.test/v1/model-candidate',{headers:{Origin:env.ALLOWED_ORIGIN}}),env);
    assert.equal(current.status,200);const info=await current.json();assert.equal(info.verified,true);
    assert.equal(info.modelSha256,bundle.report.modelSha256);assert.equal(info.promotedToDefault,false);
    assert.equal(db.sqlite.prepare('SELECT total_changes() AS n').get().n,before);
  }finally{globalThis.Date=OriginalDate;globalThis.fetch=originalFetch;db.close();}
});
