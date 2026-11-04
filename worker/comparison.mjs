import {scoreQuality,summarizeQuality} from './metrics.mjs';

export const COMPARISON_PROTOCOL='paired-live-1';
const V1='poisson-1.0.0',V2='ensemble-v2-experimental-0.1';
const HOUR=3600000,DAY=24*HOUR,iso=t=>new Date(t).toISOString();
const parse=value=>typeof value==='string'?JSON.parse(value):value;
const valid=p=>Array.isArray(p)&&p.length===3&&p.every(v=>typeof v==='number'&&Number.isFinite(v)&&v>=0&&v<=1)&&Math.abs(p.reduce((a,b)=>a+b,0)-1)<1e-8;
const error=(status,message)=>Object.assign(new Error(message),{status});

export function comparisonSchema(){return [
  `CREATE TABLE IF NOT EXISTS comparison_predictions (
    fixture_id INTEGER PRIMARY KEY,kickoff_at TEXT NOT NULL,saved_at TEXT NOT NULL,protocol TEXT NOT NULL,payload TEXT NOT NULL)`,
  `CREATE TABLE IF NOT EXISTS comparison_results (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,fixture_id INTEGER NOT NULL,checked_at TEXT NOT NULL,payload TEXT NOT NULL)`,
  'CREATE INDEX IF NOT EXISTS comparison_results_fixture ON comparison_results(fixture_id,seq DESC)'
];}

function upcoming(game,now){
  return game?.competition?.code==='PL'&&['TIMED','SCHEDULED'].includes(game.status)&&
    [game.id,game.homeTeam?.id,game.awayTeam?.id].every(n=>Number.isSafeInteger(n)&&n>0)&&
    game.homeTeam.id!==game.awayTeam.id&&Date.parse(game.utcDate)-now>=DAY;
}

export async function registerComparison(db,services,{fixtureId,kickoff}){
  if(await db.prepare('SELECT fixture_id FROM comparison_predictions WHERE fixture_id=?1').bind(fixtureId).first())return {complete:true,result:{fixtureId,reason:'already_registered'}};
  const now=services.now();
  if(Date.parse(kickoff)-now<DAY)return {complete:true,result:{fixtureId,reason:'prediction_window_closed'}};
  if(await services.budgetUsage('fd:minute:'+iso(now).slice(0,16))>2)return {nextAt:now+30*60000,result:{deferred:true,reason:'provider_minute_budget'}};
  const year=Number(kickoff.slice(0,4))-(Number(kickoff.slice(5,7))<7?1:0);
  const current=await services.upstream('fd',`/competitions/PL/matches?season=${year}`);
  if(!Array.isArray(current.data?.matches))throw error(502,'Нет календаря для совместной проверки.');
  const game=current.data.matches.find(g=>g.id===fixtureId);
  if(!upcoming(game,services.now()))return {complete:true,result:{fixtureId,reason:'fixture_not_eligible'}};
  const sources=[{path:`/competitions/PL/matches?season=${year}`,fetchedAt:current.fetchedAt}],notices=[];
  let previous=[];
  try{
    const old=await services.upstream('fd',`/competitions/PL/matches?season=${year-1}`,{ttl:3600});
    if(!Array.isArray(old.data?.matches))throw error(502,'Нет предыдущего сезона.');
    previous=old.data.matches;sources.push({path:`/competitions/PL/matches?season=${year-1}`,fetchedAt:old.fetchedAt});
  }catch(e){if(e.status!==403)throw e;notices.push('Предыдущий сезон недоступен по тарифу; v1 и база используют текущий.');}
  if(!services.predictV1)throw error(503,'Не подключён расчёт v1 для совместной проверки.');
  const v1=services.predictV1(current.data.matches,previous,game,year);
  const v2=services.predictCandidate(game.homeTeam.name,game.awayTeam.name,game.utcDate);
  if(v1.modelVersion!==V1||v2.modelVersion!==V2)throw error(422,'Версии моделей не совпадают с протоколом совместной проверки.');
  const final=await services.upstream('fd','/matches/'+fixtureId,{fresh:true});
  sources.push({path:'/matches/'+fixtureId,fetchedAt:final.fetchedAt});
  const savedAt=iso(services.now()),g=final.data;
  if(!upcoming(g,services.now())||g.id!==game.id||g.utcDate!==game.utcDate||g.homeTeam.id!==game.homeTeam.id||g.awayTeam.id!==game.awayTeam.id)
    throw error(409,'Матч изменился во время совместного расчёта.');
  if(![v1.probabilities,v2.probabilities,v1.baseline].every(valid))throw error(422,'Некорректные вероятности совместной проверки.');
  if(sources.some(s=>!Number.isFinite(Date.parse(s.fetchedAt))||Date.parse(s.fetchedAt)>Date.parse(savedAt)))throw error(502,'Не подтверждено время источника.');
  if(!Number.isFinite(Date.parse(v2.asOf+'T00:00:00Z'))||Date.parse(v2.asOf+'T00:00:00Z')>Date.parse(savedAt))throw error(422,'Не подтверждён срез обучения v2.');
  const packet={schemaVersion:1,protocol:COMPARISON_PROTOCOL,market:'1X2_REGULATION',fixtureId,kickoff:g.utcDate,savedAt,
    home:g.homeTeam.name,away:g.awayTeam.name,homeId:g.homeTeam.id,awayId:g.awayTeam.id,league:'PL',year,
    models:{v1:{modelVersion:v1.modelVersion,probabilities:v1.probabilities},
      v2:{modelVersion:v2.modelVersion,probabilities:v2.probabilities,trainingCutoff:v2.asOf,unseenTeams:v2.unseenTeams||[]},
      base:{modelVersion:'league-frequencies-v1',probabilities:v1.baseline}},
    inputs:v1.inputs,stats:v1.stats,parameters:v1.parameters,sources,notices,
    policy:{minimumHorizonHours:24,sharedRegistrationTime:true,newsUsedInProbabilities:false,automaticPromotion:false}};
  const payload=JSON.stringify(packet);
  if(payload.length>200000)throw error(422,'Снимок совместного расчёта слишком большой.');
  // One row freezes all three forecasts atomically. SQL-time guard prevents a
  // slow or terminated worker from committing inside the 24-hour deadline.
  const inserted=await db.prepare(`INSERT INTO comparison_predictions (fixture_id,kickoff_at,saved_at,protocol,payload)
    SELECT ?1,?2,?3,?4,?5 WHERE unixepoch('now')<?6 ON CONFLICT(fixture_id) DO NOTHING`)
    .bind(fixtureId,g.utcDate,savedAt,COMPARISON_PROTOCOL,payload,Math.floor((Date.parse(g.utcDate)-DAY)/1000)).run();
  return {complete:true,result:{fixtureId,registered:inserted.meta.changes>0,savedAt,protocol:COMPARISON_PROTOCOL}};
}

export function observeComparison(packet,game,checkedAt){
  const base={checkedAt,provider:'football-data.org',providerStatus:game?.status};
  if(!game||game.id!==packet.fixtureId||game.homeTeam?.id!==packet.homeId||game.awayTeam?.id!==packet.awayId||game.utcDate!==packet.kickoff||
    Date.parse(packet.kickoff)-Date.parse(packet.savedAt)<DAY)return {...base,status:'review_required',reason:'fixture_identity_or_time_changed'};
  if(game.status==='CANCELLED')return {...base,status:'cancelled'};
  if(game.status!=='FINISHED')return {...base,status:game.status==='POSTPONED'?'postponed':'pending'};
  const score=game.score?.duration==='REGULAR'?game.score.fullTime:game.score?.regularTime;
  if(!score||![score.home,score.away].every(v=>Number.isInteger(v)&&v>=0)||Date.parse(checkedAt)<Date.parse(packet.kickoff)+4*HOUR)
    return {...base,status:'review_required',reason:'unverified_regular_time_score'};
  return {...base,status:'settled',score};
}

export async function settleComparisons(db,services){
  const now=services.now();
  const rows=await db.prepare(`SELECT p.fixture_id,p.payload,(SELECT payload FROM comparison_results WHERE fixture_id=p.fixture_id ORDER BY seq DESC LIMIT 1) AS latest
    FROM comparison_predictions p WHERE kickoff_at<=?1 AND kickoff_at>=?2 AND (NOT EXISTS(SELECT 1 FROM comparison_results WHERE fixture_id=p.fixture_id)
      OR (SELECT checked_at FROM comparison_results WHERE fixture_id=p.fixture_id ORDER BY seq DESC LIMIT 1)<?3)
    ORDER BY kickoff_at LIMIT 20`).bind(iso(now-4*HOUR),iso(now-30*DAY),iso(now-DAY)).all();
  const seasons=new Map();let checked=0;
  for(const row of rows.results){
    const packet=parse(row.payload);
    if(!seasons.has(packet.year))seasons.set(packet.year,await services.upstream('fd',`/competitions/PL/matches?season=${packet.year}`));
    const source=seasons.get(packet.year);
    if(!Array.isArray(source.data?.matches))throw error(502,'Нет массива результатов совместной проверки.');
    const game=source.data.matches.find(g=>g.id===packet.fixtureId);
    const observation=observeComparison(packet,game,source.fetchedAt),before=parse(row.latest);
    const state={...observation};delete state.checkedAt;
    const old=before?{...before}:null;if(old)delete old.checkedAt;
    if(JSON.stringify(state)!==JSON.stringify(old))await db.prepare('INSERT INTO comparison_results (fixture_id,checked_at,payload) VALUES (?1,?2,?3)')
      .bind(packet.fixtureId,source.fetchedAt,JSON.stringify(observation)).run();
    checked++;
  }
  return {selected:rows.results.length,checked};
}

export async function comparisonReport(db,now=Date.now()){
  const exists=await db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='comparison_predictions'").first();
  const records=exists?(await db.prepare(`SELECT fixture_id,kickoff_at,saved_at,protocol,
    json_extract(payload,'$.home') AS home,json_extract(payload,'$.away') AS away,
    json_extract(payload,'$.models') AS models,
    (SELECT payload FROM comparison_results WHERE fixture_id=p.fixture_id AND checked_at<=?2 ORDER BY seq DESC LIMIT 1) AS latest
    FROM comparison_predictions p WHERE protocol=?1 AND saved_at<=?2 ORDER BY kickoff_at DESC,fixture_id DESC`).bind(COMPARISON_PROTOCOL,iso(now)).all()).results:[];
  const totals={registered:records.length,pending:0,settled:0,review:0,excluded:0},scored={v1:[],v2:[],base:[]};
  const fixtures=[];
  for(const row of records){
    const models=parse(row.models),latest=parse(row.latest)||{status:'pending'};
    let state=latest.status==='settled'?'settled':latest.status==='pending'?'pending':'review';
    if(!['v1','v2','base'].every(key=>valid(models?.[key]?.probabilities))||models.v1.modelVersion!==V1||models.v2.modelVersion!==V2||
      !Number.isFinite(Date.parse(row.kickoff_at))||!Number.isFinite(Date.parse(row.saved_at))||Date.parse(row.kickoff_at)-Date.parse(row.saved_at)<DAY||Date.parse(row.saved_at)>now)state='excluded';
    if(state==='settled'&&(!latest.score||![latest.score.home,latest.score.away].every(n=>Number.isInteger(n)&&n>=0)||
      !Number.isFinite(Date.parse(latest.checkedAt))||Date.parse(latest.checkedAt)<Date.parse(row.kickoff_at)+4*HOUR||Date.parse(latest.checkedAt)>now))state='excluded';
    totals[state]++;
    if(state==='settled')for(const key of Object.keys(scored))scored[key].push({metrics:scoreQuality(models[key].probabilities,latest.score)});
    if(fixtures.length<100)fixtures.push({fixtureId:row.fixture_id,home:row.home,away:row.away,kickoff:row.kickoff_at,savedAt:row.saved_at,state,models,latest});
  }
  return {protocol:COMPARISON_PROTOCOL,generatedAt:iso(now),status:exists?'active':'not_started',totals,
    models:Object.fromEntries(Object.entries(scored).map(([key,rows])=>[key,summarizeQuality(rows)])),fixtures,
    fixtureScope:{total:records.length,returned:fixtures.length,limit:100},
    policy:{minimumHorizonHours:24,sharedRegistrationTime:true,newsUsedInProbabilities:false,automaticPromotion:false}};
}

export async function comparisonEvidence(db,fixtureId){
  const exists=await db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='comparison_predictions'").first();
  const row=exists?await db.prepare('SELECT payload FROM comparison_predictions WHERE fixture_id=?1').bind(fixtureId).first():null;
  if(!row)throw error(404,'Для этого матча ещё нет совместного прогноза.');
  return parse(row.payload);
}
