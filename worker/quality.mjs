import {modelV2Info} from './model-v2.mjs';

export const QUALITY_VERSION='quality-1.0.0';
const V1='poisson-1.0.0',HOUR=3600000;
const parse=value=>typeof value==='string'?JSON.parse(value):value;

function distribution(values,counts=false){
  if(!Array.isArray(values)||values.length!==3||values.some(v=>typeof v!=='number'||!Number.isFinite(v)||v<0||(!counts&&v>1)))return null;
  const total=values.reduce((sum,v)=>sum+v,0);
  return total>0&&Number.isFinite(total)&&(counts||Math.abs(total-1)<1e-6)?values.map(v=>v/total):null;
}

export function scoreQuality(probabilities,score){
  const actual=score.home>score.away?0:score.home===score.away?1:2;
  const max=Math.max(...probabilities),leaders=probabilities.map((p,i)=>Math.abs(p-max)<=1e-12?i:-1).filter(i=>i>=0);
  return {brier:probabilities.reduce((sum,p,i)=>sum+(p-Number(i===actual))**2,0),
    logLoss:-Math.log(Math.max(1e-12,probabilities[actual])),actual,actualProbability:probabilities[actual],
    top:leaders.length===1?leaders[0]:null,topProbability:max,correct:leaders.length===1?leaders[0]===actual:null};
}

function summarize(records){
  const scored=records.filter(record=>record.metrics),eligible=scored.filter(record=>record.metrics.correct!==null);
  const average=key=>scored.length?scored.reduce((sum,row)=>sum+row.metrics[key],0)/scored.length:null;
  return {n:scored.length,brier:average('brier'),logLoss:average('logLoss'),
    correct:eligible.filter(row=>row.metrics.correct).length,topEligible:eligible.length,ties:scored.length-eligible.length,
    accuracy:eligible.length?eligible.filter(row=>row.metrics.correct).length/eligible.length:null};
}

function forecast(row,key,now){
  const observation=parse(row.latest)||{status:'pending'},probabilities=distribution(parse(row.probabilities));
  const saved=Date.parse(row.saved_at),kickoff=Date.parse(row.kickoff_at),checked=Date.parse(observation.checkedAt);
  const homeId=row.home_id,awayId=row.away_id;
  let reason=null,state=['pending','live'].includes(observation.status)?'pending':observation.status==='settled'?'settled':'review';
  if(!probabilities)reason='invalid_probabilities';
  else if(![homeId,awayId,row.fixture_id].every(v=>Number.isSafeInteger(v)&&v>0)||homeId===awayId)reason='invalid_identity';
  else if(!Number.isFinite(saved)||!Number.isFinite(kickoff)||saved>now||kickoff-saved<(key==='v2'?24*HOUR:5*60000))reason='invalid_prediction_time';
  else if(key==='v1'&&(row.provider!=='football-data.org'||row.trust!=='server-recorded'||row.market!=='1X2_REGULATION'||row.prediction_id!==`fd:${row.fixture_id}:${V1}`))reason='unverified_prediction';
  const score=key==='v1'?observation.result:observation.score;
  if(!reason&&state==='settled'){
    if(!score||![score.home,score.away].every(v=>Number.isInteger(v)&&v>=0)||!Number.isFinite(checked)||checked<kickoff+4*HOUR||checked>now)reason='unverified_result';
    else if(key==='v1'&&Date.parse(observation.kickoffAt)!==kickoff)reason='changed_kickoff';
  }
  if(reason)state='excluded';
  return {fixtureId:row.fixture_id,key,modelVersion:row.model_version,kickoff:row.kickoff_at,savedAt:row.saved_at,
    homeId,awayId,home:row.home_name,away:row.away_name,league:row.league||'PL',probabilities,
    baseProbabilities:key==='v1'?distribution(parse(row.base_probabilities),true):null,
    state,reason,sourceStatus:observation.status,checkedAt:observation.checkedAt||null,
    score:state==='settled'?score:null,metrics:state==='settled'?scoreQuality(probabilities,score):null};
}

function cohort(records,key,modelVersion){
  return {key,modelVersion,total:records.length,pending:records.filter(r=>r.state==='pending').length,
    review:records.filter(r=>r.state==='review').length,excluded:records.filter(r=>r.state==='excluded').length,
    metrics:summarize(records),leagues:[...new Set(records.map(r=>r.league))].sort()};
}

export function buildQuality(v1Rows,v2Rows,now=Date.now()){
  const info=modelV2Info();
  const v1=v1Rows.filter(row=>row.model_version===V1).map(row=>forecast(row,'v1',now));
  const v2=v2Rows.filter(row=>row.model_version===info.modelVersion).map(row=>forecast(row,'v2',now));
  const fixtures=new Map();
  for(const record of [...v1,...v2]){
    if(!fixtures.has(record.fixtureId))fixtures.set(record.fixtureId,{fixtureId:record.fixtureId,kickoff:record.kickoff,home:record.home,away:record.away,league:record.league});
    fixtures.get(record.fixtureId)[record.key]=record;
  }
  const paired={common:0,pending:0,review:0,excluded:0,reasons:{},models:null},pairedV1=[],pairedV2=[],pairedBase=[];
  for(const fixture of fixtures.values()){
    const a=fixture.v1,b=fixture.v2;
    if(!a||!b){fixture.pairState='unpaired';continue;}
    paired.common++;
    let reason=null,state;
    if(a.homeId!==b.homeId||a.awayId!==b.awayId||Date.parse(a.kickoff)!==Date.parse(b.kickoff)||a.league!==b.league){state='review';reason='fixture_mismatch';}
    else if(a.state==='review'||b.state==='review'){state='review';reason='result_requires_review';}
    else if(a.state==='excluded'||b.state==='excluded'){state='excluded';reason='invalid_record';}
    else if(Date.parse(a.kickoff)-Date.parse(a.savedAt)<24*HOUR){state='excluded';reason='v1_less_than_24h';}
    else if(!a.baseProbabilities){state='excluded';reason='missing_base';}
    else if(a.state==='pending'||b.state==='pending')state='pending';
    else if(a.score.home!==b.score.home||a.score.away!==b.score.away){state='review';reason='score_disagreement';}
    else {state='settled';pairedV1.push(a);pairedV2.push(b);pairedBase.push({metrics:scoreQuality(a.baseProbabilities,a.score)});}
    fixture.pairState=state;fixture.pairReason=reason;
    if(state!=='settled')paired[state]++;
    if(reason)paired.reasons[reason]=(paired.reasons[reason]||0)+1;
  }
  paired.models={v1:summarize(pairedV1),v2:summarize(pairedV2),base:summarize(pairedBase)};
  const all=[...fixtures.values()].sort((a,b)=>Date.parse(b.kickoff)-Date.parse(a.kickoff)||b.fixtureId-a.fixtureId);
  return {schemaVersion:1,version:QUALITY_VERSION,generatedAt:new Date(now).toISOString(),
    cohorts:{v1:cohort(v1,'v1',V1),v2:cohort(v2,'v2',info.modelVersion)},paired,
    fixtures:all.slice(0,200),fixtureScope:{total:all.length,returned:Math.min(200,all.length),limit:200},
    model:{modelVersion:info.modelVersion,asOf:info.asOf,newsUsedInProbabilities:info.newsUsedInProbabilities,promotedToDefault:info.promotedToDefault},
    historical:info.benchmark};
}

// Compact projections avoid parsing large immutable input snapshots. This route
// reads existing ledgers only: no migration, provider request, quota or writes.
export async function qualityReport(db,now=null){
  const v1=await db.prepare(`SELECT id AS prediction_id,fixture_id,model_version,kickoff_at,saved_at,
    json_extract(snapshot,'$.provider') AS provider,json_extract(snapshot,'$.trust') AS trust,
    json_extract(snapshot,'$.market') AS market,json_extract(snapshot,'$.league') AS league,
    json_extract(snapshot,'$.homeId') AS home_id,json_extract(snapshot,'$.awayId') AS away_id,
    json_extract(snapshot,'$.homeName') AS home_name,json_extract(snapshot,'$.awayName') AS away_name,
    json_extract(snapshot,'$.probabilities') AS probabilities,json_extract(snapshot,'$.stats.baseCounts') AS base_probabilities,
    (SELECT payload FROM observations WHERE prediction_id=p.id ORDER BY seq DESC LIMIT 1) AS latest
    FROM predictions p WHERE model_version=?1`).bind(V1).all();
  const v2=await db.prepare(`SELECT fixture_id,model_version,kickoff_at,saved_at,probabilities,
    json_extract(metadata,'$.homeId') AS home_id,json_extract(metadata,'$.awayId') AS away_id,
    json_extract(metadata,'$.home') AS home_name,json_extract(metadata,'$.away') AS away_name,
    (SELECT payload FROM automation_results WHERE fixture_id=p.fixture_id ORDER BY seq DESC LIMIT 1) AS latest
    FROM automation_predictions p WHERE model_version=?1`).bind(modelV2Info().modelVersion).all();
  return buildQuality(v1.results,v2.results,now??Date.now());
}
