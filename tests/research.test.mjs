import test from 'node:test';
import assert from 'node:assert/strict';
import {parseFeed, teamStatistics, calendarContext, validateAiFacts, normalizeModelAnswer, extractFacts, collectResearch, getResearch, storedResearch, RESEARCH_VERSION} from '../worker/research.mjs';

const now = Date.parse('2026-10-27T19:00:00Z');
const fixture = {id:560626,utcDate:'2026-10-31T12:30:00Z',status:'TIMED',competition:{code:'PL'},season:{startDate:'2026-08-21'},
  homeTeam:{id:61,name:'Chelsea FC'},awayTeam:{id:66,name:'Manchester United FC'}};
const game = (id, date, home=61, away=66, hg=2, ag=1) => ({id,utcDate:date,status:'FINISHED',
  homeTeam:{id:home,name:home===61?'Chelsea FC':'Manchester United FC'},awayTeam:{id:away,name:away===66?'Manchester United FC':'Chelsea FC'},
  score:{duration:'REGULAR',fullTime:{home:hg,away:ag}}});
const rss = `<rss><channel><item><title>Palmer injury update</title><link>https://www.bbc.com/sport/football/articles/palmer</link><pubDate>Tue, 27 Oct 2026 12:00:00 GMT</pubDate><description>Cole Palmer is a doubt for the match after missing training.</description></item></channel></rss>`;
const article = {id:'chelsea:0',team:'Chelsea FC',url:'https://www.bbc.com/sport/football/articles/palmer',
  title:'Palmer injury update',summary:'Cole Palmer is a doubt for the match after missing training.',publishedAt:'2026-10-27T12:00:00Z',fetchedAt:'2026-10-27T19:00:00Z'};

test('RSS preserves source/date and strips markup without accepting foreign links',()=>{
  const news=parseFeed(rss,{teamName:'Chelsea FC',slug:'chelsea',now});
  assert.equal(news.length,1);assert.equal(news[0].source,'BBC Sport');
  assert.equal(news[0].verification,'news_report');
  assert.equal(parseFeed(rss.replace('https://www.bbc.com/','https://evil.example/'),{teamName:'Chelsea FC',slug:'chelsea',now}).length,0);
});

test('future and stale news are not available at the collection cutoff',()=>{
  assert.equal(parseFeed(rss.replace('27 Oct 2026','28 Oct 2026'),{teamName:'Chelsea FC',slug:'chelsea',now}).length,0);
  assert.equal(parseFeed(rss.replace('27 Oct 2026','01 Oct 2026'),{teamName:'Chelsea FC',slug:'chelsea',now}).length,0);
});

test('form excludes unfinished, future and recently kicked-off matches',()=>{
  const games=[game(1,'2026-10-25T14:00:00Z'),game(2,'2026-10-27T18:00:00Z'),game(3,'2026-10-28T12:00:00Z'),
    {...game(4,'2026-10-24T12:00:00Z'),status:'POSTPONED'}];
  const team=teamStatistics(fixture.homeTeam,games,{cutoff:now,kickoff:Date.parse(fixture.utcDate)});
  assert.equal(team.form.length,1);assert.equal(team.points,3);
  assert.equal(team.injuries.absenceConfirmed,false);assert.equal(team.calendar.gapDays,null);
});

test('calendar gap remains partial and identifies an intervening scheduled match',()=>{
  const calendar=calendarContext(61,[{...game(1,'2026-10-28T19:00:00Z'),status:'TIMED',competition:{name:'Cup'}}],{cutoff:now,kickoff:Date.parse(fixture.utcDate)});
  assert.equal(calendar.status,'partial');assert.equal(calendar.basedOnScheduledMatch,true);
  assert.ok(calendar.gapDays>2 && calendar.gapDays<3);
});

test('AI facts require a real source, verbatim evidence and supported subject',()=>{
  const good={sourceId:'chelsea:0',category:'injury',subject:'Cole Palmer',quote:'Cole Palmer is a doubt for the match after missing training.'};
  const result=validateAiFacts({facts:[good,{...good,sourceId:'fake'},{...good,subject:'Invented player'},{...good,quote:'He will definitely miss the next five matches.'}]},[article]);
  assert.equal(result.facts.length,1);assert.equal(result.rejected,3);
  assert.equal(result.facts[0].url,article.url);assert.equal(result.facts[0].verification,'reported_in_source');
});

test('missing AI and exhausted budget are explicit and do not generate facts',async()=>{
  assert.equal((await extractFacts(null,[article],()=>{})).status,'not_connected');
  let calls=0;
  const result=await extractFacts({run:async()=>{calls++;}},[article],()=>{throw new Error('budget');});
  assert.equal(result.status,'budget_reached');assert.equal(calls,0);
});

test('malformed AI output fails closed; valid quoted evidence is retained',async()=>{
  const bad=await extractFacts({run:async()=>({response:'A confident unsupported prediction'})},[article],async()=>{});
  assert.equal(bad.status,'unavailable');assert.deepEqual(bad.facts,[]);
  const valid=await extractFacts({run:async()=>({response:JSON.stringify({facts:[{sourceId:article.id,category:'injury',subject:'Cole Palmer',quote:article.summary}]})})},[article],async()=>{});
  assert.equal(valid.status,'complete');assert.equal(valid.facts.length,1);
});

test('structured and textual model responses normalize without accepting unsupported shapes',()=>{
  const object={facts:[]};
  for(const answer of [object,{response:object},{response:JSON.stringify(object)},{result:{response:object}},JSON.stringify(object),'Result:\n```json\n'+JSON.stringify(object)+'\n```'])assert.deepEqual(normalizeModelAnswer(answer),object);
  assert.deepEqual(normalizeModelAnswer({response:null,choices:[{finish_reason:'stop',message:{content:'{"facts":[]}'}}]}),object);
  assert.deepEqual(normalizeModelAnswer({choices:[{message:{content:[{type:'text',text:'{"facts":[]}'}]}}]}),object);
  assert.throws(()=>normalizeModelAnswer({choices:[{finish_reason:'length',message:{content:'{"facts":[]}'}}]}));
  assert.throws(()=>normalizeModelAnswer({request_id:'async-not-yet-complete'}));
});

function services({injuryError=true, date=now}={}) {
  const calls=[];
  return {calls,now:()=>date,reserve:async(name,limit)=>calls.push(['budget',name,limit]),cachePut:async()=>{},
    readLimited:async(body,limit)=>{const text=await new Response(body).text();if(text.length>limit)throw new Error('limit');return text;},
    fetch:async()=>new Response(rss),
    upstream:async(provider,path)=>{
      calls.push([provider,path]);
      if(path.startsWith('/matches/'))return {data:fixture,fetchedAt:new Date(now-1000).toISOString()};
      if(path.startsWith('/competitions/'))return {data:{matches:[game(1,'2026-10-25T14:00:00Z')]},fetchedAt:new Date(now-1000).toISOString()};
      if(path.startsWith('/teams/'))return {data:{matches:[game(1,'2026-10-25T14:00:00Z')]},fetchedAt:new Date(now-1000).toISOString()};
      if(provider==='af' && injuryError)throw Object.assign(new Error('quota'),{status:429});
      return {data:{response:[]},fetchedAt:new Date(now).toISOString()};
    }};
}

test('real collector shape marks quota and unavailable lineup instead of healthy players',async()=>{
  const deps=services();const packet=await collectResearch(560626,{FOOTPREDICT_CACHE:{get:async()=>null}},deps);
  assert.equal(packet.status,'partial');assert.equal(packet.teams.length,2);
  assert.equal(packet.teams[0].injuries.status,'unavailable');assert.equal(packet.coverage.lineups,false);
  assert.ok(packet.sourceErrors.some(item=>item.status===429));assert.equal(packet.ai.status,'not_connected');
  assert.ok(deps.calls.some(call=>call[0]==='af'));assert.ok(!deps.calls.some(call=>call[0]==='old'));
});

test('invalid league and started fixtures are rejected before secondary sources',async()=>{
  const deps=services();deps.upstream=async()=>({data:{...fixture,competition:{code:'CL'}}});
  await assert.rejects(()=>collectResearch(560626,{},deps),error=>error.status===422);
  await assert.rejects(()=>collectResearch(560626,{},services({date:Date.parse(fixture.utcDate)+1})),error=>error.status===422);
});

test('fresh stored card is returned without provider calls',async()=>{
  const saved={version:RESEARCH_VERSION,collectedAt:new Date(now-1000).toISOString(),fixture:{id:560626},candidate:{modelVersion:'frozen-before-update',probabilities:[.4,.3,.3]}};
  const database={prepare:sql=>({run:async()=>({success:true}),bind:()=>({first:async()=>({payload:JSON.stringify(saved)})})})};
  const deps=services();const packet=await getResearch(560626,{FOOTPREDICT_DB:database},deps);
  assert.equal(packet.cached,true);assert.deepEqual(deps.calls,[]);
  assert.equal(packet.candidate.modelVersion,'frozen-before-update');
});

test('candidate probabilities are recorded in the collected snapshot before kickoff',async()=>{
  const deps=services();deps.predictCandidate=()=>({status:'experimental',modelVersion:'test-v2',probabilities:[.4,.3,.3]});
  const result=await collectResearch(560626,{FOOTPREDICT_CACHE:{get:async()=>null}},deps);
  assert.equal(result.candidate.modelVersion,'test-v2');assert.equal(result.candidate.generatedAt,result.collectedAt);
});

test('a fresh card with exhausted AI quota can refresh after the UTC daily reset',async()=>{
  const before='2026-10-27T23:50:00Z',after=Date.parse('2026-10-28T00:10:00Z');
  const saved={version:RESEARCH_VERSION,collectedAt:before,fixture:{id:560626},ai:{status:'budget_reached'}};
  let inserts=0;
  const database={prepare:sql=>({run:async()=>({success:true}),bind:()=>({
    first:async()=>({payload:JSON.stringify(saved)}),run:async()=>{inserts++;return {success:true};}
  })})};
  const deps=services({date:after});
  const packet=await getResearch(560626,{FOOTPREDICT_DB:database},deps);
  assert.equal(packet.cached,false);assert.equal(inserts,1);
  assert.ok(deps.calls.some(call=>call[0]==='fd'));
});

test('stored-only reads do not collect new evidence or require provider credentials',async()=>{
  const saved={version:'earlier-research-version',collectedAt:'2026-10-01T10:00:00Z',candidate:{probabilities:[.4,.3,.3]}};
  const database={prepare:sql=>({run:async()=>({success:true}),bind:()=>({first:async()=>({payload:JSON.stringify(saved)})})})};
  const result=await storedResearch(560626,{FOOTPREDICT_DB:database});
  assert.equal(result.storedOnly,true);assert.equal(result.collectedAt,saved.collectedAt);
  const empty={prepare:sql=>({run:async()=>({success:true}),bind:()=>({first:async()=>null})})};
  await assert.rejects(()=>storedResearch(560626,{FOOTPREDICT_DB:empty}),error=>error.status===404);
});
