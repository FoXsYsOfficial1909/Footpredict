// Free, structured current-status metadata. FPL is neither a medical source nor a starting XI.
// Keep this separate from availability.mjs: its exclusion of fantasy news still applies.
import SOURCE_CONFIG from './research-sources.mjs';
import {cleanText} from './text.mjs';

export const PLAYER_AVAILABILITY_SOURCE = 'Premier League FPL player availability';
export const PLAYER_AVAILABILITY_LIMITS = Object.freeze({cacheSeconds:3600, failureSeconds:900,
  maxBytes:3000000, maxFixtures:500, maxPlayers:1500, newsAgeDays:14});
const BOOTSTRAP_URL='https://fantasy.premierleague.com/api/bootstrap-static/';
const FIXTURES_URL='https://fantasy.premierleague.com/api/fixtures/';
const CACHE_KEY='research:fpl:availability:v2';
const DAY=86400000;
const STATUS=Object.freeze({a:'available',i:'injured',d:'doubtful',s:'suspended',u:'unavailable'});
const inflight=new WeakMap();
const teamKey=value=>String(value??'').normalize('NFKD').replace(/[\u0300-\u036f]/g,'').toLowerCase().replace(/&/g,'and').replace(/[^a-z0-9]/g,'');
const aliases=new Map();
for(const [name,config] of Object.entries(SOURCE_CONFIG.teams)) {
  aliases.set(teamKey(name),name); aliases.set(teamKey(config.csv_name),name);
}
for(const [alias,name] of Object.entries({'Man Utd':'Manchester United FC','Spurs':'Tottenham Hotspur FC',
  'Coventry City':'Coventry City FC','Hull City':'Hull City AFC','Ipswich Town':'Ipswich Town FC',
  'Nottingham Forest':'Nottingham Forest FC','Brighton and Hove Albion':'Brighton & Hove Albion FC',
  'Tottenham Hotspur':'Tottenham Hotspur FC','Manchester City':'Manchester City FC',
  'Manchester United':'Manchester United FC','Newcastle United':'Newcastle United FC',
  'Leeds United':'Leeds United FC','West Ham United':'West Ham United FC',
  'Wolverhampton Wanderers':'Wolverhampton Wanderers FC'})) aliases.set(teamKey(alias),name);
const safeText=(value,max=600)=>cleanText(value,max).replace(/[<>\u0000-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g,'').trim();
const canonical=value=>aliases.get(teamKey(value))||null;
const timestamp=value=>typeof value==='string'&&value.length<60?Date.parse(value):NaN;
const iso=value=>Number.isFinite(value)?new Date(value).toISOString():null;
const positive=value=>Number.isInteger(value)&&value>0;

function selectedFixture(value={}) {
  const names=[value.homeTeam?.name||value.home,value.awayTeam?.name||value.away];
  return {names:names.map(name=>safeText(name,100)),canonical:names.map(canonical),
    kickoff:timestamp(value.utcDate||value.kickoff),competition:value.competition?.code||value.competition||null};
}

function baseTeam(team) {
  return {team,name:team,status:'unknown',reports:[],registeredPlayerCount:0,excludedPlayers:0,
    roster:null,importance:{status:'unknown',proxy:'registered_squad_season_minutes'},
    medicalStatus:'unknown',fixtureReadiness:'not_confirmed',absenceConfirmed:false,
    rosterCoverage:'FPL registered players only; not a complete first-team squad'};
}

function unavailable(fixture,now,reason,{fetchedAt=null,error=null,cacheStatus=null}={}) {
  const selected=selectedFixture(fixture);
  return {source:PLAYER_AVAILABILITY_SOURCE,sourceType:'official_fpl_metadata',url:BOOTSTRAP_URL,
    fixtureUrl:FIXTURES_URL,status:'unavailable',reason,fetchedAt,checkedAt:iso(now),cacheStatus,
    fixtureMatch:null,teams:selected.names.map(baseTeam),
    coverage:{complete:false,fixtureMatched:false,scope:'current FPL registered-player statuses',
      medicalStatus:'unknown',fixtureReadiness:'not_confirmed'},...(error?{error}:{})};
}

function validShape(bootstrap,fixtures) {
  if(!bootstrap||!Array.isArray(bootstrap.teams)||bootstrap.teams.length<2||bootstrap.teams.length>30||
    !Array.isArray(bootstrap.elements)||bootstrap.elements.length>PLAYER_AVAILABILITY_LIMITS.maxPlayers||
    !Array.isArray(bootstrap.events)||bootstrap.events.length>50||!Array.isArray(fixtures)||
    fixtures.length>PLAYER_AVAILABILITY_LIMITS.maxFixtures)return false;
  const ids=new Set(),clubs=new Set(),players=new Set();
  for(const team of bootstrap.teams) {
    const club=canonical(team?.name);
    if(!positive(team?.id)||ids.has(team.id)||(club&&clubs.has(club)))return false;
    ids.add(team.id); if(club)clubs.add(club);
  }
  for(const player of bootstrap.elements) {
    if(!positive(player?.id)||players.has(player.id))return false;
    players.add(player.id);
  }
  return true;
}

// Explicit departures must not appear as injuries to a club's present squad.
const DEPARTED=/\b(?:has |have )?(?:joined|departed|left the club|left club|transferred|released by)\b|\b(?:contract (?:has )?expired|free agent|loaned (?:out |to ))\b|\breturned to [A-Z][\w .'-]+(?:FC|CF|AFC)\b/i;

/** Match the exact home/away clubs AND UTC kickoff before attaching any current FPL status. */
export function parseAvailability(bootstrap,fixtures,options={}) {
  const fixture=options.fixture||options,now=Number.isFinite(options.now)?options.now:Date.now();
  const fetchedAt=options.fetchedAt||iso(now),fetched=timestamp(fetchedAt);
  const fail=reason=>unavailable(fixture,now,reason,{fetchedAt});
  const selected=selectedFixture(fixture);
  if(selected.competition&&selected.competition!=='PL')return fail('unsupported_competition');
  if(!selected.canonical.every(Boolean)||selected.canonical[0]===selected.canonical[1])return fail('team_not_mapped');
  if(!Number.isFinite(selected.kickoff)||selected.kickoff<=now)return fail('upcoming_fixture_required');
  if(!Number.isFinite(fetched)||fetched>now||now-fetched>=PLAYER_AVAILABILITY_LIMITS.cacheSeconds*1000)return fail('stale_source');
  if(!validShape(bootstrap,fixtures))return fail('invalid_source_schema');
  const clubs=selected.canonical.map(name=>bootstrap.teams.find(team=>canonical(team.name)===name));
  if(clubs.some(team=>!team))return fail('team_not_in_source_season');
  const matched=fixtures.filter(game=>game?.team_h===clubs[0].id&&game?.team_a===clubs[1].id&&timestamp(game.kickoff_time)===selected.kickoff);
  if(matched.length!==1)return fail(matched.length?'ambiguous_fixture':'fixture_not_in_source_season');
  const game=matched[0];
  if(game.started===true||game.finished===true)return fail('source_fixture_already_started');
  if(!positive(game.event)||!bootstrap.events.some(event=>event?.id===game.event))return fail('fixture_gameweek_not_in_source');
  const teams=selected.names.map(baseTeam);
  for(let index=0;index<teams.length;index++) {
    const target=teams[index],players=bootstrap.elements.filter(player=>player.team===clubs[index].id);
    target.roster=[];
    for(const player of players) {
      // 1–4 are football positions; any manager/coach or removed registry entry is excluded.
      const news=safeText(player.news,600);
      if(![1,2,3,4].includes(player.element_type)||player.removed===true||DEPARTED.test(news)) {
        target.excludedPlayers++;continue;
      }
      const name=safeText([player.first_name,player.second_name].filter(Boolean).join(' '),120);
      if(!name){target.excludedPlayers++;continue;}
      const published=timestamp(player.news_added);
      if(Number.isFinite(published)&&published>now) {target.excludedPlayers++;continue;}
      target.registeredPlayerCount++;
      const status=STATUS[player.status]||'unknown';
      const freshness=!news?'status_only':!Number.isFinite(published)?'undated':now-published>PLAYER_AVAILABILITY_LIMITS.newsAgeDays*DAY?'stale':'recent';
      target.roster.push({playerId:player.id,player:name,position:player.element_type,status,
        seasonMinutes:Number.isInteger(player.minutes)&&player.minutes>=0&&player.minutes<=10000?player.minutes:null,
        newsPublishedAt:news?iso(published):null,newsFreshness:freshness});
      // "Available" without news is not a report of medical fitness or a selected starting XI.
      if(status==='available'&&!news)continue;
      target.reports.push({id:'fpl:'+player.id,playerId:player.id,player:name,team:target.team,status,
        providerStatus:typeof player.status==='string'?safeText(player.status,10):null,
        reason:news||null,news:news||null,publishedAt:news?iso(published):null,
        publishedPrecision:news&&Number.isFinite(published)?'timestamp':'unknown',freshness,stale:freshness==='stale',
        source:PLAYER_AVAILABILITY_SOURCE,url:BOOTSTRAP_URL,fetchedAt,
        verification:'official_fantasy_provider_report',scope:'current_player_status',
        medicalStatus:'unknown',fixtureReadiness:'not_confirmed',absenceConfirmed:false});
    }
    target.status=!target.registeredPlayerCount?'unknown':target.reports.length?'reports_found':'no_supported_reports';
    target.importance={status:target.roster.length&&target.roster.every(p=>p.seasonMinutes!==null)?'available':'unknown',
      proxy:'registered_squad_season_minutes',asOf:fetchedAt,
      totalMinutes:target.roster.every(p=>p.seasonMinutes!==null)?target.roster.reduce((n,p)=>n+p.seasonMinutes,0):null,
      scope:'Current FPL registered squad only; minutes proxy is not replacement quality or medical fitness.'};
  }
  const result={source:PLAYER_AVAILABILITY_SOURCE,sourceType:'official_fpl_metadata',url:BOOTSTRAP_URL,
    fixtureUrl:FIXTURES_URL,status:teams.every(team=>team.registeredPlayerCount)?'available':'partial',
    reason:null,fetchedAt,checkedAt:iso(now),fixtureMatch:{id:game.id,kickoff:iso(selected.kickoff),
      home:selected.names[0],away:selected.names[1],gameweek:game.event,validation:'exact_clubs_and_utc_kickoff'},teams,
    coverage:{complete:false,fixtureMatched:true,scope:'current FPL registered-player statuses',
      registeredPlayers:teams.reduce((sum,team)=>sum+team.registeredPlayerCount,0),
      excludedPlayers:teams.reduce((sum,team)=>sum+team.excludedPlayers,0),
      medicalStatus:'unknown',fixtureReadiness:'not_confirmed'},
    notes:['Reported FPL status is not a medical diagnosis or confirmed absence for this fixture.',
      'No report does not establish that all players are healthy. FPL probabilities are not used.']};
  return result;
}

function compact(bootstrap,fixtures) {
  if(!validShape(bootstrap,fixtures))throw Object.assign(new Error('Invalid FPL schema'),{reason:'invalid_source_schema'});
  const fields=['id','team','first_name','second_name','status','news','news_added','element_type','removed','minutes'];
  return {bootstrap:{teams:bootstrap.teams.map(team=>({id:team.id,name:safeText(team.name,100)})),
    events:bootstrap.events.map(event=>({id:event.id})),elements:bootstrap.elements.map(player=>Object.fromEntries(fields.map(field=>[field,player[field]])))},
    fixtures:fixtures.map(game=>Object.fromEntries(['id','event','team_h','team_a','kickoff_time','started','finished'].map(field=>[field,game[field]])))};
}

async function readCache(env,services) {
  try {
    const item=await env.FOOTPREDICT_CACHE?.get(CACHE_KEY,'json');
    const age=services.now()-timestamp(item?.fetchedAt);
    const ttl=item?.error?PLAYER_AVAILABILITY_LIMITS.failureSeconds:PLAYER_AVAILABILITY_LIMITS.cacheSeconds;
    if(item?.version===2&&age>=0&&age<ttl*1000&&(item.error||validShape(item.bootstrap,item.fixtures)))return item;
  } catch { /* Optional cache; failure is never evidence of a healthy squad. */ }
  return null;
}

async function fetchJson(url,services) {
  const response=await services.fetch(url,{headers:{'User-Agent':'FootpredictResearch/0.1','Accept':'application/json'},
    credentials:'omit',redirect:'manual',signal:AbortSignal.timeout(15000)});
  if(!response.ok)throw Object.assign(new Error('Public FPL data unavailable'),{status:response.status,
    reason:response.status===429?'rate_limited':response.status===403?'not_permitted':'http_error'});
  if(Number(response.headers?.get('content-length'))>PLAYER_AVAILABILITY_LIMITS.maxBytes)
    throw Object.assign(new Error('FPL response too large'),{reason:'response_too_large'});
  const raw=await services.readLimited(response.body,PLAYER_AVAILABILITY_LIMITS.maxBytes);
  if(typeof raw!=='string'||new TextEncoder().encode(raw).byteLength>PLAYER_AVAILABILITY_LIMITS.maxBytes)
    throw Object.assign(new Error('FPL response too large'),{reason:'response_too_large'});
  try{return JSON.parse(raw);}catch{throw Object.assign(new Error('Invalid FPL JSON'),{reason:'invalid_source_schema'});}
}

async function collect(env,services) {
  let item;
  try {
    const [bootstrap,fixtures]=await Promise.all([fetchJson(BOOTSTRAP_URL,services),fetchJson(FIXTURES_URL,services)]);
    item={version:2,...compact(bootstrap,fixtures),fetchedAt:iso(services.now())};
  } catch(error) {
    item={version:2,fetchedAt:iso(services.now()),error:{source:PLAYER_AVAILABILITY_SOURCE,
      status:Number.isInteger(error?.status)?error.status:null,reason:error?.reason||'request_failed'}};
  }
  try {await services.cachePut(CACHE_KEY,item,item.error?PLAYER_AVAILABILITY_LIMITS.failureSeconds:PLAYER_AVAILABILITY_LIMITS.cacheSeconds);}catch{/* Optional cache. */}
  return item;
}

function fromItem(item,fixture,services,cacheStatus) {
  if(item?.error)return unavailable(fixture,services.now(),item.error.reason,{fetchedAt:item.fetchedAt,error:item.error,cacheStatus});
  return {...parseAvailability(item.bootstrap,item.fixtures,{fixture,now:services.now(),fetchedAt:item.fetchedAt}),cacheStatus};
}

/** One cache entry for the entire league, shared by every club/fixture. No API key. */
export async function getPlayerAvailability(env,services,fixture) {
  const cached=await readCache(env,services);
  if(cached)return fromItem(cached,fixture,services,'hit');
  let promise=inflight.get(env);
  if(!promise){promise=collect(env,services);inflight.set(env,promise);}
  try{return fromItem(await promise,fixture,services,'miss');}
  finally{if(inflight.get(env)===promise)inflight.delete(env);}
}

/** Read-only refreshes must not create provider calls. */
export async function storedPlayerAvailability(env,services,fixture) {
  const cached=await readCache(env,services);
  return cached?fromItem(cached,fixture,services,'hit'):unavailable(fixture,services.now(),'not_collected',{cacheStatus:'miss'});
}
