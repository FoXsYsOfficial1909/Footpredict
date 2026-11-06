import test from 'node:test';
import assert from 'node:assert/strict';
import {getConfirmedLineups,LINEUP_LIMITS} from '../worker/confirmed-lineups.mjs';

const kickoff='2026-11-06T20:00:00Z',now=Date.parse(kickoff)-3600000;
const fixture={id:560634,competition:{code:'PL'},season:{startDate:'2026-08-01'},utcDate:kickoff,homeTeam:{name:'Everton FC'},awayTeam:{name:'Coventry City FC'}};
const competition=()=>({id:'401879242',date:kickoff,competitors:[{homeAway:'home',team:{id:'368',displayName:'Everton'}},{homeAway:'away',team:{id:'388',displayName:'Coventry City'}}]});
const scoreboard=()=>({leagues:[{id:'700',slug:'eng.1',season:{year:2026}}],events:[{id:'401879242',competitions:[competition()]}]});
const lineup=(side,name,teamId)=>({homeAway:side,team:{id:teamId,displayName:name},formation:'4-2-3-1',roster:Array.from({length:11},(_,i)=>({starter:true,athlete:{id:teamId+String(i).padStart(2,'0'),displayName:name+' player '+i},position:{displayName:i?'Defender':'Goalkeeper'},jersey:String(i+1)}))});
const summary=()=>({header:{id:'401879242',uid:'s:600~l:700~e:401879242',season:{year:2026},competitions:[competition()]},rosters:[lineup('home','Everton','368'),lineup('away','Coventry City','388')]});

function setup({time=now,board=scoreboard(),game=summary(),status=200}={}){
  const cache=new Map(),requests=[],budgets=[];
  const env={FOOTPREDICT_CACHE:{get:async key=>cache.get(key)||null}};
  const services={now:()=>time,reserve:async(name,limit)=>budgets.push({name,limit}),cachePut:async(key,value,ttl)=>{assert.ok([300,600].includes(ttl));cache.set(key,structuredClone(value));},
    readLimited:async(body,limit)=>{assert.equal(limit,1500000);return new Response(body).text();},
    fetch:async(url,options)=>{requests.push({url,options});return new Response(JSON.stringify(url.includes('/scoreboard')?board:game),{status});}};
  return {env,services,cache,requests,budgets};
}

test('ESPN XI requires exact event identity and labels provider report without official confirmation',async()=>{
  const ctx=setup(),result=await getConfirmedLineups(ctx.env,ctx.services,fixture);
  assert.equal(result.status,'reported_by_provider');assert.equal(result.officialConfirmation,false);
  assert.equal(result.reason,'both_starting_xis_received');assert.equal(result.fixtureMatch.providerFixtureId,'401879242');
  assert.ok(result.source.url.endsWith('/401879242'));assert.equal(result.fetchedAt,'2026-11-06T19:00:00.000Z');
  for(const team of result.teams){assert.equal(team.players.length,11);assert.equal(team.confirmation,'provider_starting_xi');assert.equal(team.officialConfirmation,false);assert.equal(team.formation,'4-2-3-1');}
  assert.equal(ctx.requests.length,2);assert.equal(ctx.budgets[0].limit,LINEUP_LIMITS.dailyRequests);
  for(const {url,options} of ctx.requests){assert.ok(url.startsWith('https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/'));assert.equal(options.redirect,'manual');assert.equal(options.credentials,'omit');}
});

test('no source request outside bounded lineup publication window or for another league',async()=>{
  for(const time of [Date.parse(kickoff)-91*60000,Date.parse(kickoff)+7*3600000]){
    const ctx=setup({time}),result=await getConfirmedLineups(ctx.env,ctx.services,fixture);
    assert.equal(result.reason,'outside_lineup_window');assert.equal(ctx.requests.length,0);assert.ok(result.teams.every(t=>t.players.length===0));
  }
  const ctx=setup(),result=await getConfirmedLineups(ctx.env,ctx.services,{...fixture,competition:{code:'BL1'}});
  assert.equal(result.reason,'invalid_fixture');assert.equal(ctx.requests.length,0);
});

test('date cache and event summary are reused across viewers without source requests',async()=>{
  const ctx=setup();await getConfirmedLineups(ctx.env,ctx.services,fixture);
  const second=await getConfirmedLineups(ctx.env,ctx.services,fixture);
  assert.equal(second.status,'reported_by_provider');assert.equal(second.cached,true);assert.equal(ctx.requests.length,2);
});

test('future-dated and expired cache data cannot masquerade as a fresh lineup',async()=>{
  for(const age of [-1000,300001]){
    const ctx=setup();await getConfirmedLineups(ctx.env,ctx.services,fixture);
    for(const saved of ctx.cache.values())saved.fetchedAt=new Date(now-age).toISOString();
    await getConfirmedLineups(ctx.env,ctx.services,fixture);assert.equal(ctx.requests.length,4);
  }
});

test('wrong season, competition, kickoff, side, club and ambiguous event mappings are rejected',async()=>{
  const changes=[b=>b.leagues[0].season.year=2025,b=>b.leagues[0].id='701',b=>b.leagues[0].slug='eng.2',
    b=>b.events[0].competitions[0].date='2026-11-06T20:02:00Z',b=>b.events[0].competitions[0].competitors.reverse().forEach((v,i)=>v.homeAway=i?'away':'home'),
    b=>b.events[0].competitions[0].competitors[0].team.displayName='Liverpool',b=>b.events.push({...b.events[0],id:'999',competitions:[{...competition(),id:'999'}]})];
  for(const mutate of changes){const board=scoreboard();mutate(board);const ctx=setup({board}),result=await getConfirmedLineups(ctx.env,ctx.services,fixture);assert.equal(result.status,'unavailable');assert.equal(ctx.requests.length,1);}
});

test('summary must repeat the same EPL event, season and clubs before players are used',async()=>{
  const changes=[g=>g.header.id='999',g=>g.header.uid='s:600~l:701~e:401879242',g=>g.header.season.year=2025,
    g=>g.header.competitions[0].competitors[1].team.displayName='Hull City',g=>g.header.competitions[0].date='2026-11-07T20:00Z'];
  for(const mutate of changes){const game=summary();mutate(game);const ctx=setup({game}),result=await getConfirmedLineups(ctx.env,ctx.services,fixture);assert.equal(result.reason,'summary_identity_mismatch');assert.ok(result.teams.every(t=>t.players.length===0));}
});

test('missing rosters, formation or explicit starter flags leave the XI unknown',async()=>{
  const changes=[r=>delete r.roster,r=>delete r.formation,r=>r.formation='4-4-1',r=>r.roster.forEach(p=>delete p.starter),r=>r.roster.pop(),r=>r.roster.push({...r.roster[0],athlete:{id:'99999',displayName:'Extra player'}})];
  for(const mutate of changes){const game=summary();game.rosters.forEach(mutate);const ctx=setup({game}),result=await getConfirmedLineups(ctx.env,ctx.services,fixture);assert.equal(result.status,'not_announced');assert.ok(result.teams.every(t=>t.players.length===0));}
});

test('predicted rosters, duplicate players and cross-club rosters cannot become published XIs',async()=>{
  const changes=[r=>r.predicted=true,r=>r.lineupType='predicted',r=>r.roster[1].athlete.id=r.roster[0].athlete.id,r=>r.roster[1].athlete.displayName=r.roster[0].athlete.displayName,r=>r.team.displayName='Liverpool',r=>r.team.id='999'];
  for(const mutate of changes){const game=summary();game.rosters.forEach(mutate);const ctx=setup({game}),result=await getConfirmedLineups(ctx.env,ctx.services,fixture);assert.equal(result.status,'not_announced');assert.ok(result.teams.every(t=>t.players.length===0));}
});

test('one received XI stays separate from the missing opposing XI',async()=>{
  const game=summary();delete game.rosters[1].roster;
  const ctx=setup({game}),result=await getConfirmedLineups(ctx.env,ctx.services,fixture);
  assert.equal(result.reason,'partial_starting_xi');assert.equal(result.teams[0].players.length,11);assert.equal(result.teams[1].status,'not_announced');assert.equal(result.teams[1].players.length,0);
});

test('HTTP errors and redirects fail as unknown without following arbitrary locations',async()=>{
  for(const status of [302,403,429,503]){const ctx=setup({status}),result=await getConfirmedLineups(ctx.env,ctx.services,fixture);assert.equal(result.status,'unavailable');assert.equal(result.reason,status===429?'source_rate_limited':'source_unavailable');assert.equal(ctx.requests.length,1);}
});

test('daily source quota prevents a new request and never fabricates a healthy squad',async()=>{
  const ctx=setup();ctx.services.reserve=async()=>{throw Object.assign(new Error('limit'),{status:429});};
  const result=await getConfirmedLineups(ctx.env,ctx.services,fixture);
  assert.equal(result.reason,'source_rate_limited');assert.equal(ctx.requests.length,0);assert.ok(result.teams.every(t=>t.players.length===0&&t.officialConfirmation===false));
});

test('source body limit and invalid JSON are rejected',async()=>{
  for(const response of [()=>new Response('{}',{headers:{'content-length':'1500001'}}),()=>new Response('<html>Not JSON</html>')]){
    const ctx=setup();ctx.services.fetch=async()=>response();const result=await getConfirmedLineups(ctx.env,ctx.services,fixture);assert.equal(result.status,'unavailable');
  }
});

test('source failures are cached briefly without turning them into empty confirmed lineups',async()=>{
  for(const status of [403,429,503]){
    const ctx=setup({status});
    const first=await getConfirmedLineups(ctx.env,ctx.services,fixture),second=await getConfirmedLineups(ctx.env,ctx.services,fixture);
    assert.equal(second.status,'unavailable');assert.equal(first.reason,second.reason);assert.equal(ctx.requests.length,1);assert.equal(ctx.budgets.length,1);
    assert.ok(second.teams.every(t=>t.players.length===0&&t.officialConfirmation===false));
  }
});

test('concurrent request collapse is restricted to the same environment',async()=>{
  const one=setup(),two=setup({status:403}),original=one.services.fetch;
  one.services.fetch=async(...args)=>{await new Promise(resolve=>setTimeout(resolve,5));return original(...args);};
  const [a,b,c]=await Promise.all([getConfirmedLineups(one.env,one.services,fixture),getConfirmedLineups(one.env,one.services,fixture),getConfirmedLineups(two.env,two.services,fixture)]);
  assert.equal(a.status,'reported_by_provider');assert.equal(b.status,'reported_by_provider');assert.equal(c.status,'unavailable');assert.equal(one.requests.length,2);assert.equal(two.requests.length,1);
});
