// Workers AI may return a parsed `response` beside OpenAI completion metadata.
// Read only explicit answer fields; never treat refusal, tool calls or a partial
// completion as a finished answer. Diagnostics contain shapes, not model text.
class AnswerError extends Error {
  constructor(reason,message){super(message);this.reason=reason;}
}
const bad=(reason,message)=>{throw new AnswerError(reason,message);};
const type=value=>value===null?'null':Array.isArray(value)?'array':typeof value;
const keys=value=>value&&typeof value==='object'?Object.keys(value).filter(key=>/^[a-z_]{1,40}$/i.test(key)).slice(0,12):[];
const hasCalls=value=>value!=null&&(!Array.isArray(value)||value.length>0);

function guard(value){
  if(!value||typeof value!=='object')return;
  if(value.refusal)bad('refusal','AI answer was refused');
  if(hasCalls(value.tool_calls)||value.function_call)bad('tool_call','AI answer requested a tool');
  if(value.finish_reason!=null&&value.finish_reason!=='stop')
    bad(value.finish_reason==='length'?'truncated':value.finish_reason==='content_filter'?'content_filter':'unfinished','AI answer was not completed normally');
  if(value.success===false||value.error||Array.isArray(value.errors)&&value.errors.length)bad('provider_error','AI provider reported an error');
  if(['queued','in_progress','processing','incomplete','failed','cancelled'].includes(value.status))bad('unfinished','AI answer is not complete');
}

export function describeModelAnswer(answer){
  const choice=Array.isArray(answer?.choices)?answer.choices[0]:null,message=choice?.message;
  const content=message?.content??choice?.text;
  const contentCharacters=typeof content==='string'?content.length:Array.isArray(content)?content.reduce((sum,part)=>sum+(typeof part?.text==='string'?part.text.length:0),0):0;
  const responseCharacters=typeof answer?.response==='string'?answer.response.length:typeof answer==='string'?answer.length:0;
  const finish=choice?.finish_reason;
  return {type:type(answer),keys:keys(answer),responseType:type(answer?.response),responseKeys:keys(answer?.response),
    responseCharacters,choicesCount:Array.isArray(answer?.choices)?answer.choices.length:null,
    choice:choice?{keys:keys(choice),messageType:type(message),messageKeys:keys(message),contentType:type(content),contentCharacters,
      parsedType:type(message?.parsed),finishReason:finish==null?null:['stop','length','content_filter','tool_calls','function_call'].includes(finish)?finish:'other'}:null,
    outputCharacters:responseCharacters+contentCharacters};
}

export function parseModelAnswer(answer){
  let candidate=answer,path='root',characters=0;
  for(let depth=0;depth<8;depth++){
    if(typeof candidate==='string'){
      characters+=candidate.length;
      if(characters>16000)bad('too_large','AI output too large');
      let raw=candidate.trim().replace(/^```(?:json)?\s*/i,'').replace(/\s*```$/,'');
      if(!raw.startsWith('{')){const first=raw.indexOf('{'),last=raw.lastIndexOf('}');if(first>=0&&last>first)raw=raw.slice(first,last+1);}
      try{candidate=JSON.parse(raw);}catch{bad('invalid_json','Invalid AI JSON');}
      path+='.json';continue;
    }
    if(!candidate||typeof candidate!=='object'||Array.isArray(candidate))bad('unsupported_shape','Unsupported AI response shape');
    guard(candidate);
    let choice;
    if(candidate.choices!==undefined){
      if(!Array.isArray(candidate.choices)||candidate.choices.length>1)bad('ambiguous_choices','Expected a single AI completion');
      choice=candidate.choices[0];guard(choice);guard(choice?.message);
    }
    if(Array.isArray(candidate.facts))return {value:candidate,path};
    // JSON mode's documented parsed response is authoritative. Metadata-only
    // choices must not shadow it; an explicit incomplete/refused choice above
    // still rejects the entire envelope.
    if(candidate.response!=null&&candidate.response!==''){candidate=candidate.response;path+='.response';continue;}
    if(candidate.result!=null){candidate=candidate.result;path+='.result';continue;}
    if(choice){
      if(choice.message?.parsed!=null){candidate=choice.message.parsed;path+='.choices[0].message.parsed';continue;}
      let content=choice.message?.content??choice.text;
      if(content==null)bad(choice.delta?'streaming_response':'missing_content','AI completion has no answer content');
      if(Array.isArray(content)){
        if(!content.length||content.some(part=>!['text','output_text'].includes(part?.type)||typeof part.text!=='string'))bad('unsupported_content','Unsupported AI content blocks');
        content=content.map(part=>part.text).join('');
      }
      candidate=content;path+=choice.message?.content!=null?'.choices[0].message.content':'.choices[0].text';continue;
    }
    bad(candidate.choices?'empty_choices':'unsupported_shape','Unsupported AI response shape');
  }
  bad('too_deep','AI response nesting limit exceeded');
}

export const normalizeModelAnswer=answer=>parseModelAnswer(answer).value;
