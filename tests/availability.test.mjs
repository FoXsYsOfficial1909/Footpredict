import test from 'node:test';
import assert from 'node:assert/strict';
import {buildEvidence,sourceExcerpts,validateEvidenceChoices,currentAvailabilityView} from '../worker/availability.mjs';
import {extractFacts,storedResearch,getResearch} from '../worker/research.mjs';
import {memoryD1} from './helpers/d1.mjs';

const article=(overrides={})=>({id:'club:1',team:'Example FC',source:'BBC Sport',url:'https://www.bbc.com/sport/football/articles/a',
  title:'Alex Smith injury update',summary:'Alex Smith is a doubt for Saturday after missing training.',publishedAt:'2026-10-28T12:00:00Z',fetchedAt:'2026-10-29T08:00:00Z',...overrides});

test('evidence preserves negation and does not assert medical fitness or a future lineup',()=>{
  const evidence=buildEvidence([article({title:'Which match will Smith miss?',summary:"BBC Sport's Ask Me Anything team explains why Alex Smith's suspension will not rule him out of Saturday's Premier League match against Example United."})]);
  assert.equal(evidence.length,1);assert.ok(evidence[0].quote.includes('will not rule him out'));
  const fact=sourceExcerpts(evidence)[0];assert.equal(fact.fixtureApplicability,'not_confirmed');assert.equal(fact.extractionMethod,'source_excerpt');
  assert.ok(fact.quote.split(/\s+/).length<=25);
});

test('availability later in a busy feed remains eligible and other squads are excluded',()=>{
  const generic=Array.from({length:9},(_,i)=>article({id:'generic:'+i,title:'Match reaction '+i,summary:'Fans discuss the result.'}));
  const evidence=buildEvidence([...generic,article(),article({id:'women',url:'https://example.org/news/women-team-news',title:'Women injury update'}),article({id:'academy',title:'Under-21 injury update'})]);
  assert.equal(evidence.length,1);assert.equal(evidence[0].sourceId,'club:1');
});

test('AI chooses an evidence id; quote, classification and URL are reconstructed by the server',()=>{
  const evidence=buildEvidence([article()]);
  const result=validateEvidenceChoices({facts:[{evidenceId:'e0',subject:'Alex Smith',quote:'Invented claim',url:'https://evil.example/',category:'confirmed_fit'},
    {evidenceId:'e9',subject:null},{evidenceId:'e0',subject:'Invented player'}]},evidence);
  assert.equal(result.facts.length,1);assert.equal(result.facts[0].quote,evidence[0].quote);assert.equal(result.facts[0].url,article().url);
  assert.equal(result.facts[0].category,'availability');assert.ok(result.facts[0].quote.includes('is a doubt'));assert.equal(result.rejected,2);
  const unsupported=validateEvidenceChoices({facts:[{evidenceId:'e0',subject:'Invented player'}]},evidence);
  assert.equal(unsupported.rejectionReasons.subject_not_in_selected_quote,1);
});

test('non-availability news consumes no AI quota and grounded excerpts remain available without AI',async()=>{
  let reserved=0,called=0;
  const result=await extractFacts({run:async()=>{called++;}},[article({title:'Club opens a new shop',summary:'The new store opens tomorrow.'})],async()=>{reserved++;});
  assert.equal(result.status,'no_relevant_evidence');assert.equal(reserved,0);assert.equal(called,0);
  assert.equal(sourceExcerpts(buildEvidence([article()])).length,1);
});

test('a real-shaped choices response selects a source excerpt without asking the model to copy it',async()=>{
  const result=await extractFacts({run:async(model,input)=>{
    assert.ok(input.messages[1].content.includes('evidenceId'));assert.ok(!input.messages[1].content.includes('12-200 characters'));
    return {choices:[{message:{content:'{"facts":[{"evidenceId":"e0","subject":"Alex Smith"}]}'},finish_reason:'stop'}]};
  }},[article()],async()=>{});
  assert.equal(result.status,'complete');assert.equal(result.facts.length,1);assert.equal(result.facts[0].extractionMethod,'ai_selection');
});

test('all FPL titles, fantasy sections and URLs are excluded even when their summaries sound medical',async()=>{
  const variants=[{title:'Replacing injured Joao Pedro in FPL'},{title:'FPL injury news'},
    {title:'Fantasy Premier League team news'},{section:'Fantasy Football'},
    {url:'https://www.bbc.com/sport/fantasyfootball/injury-update'},
    {title:'Injury update',summary:'FPL managers expect Alex Smith to miss the match.'},
    {url:'https://www.bbc.com/sport/football/fpl-injury-news'},
    {url:'https://www.bbc.com/sport/football/%66%70%6c-injury-news'}];
  let calls=0,reserved=0;
  for(const variant of variants){
    const articles=[article(variant)];assert.deepEqual(buildEvidence(articles),[]);
    const result=await extractFacts({run:async()=>{calls++;}},articles,async()=>{reserved++;});
    assert.equal(result.status,'no_relevant_evidence');
  }
  assert.equal(calls,0);assert.equal(reserved,0);
  assert.equal(buildEvidence([article({title:'Fplovia FC injury update'})]).length,1);
});

test('filtering existing display reports preserves negation, original timestamps and archived bytes',async()=>{
  const fantasy=article({id:'fpl',title:'Replacing injured Joao Pedro in FPL',summary:'Joao Pedro is injured.'});
  const good=article({id:'real',title:'Alex Smith injury news',summary:'Alex Smith will not miss Saturday through injury.'});
  const badFact={sourceId:'fpl',team:fantasy.team,url:fantasy.url,quote:fantasy.title,category:'injury'};
  const goodFact={sourceId:'real',team:good.team,url:good.url,quote:good.summary,category:'injury'};
  const packet={version:'research-0.5.0',collectedAt:'2026-10-30T00:17:37.734Z',articles:[fantasy,good],availabilityReports:[badFact,goodFact],
    ai:{status:'complete',facts:[badFact,goodFact],rejected:0},teams:[{name:good.team,availability:{status:'reports_found',reports:[badFact,goodFact]}}],
    candidate:{probabilities:[.4,.3,.3]},coverage:{ai:true,availabilityReports:true,injuries:false}};
  const original=JSON.stringify(packet),view=currentAvailabilityView(packet);
  assert.equal(JSON.stringify(packet),original);assert.deepEqual(view.ai.facts,[goodFact]);assert.equal(view.ai.status,'partial');
  assert.equal(view.ai.unfilteredStatus,'complete');assert.equal(view.ai.rejected,0);assert.equal(view.sourcePolicy.aiFactsExcluded,1);
  assert.deepEqual(view.availabilityReports,[goodFact]);assert.equal(view.teams[0].availability.reports.length,1);
  assert.ok(view.ai.facts[0].quote.includes('will not miss'));assert.equal(view.collectedAt,packet.collectedAt);
  assert.deepEqual(view.candidate,packet.candidate);assert.deepEqual(view.articles,packet.articles);
  assert.deepEqual(currentAvailabilityView(view),view);
  const db=memoryD1();db.sqlite.exec('CREATE TABLE research_snapshots(id TEXT PRIMARY KEY,fixture_id INTEGER,collected_at TEXT,bucket TEXT,payload TEXT)');
  db.sqlite.prepare('INSERT INTO research_snapshots VALUES(?,?,?,?,?)').run('existing',560626,packet.collectedAt,'bucket',original);
  const stored=await storedResearch(560626,{FOOTPREDICT_DB:db});
  assert.equal(stored.ai.facts.length,1);
  const stale=await getResearch(560626,{FOOTPREDICT_DB:db},{now:()=>Date.parse('2026-10-30T16:00:00Z'),
    reserve:async()=>{throw Object.assign(new Error('Дневной бюджет исчерпан'),{status:429});}});
  assert.equal(stale.stale,true);assert.equal(stale.ai.facts.length,1);assert.equal(stale.refreshReason,'daily_budget');
  assert.equal(db.sqlite.prepare('SELECT payload FROM research_snapshots').get().payload,original);db.close();
});

test('a fully excluded selection cannot leave the availability or AI coverage green',()=>{
  const source=article({id:'fpl',title:'FPL injury update'}),fact={sourceId:'fpl',url:source.url,team:source.team,quote:source.title};
  const view=currentAvailabilityView({articles:[source],ai:{status:'complete',facts:[fact]},availabilityReports:[fact],coverage:{ai:true,availabilityReports:true},
    teams:[{availability:{reports:[fact],status:'reports_found',medicalStatus:'unknown'}}]});
  assert.equal(view.ai.status,'filtered');assert.equal(view.coverage.ai,false);assert.equal(view.coverage.availabilityReports,false);
  assert.equal(view.teams[0].availability.status,'no_supported_reports');assert.equal(view.teams[0].availability.medicalStatus,'unknown');
});
