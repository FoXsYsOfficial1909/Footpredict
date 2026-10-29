import test from 'node:test';
import assert from 'node:assert/strict';
import {buildEvidence,sourceExcerpts,validateEvidenceChoices} from '../worker/availability.mjs';
import {extractFacts} from '../worker/research.mjs';

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
