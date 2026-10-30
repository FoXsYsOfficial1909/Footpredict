import test from 'node:test';
import assert from 'node:assert/strict';
import {parseModelAnswer,normalizeModelAnswer,describeModelAnswer} from '../worker/ai-response.mjs';
import {extractFacts} from '../worker/research.mjs';

const body={facts:[{evidenceId:'e0',subject:null}]};
const source={id:'club:0',team:'Example FC',url:'https://www.bbc.com/sport/football/articles/smith',
  title:'Alex Smith injury update',summary:'Alex Smith is a doubt after missing training.',publishedAt:'2026-10-30T01:00:00Z'};
// Reconstructed from the documented JSON-mode response plus the observed outer
// completion keys. These are regressions, not recovered historical model output.
const envelope=response=>({id:'completion-example',object:'chat.completion',choices:[{index:0,finish_reason:'stop',message:{role:'assistant',content:null}}],response});

test('JSON-mode response is not shadowed by metadata-only choices',()=>{
  const answer=envelope(body),parsed=parseModelAnswer(answer);
  assert.deepEqual(parsed.value,body);assert.equal(parsed.path,'root.response');
  assert.deepEqual(normalizeModelAnswer({...answer,choices:[]}),body);
  assert.deepEqual(normalizeModelAnswer({...answer,response:JSON.stringify(body)}),body);
  assert.deepEqual(normalizeModelAnswer({result:answer}),body);
});

test('parsed message objects, textual completions and text blocks remain supported',()=>{
  for(const choice of [
    {message:{parsed:body,content:null}}, {message:{content:JSON.stringify(body),tool_calls:[]}},
    {message:{content:[{type:'text',text:JSON.stringify(body)}]}},{text:JSON.stringify(body)}]){
    assert.deepEqual(normalizeModelAnswer({choices:[{finish_reason:'stop',...choice}]}),body);
  }
  assert.deepEqual(normalizeModelAnswer({response:null,choices:[{message:{content:JSON.stringify(body)}}]}),body);
});

test('a parsed body never bypasses truncation, refusal, tool calls or provider errors',()=>{
  const cases=[
    [{...envelope(body),choices:[{finish_reason:'length',message:{content:null}}]},'truncated'],
    [{...envelope(body),choices:[{finish_reason:'content_filter'}]},'content_filter'],
    [{...envelope(body),choices:[{message:{refusal:'No',parsed:body}}]},'refusal'],
    [{...envelope(body),tool_calls:[{function:{name:'execute'}}]},'tool_call'],
    [{...envelope(body),choices:[{message:{tool_calls:[{}]}}]},'tool_call'],
    [{...envelope(body),success:false},'provider_error'],
    [{response:body,status:'in_progress'},'unfinished'],
    [{response:body,choices:[{},{}]},'ambiguous_choices']
  ];
  for(const [answer,reason] of cases)assert.throws(()=>normalizeModelAnswer(answer),error=>error.reason===reason);
});

test('missing content, streaming and unsupported blocks remain rejected with distinct reasons',()=>{
  for(const [answer,reason] of [
    [envelope(null),'missing_content'],[{choices:[]},'empty_choices'],
    [{choices:[{delta:{content:'{"facts":[]}'}}]},'streaming_response'],
    [{choices:[{message:{content:[{type:'image_url',image_url:'https://example.org'}]}}]},'unsupported_content'],
    ['{"facts":[]','invalid_json'],[{response:'x'.repeat(16001)},'too_large']]){
    assert.throws(()=>normalizeModelAnswer(answer),error=>error.reason===reason);
  }
});

test('diagnostics expose field types and lengths without leaking completion text or credentials',async()=>{
  const hidden='Bearer must-never-be-logged';
  const answer={choices:[{finish_reason:'stop',message:{content:JSON.stringify({notFacts:hidden})}}]};
  const shape=describeModelAnswer(answer);
  assert.ok(shape.outputCharacters>0);assert.equal(shape.choice.contentType,'string');
  assert.ok(!JSON.stringify(shape).includes(hidden));
  const result=await extractFacts({run:async()=>answer},[source],async()=>{});
  assert.equal(result.status,'unavailable');assert.equal(result.diagnostic.parseReason,'unsupported_shape');
  assert.ok(!JSON.stringify(result).includes(hidden));
  assert.equal(result.diagnostic.responseShape.choice.messageType,'object');
});

test('extraction requests bounded schema-constrained selection and consumes exactly one call',async()=>{
  let reserved=0,calls=0;
  const result=await extractFacts({run:async(model,input)=>{
    calls++;assert.equal(input.stream,false);assert.equal(input.max_tokens,768);
    assert.equal(input.response_format.type,'json_schema');
    const schema=input.response_format.json_schema;
    assert.equal(schema.properties.facts.maxItems,4);
    assert.deepEqual(schema.properties.facts.items.properties.evidenceId.enum,['e0']);
    return envelope(body);
  }},[source],async()=>{reserved++;});
  assert.equal(reserved,1);assert.equal(calls,1);assert.equal(result.status,'complete');
  assert.equal(result.responseFormat,'root.response');assert.equal(result.facts[0].quote,source.summary);
});

test('unusable completion does not trigger an unbudgeted retry or silently invent facts',async()=>{
  let calls=0,reserved=0;
  const result=await extractFacts({run:async()=>{calls++;return envelope(null);}},[source],async()=>{reserved++;});
  assert.equal(calls,1);assert.equal(reserved,1);assert.equal(result.status,'unavailable');assert.deepEqual(result.facts,[]);
  assert.equal(result.diagnostic.parseReason,'missing_content');
});
