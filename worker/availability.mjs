// Select source excerpts; never infer a player's medical fitness or a starting XI.
import {cleanText} from './text.mjs';

const OTHER_SQUADS=/(?:\bwom[ae]n(?:['’]?s)?\b|\bladies\b|\bgirls\b|\bacademy\b|\bunder[- ]?(?:18|19|21|23)s?\b|\bu(?:18|19|21|23)s?\b|\bb[- ]team\b)/i;
const FANTASY=/\b(?:fpl|fantasy(?:[-_\s]*(?:football|premier[-_\s]*league))?)\b/i;
const SPECULATION=/\b(?:predicted|predictions?)\b/i;
export const AVAILABILITY_POLICY='availability-policy-2';
const SIGNALS=[
  ['injury',/\binjur(?:y|ies|ed)\b|\b(?:hamstring|shin|knee|ankle|quad) (?:injury|problem)\b/i],
  ['suspension',/\bsuspen(?:sion|ded)\b|\bred card\b|\bban(?:ned)?\b/i],
  ['availability',/\bruled out\b|\bunavailable\b|\bfitness\b|\b(?:in|a) doubt\b|\b(?:returns?|returning|back) (?:to|in) (?:full |team )?training\b|\b(?:fit|available) (?:for|to face)\b/i],
  ['lineup',/\bteam news\b|\bline[- ]?ups?\b|\bstarting (?:XI|eleven)\b/i]
];

export function isFirstTeamText(text){return !OTHER_SQUADS.test(text);}
export function availabilityCategory(text){return SIGNALS.find(([,pattern])=>pattern.test(text))?.[0]||null;}
export function availabilityPriority(text){if(FANTASY.test(text))return 0;const type=availabilityCategory(text);return type==='injury'||type==='suspension'?3:type==='availability'?2:type==='lineup'?1:0;}

export function isAvailabilitySource(article){
  const title=cleanText(article.title,500),summary=cleanText(article.summary,1600),section=cleanText(article.section,200);
  let address=String(article.url||'');try{address=decodeURIComponent(address);}catch{/* A malformed escape is not decoded. */}
  const text=title+' '+summary+' '+section+' '+address;
  return isFirstTeamText(text)&&!FANTASY.test(text);
}

export function isAvailabilityQuote(value){
  return typeof value==='string'&&value.length>=12&&value.length<=240&&value.split(/\s+/).length<=25&&
    !value.endsWith('?')&&!FANTASY.test(value)&&!SPECULATION.test(value)&&!!availabilityCategory(value);
}

function quoteCandidates(article) {
  const title=cleanText(article.title,500),summary=cleanText(article.summary,1600);
  const candidates=summary.split(/(?<=[.!?;])\s+/u).concat(title);
  const result=[];
  for(let candidate of candidates){
    candidate=candidate.replace(/\s+/g,' ').trim();
    if(!candidate||candidate.endsWith('?')||FANTASY.test(candidate)||SPECULATION.test(candidate))continue;
    if(candidate.split(/\s+/).length>25){
      const intro=candidate.match(/^(.{0,110}?\b(?:explains why|confirms that|said that|says that))\s+(.+)$/i);
      if(intro&&!/\bnot\b/i.test(intro[1]))candidate=intro[2];
    }
    // An entire short sentence/headline is retained, including negation and hedges.
    if(!isAvailabilityQuote(candidate))continue;
    if(!result.includes(candidate))result.push(candidate);
  }
  return result;
}

export function buildEvidence(articles,{maximum=12}={}) {
  const groups=new Map(),seen=new Set();
  for(const article of articles){
    if(!isAvailabilitySource(article))continue;
    const quotes=quoteCandidates(article);
    if(!quotes.length)continue;
    const quality=text=>availabilityPriority(text)+(/\b(?:is|are|was|were|will|expected|ruled|returns?|returning|suffers?|sustains?|miss(?:es|ing)?)\b/i.test(text)?2:0);
    const quote=quotes.sort((a,b)=>quality(b)-quality(a))[0];
    const key=article.team+'\n'+article.url+'\n'+quote;
    if(seen.has(key))continue;seen.add(key);
    if(!groups.has(article.team))groups.set(article.team,[]);
    groups.get(article.team).push({sourceId:article.id,article,quote,category:availabilityCategory(quote),
      priority:availabilityPriority(quote)+(article.sourceType==='official_club'?4:0)});
  }
  for(const group of groups.values())group.sort((a,b)=>b.priority-a.priority||Date.parse(b.article.publishedAt)-Date.parse(a.article.publishedAt));
  const selected=[];
  while(selected.length<maximum&&[...groups.values()].some(group=>group.length)){
    for(const group of groups.values())if(group.length&&selected.length<maximum)selected.push(group.shift());
  }
  return selected.map((item,index)=>({...item,id:'e'+index}));
}

function groundedFact(evidence,{subject=null,method='source_excerpt'}={}) {
  const source=evidence.article;
  return {evidenceId:evidence.id,sourceId:source.id,category:evidence.category,subject,
    quote:evidence.quote,url:source.url,team:source.team,source:source.source,
    sourceType:source.sourceType||'news_report',publishedAt:source.publishedAt,
    publishedPrecision:source.publishedPrecision||'second',fetchedAt:source.fetchedAt,
    extractionMethod:method,verification:source.sourceType==='official_club'?'official_club_report':'reported_in_source',
    fixtureApplicability:'not_confirmed',
    note:'Цитата отражает сообщение источника на указанную дату. Готовность к выбранному матчу и стартовый состав не подтверждаются.'};
}

export function sourceExcerpts(evidence,{perTeam=3}={}) {
  const counts=new Map();
  return evidence.filter(item=>{const count=counts.get(item.article.team)||0;if(count>=perTeam)return false;counts.set(item.article.team,count+1);return true;})
    .map(item=>groundedFact(item));
}

export function validateEvidenceChoices(value,evidence) {
  if(!value||typeof value!=='object'||!Array.isArray(value.facts)||value.facts.length>20)throw new Error('Invalid AI fact schema');
  const index=new Map(evidence.map(item=>[item.id,item])),facts=[],reasons={},seen=new Set();
  const reject=reason=>{reasons[reason]=(reasons[reason]||0)+1;};
  for(const item of value.facts){
    const selected=index.get(item?.evidenceId);
    if(!selected){reject('unknown_evidence_id');continue;}
    if(seen.has(selected.id)){reject('duplicate_evidence');continue;}
    if(item.subject!=null&&typeof item.subject!=='string'){reject('invalid_subject_type');continue;}
    const subject=item.subject==null?null:cleanText(item.subject,80);
    if(subject&&!selected.quote.normalize('NFKC').toLowerCase().includes(subject.normalize('NFKC').toLowerCase())){
      reject('subject_not_in_selected_quote');continue;
    }
    // Type, quote and URLs come from the selected source, not the model.
    seen.add(selected.id);facts.push(groundedFact(selected,{subject,method:'ai_selection'}));
  }
  return {facts,rejected:Object.values(reasons).reduce((a,b)=>a+b,0),rejectionReasons:reasons};
}

// Revalidate the current display without changing any stored observation or
// primary forecast. Raw snapshot downloads keep their original contents.
export function currentAvailabilityView(packet){
  const sources=new Map((packet.articles||[]).map(article=>[article.id,article]));
  const allowed=fact=>{
    const source=sources.get(fact?.sourceId);
    return !!source&&source.url===fact.url&&source.team===fact.team&&isAvailabilitySource(source)&&isAvailabilityQuote(fact.quote)&&
      cleanText(source.title+' '+source.summary,2400).includes(fact.quote);
  };
  const reports=(packet.availabilityReports||[]).filter(allowed),facts=(packet.ai?.facts||[]).filter(allowed);
  const reportsExcluded=(packet.availabilityReports?.length||0)-reports.length,aiFactsExcluded=(packet.ai?.facts?.length||0)-facts.length;
  const prior=packet.sourcePolicy?.version===AVAILABILITY_POLICY?packet.sourcePolicy:null;
  let ai=packet.ai;
  if(ai&&Array.isArray(ai.facts))ai={...ai,facts,...(aiFactsExcluded?{unfilteredStatus:ai.unfilteredStatus||ai.status,
    sourcePolicyExcluded:(ai.sourcePolicyExcluded||0)+aiFactsExcluded,status:facts.length?'partial':'filtered'}:{})};
  return {...packet,...(packet.availabilityReports?{availabilityReports:reports}:{}),...(ai?{ai}:{}),
    ...(packet.teams?{teams:packet.teams.map(team=>team.availability?{...team,availability:{...team.availability,
      reports:(team.availability.reports||[]).filter(allowed),status:(team.availability.reports||[]).some(allowed)?'reports_found':'no_supported_reports'}}:team)}:{}),
    ...(packet.coverage?{coverage:{...packet.coverage,ai:facts.length>0&&['complete','partial'].includes(ai?.status),availabilityReports:reports.length>0}}:{}),
    sourcePolicy:{version:AVAILABILITY_POLICY,reportsExcluded:(prior?.reportsExcluded||0)+reportsExcluded,
      aiFactsExcluded:(prior?.aiFactsExcluded||0)+aiFactsExcluded,archivedSnapshotUnchanged:true}};
}
