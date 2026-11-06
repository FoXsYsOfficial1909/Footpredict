import test from 'node:test';
import assert from 'node:assert/strict';
import {getPlayerContext} from '../worker/player-context.mjs';
import worker from '../worker/index.mjs';

const now=Date.parse('2026-11-06T20:00:00Z');
const home={id:61,name:'Chelsea FC'},away={id:57,name:'Arsenal FC'};
const fixture={id:560700,utcDate:'2026-11-07T15:00:00Z',competition:{code:'PL'},status:'TIMED',homeTeam:home,awayTeam:away};
const bootstrap={teams:[{id:1,name:'Chelsea'},{id:2,name:'Arsenal'}],events:[{id:11}],elements:[
  {id:1,team:1,first_name:'Example',second_name:'Player',element_type:3,status:'d',news:'Knock - being assessed',news_added:'2026-11-05T12:00:00Z'},
  {id:2,team:2,first_name:'Other',second_name:'Player',element_type:4,status:'a',news:''}
]};
const fplFixtures=[{id:100,event:11,team_h:1,team_a:2,kickoff_time:fixture.utcDate,started:false}];
function deps(games=[fixture]) {
  const requests=[],provider=[],cache=new Map();
  return {env:{FOOTPREDICT_CACHE:{get:async key=>cache.get(key)||null}},services:{
    now:()=>now,fetch:async url=>{
      requests.push(url);
      if(url.endsWith('/bootstrap-static/'))return Response.json(bootstrap);
      if(url.endsWith('/fixtures/'))return Response.json(fplFixtures);
      throw new Error('Unexpected provider '+url);
    },readLimited:async body=>new Response(body).text(),
    cachePut:async(key,value)=>cache.set(key,value),
    reserve:async()=>{throw new Error('No AI, card or football provider quota should be used by cached context');},
    upstream:async(...args)=>{provider.push(args);return {data:{matches:games},fetchedAt:new Date(now-1000).toISOString()};}
  },requests,provider};
}

test('public context automatically combines form and free dated reports without news or AI calls',async()=>{
  const past={...fixture,id:1,status:'FINISHED',utcDate:'2026-10-31T15:00:00Z',score:{duration:'REGULAR',fullTime:{home:2,away:1}}};
  const state=deps([fixture,past]);
  const first=await getPlayerContext(fixture.id,state.env,state.services);
  assert.equal(first.fixture.id,fixture.id);assert.equal(first.usedInProbabilities,false);
  assert.equal(first.teams[0].form[0].outcome,'W');assert.equal(first.teams[1].form[0].outcome,'L');
  assert.equal(first.availability.status,'available');assert.equal(first.availability.teams[0].reports[0].status,'doubtful');
  assert.equal(first.availability.teams[0].absenceConfirmed,false);
  assert.equal(first.availability.teams[1].status,'no_supported_reports');
  assert.equal(first.availability.teams[1].medicalStatus,'unknown');
  assert.equal(first.lineups.status,'not_announced');assert.equal(state.requests.length,2);
  await getPlayerContext(fixture.id,state.env,state.services);
  assert.equal(state.requests.length,2,'all visitors/fixtures share the league availability cache');
  assert.ok(state.provider.every(args=>args[0]==='fd'&&args[1]==='/competitions/PL/matches?season=2026'));
});

test('context IDs and current-league membership are checked before public sources',async()=>{
  const state=deps();
  for(const id of [0,-1,1.5,NaN,'560700',1e13])
    await assert.rejects(()=>getPlayerContext(id,state.env,state.services),e=>e.status===400);
  assert.equal(state.provider.length,0);
  await assert.rejects(()=>getPlayerContext(77,state.env,state.services),e=>e.status===404);
  assert.equal(state.requests.length,0);
  for(const bad of [{...fixture,competition:{code:'CL'}},{...fixture,utcDate:'2027-01-01T15:00:00Z'},
    {...fixture,awayTeam:home},{...fixture,utcDate:'not-a-date'}]){
    const isolated=deps([bad]);
    await assert.rejects(()=>getPlayerContext(fixture.id,isolated.env,isolated.services),e=>e.status===422);
    assert.equal(isolated.requests.length,0);
  }
});

test('a public-source failure preserves usable form and reports unknown availability',async()=>{
  const state=deps();state.services.fetch=async()=>new Response('Unavailable',{status:503});
  const result=await getPlayerContext(fixture.id,state.env,state.services);
  assert.equal(result.fixture.id,fixture.id);assert.equal(result.teams.length,2);
  assert.equal(result.availability.status,'unavailable');
  assert.ok(result.availability.teams.every(team=>team.medicalStatus==='unknown'&&team.absenceConfirmed===false));
});

test('context route enforces method and query policy before contacting providers',async()=>{
  const env={ALLOWED_ORIGIN:'https://foxsysofficial1909.github.io'};
  for(const [path,method,status] of [[`/v1/player-context/${fixture.id}`,'POST',405],
    [`/v1/player-context/${fixture.id}?url=https://example.org`,'GET',400],['/v1/player-context/0','GET',400]]){
    const response=await worker.fetch(new Request('https://worker.test'+path,{method}),env);
    assert.equal(response.status,status);
  }
});
