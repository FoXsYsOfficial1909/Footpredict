// Free ESPN event metadata. These are provider-published starting XIs, not an
// independent club confirmation and never a prediction from a season roster.
import {cleanText} from './text.mjs';

export const LINEUP_VERSION='espn-starting-xi-1';
export const LINEUP_LIMITS=Object.freeze({dailyRequests:240,refreshSeconds:300,beforeKickoffMinutes:90,afterKickoffHours:6});
const API='https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/';
const pendingByEnv=new WeakMap();
const teamName=team=>cleanText(typeof team==='string'?team:team?.name||team?.displayName,150);
const canonical=name=>teamName(name).normalize('NFKD').replace(/[\u0300-\u036f]/g,'').toLowerCase().replace(/&/g,' and ').replace(/\b(?:fc|afc)\s*$/,'').replace(/[^a-z0-9]+/g,' ').trim();

function context(fixture){
  const home=teamName(fixture?.homeTeam||fixture?.home),away=teamName(fixture?.awayTeam||fixture?.away);
  const kickoff=Date.parse(fixture?.utcDate||fixture?.kickoff),league=fixture?.competition?.code||fixture?.league;
  const seasonStart=Date.parse(fixture?.season?.startDate);
  const date=new Date(kickoff),year=Number.isFinite(seasonStart)?new Date(seasonStart).getUTCFullYear():date.getUTCFullYear()-(date.getUTCMonth()<6?1:0);
  return {id:fixture?.id,home,away,kickoff,year,valid:league==='PL'&&Boolean(canonical(home))&&Boolean(canonical(away))&&canonical(home)!==canonical(away)&&Number.isFinite(kickoff)};
}

function result(ctx,status,reason,extra={}){
  const sourceUrl=extra.fixtureMatch?.providerFixtureId?'https://www.espn.com/soccer/lineups/_/gameId/'+extra.fixtureMatch.providerFixtureId:'https://www.espn.com/soccer/league/_/name/eng.1';
  return {version:LINEUP_VERSION,status,reason,source:{provider:'ESPN',url:sourceUrl},officialConfirmation:false,
    fixture:{id:ctx.id,home:ctx.home,away:ctx.away,kickoff:Number.isFinite(ctx.kickoff)?new Date(ctx.kickoff).toISOString():null},
    fetchedAt:null,teams:[['home',ctx.home],['away',ctx.away]].map(([homeAway,team])=>({team,homeAway,status,players:[],confirmation:'not_received',officialConfirmation:false,sourceUrl,fetchedAt:null})),...extra};
}

function matches(competition,ctx){
  if(!competition||Math.abs(Date.parse(competition.date)-ctx.kickoff)>60000||!Number.isFinite(Date.parse(competition.date)))return false;
  const competitors=competition.competitors;
  if(!Array.isArray(competitors)||competitors.length!==2)return false;
  return ['home','away'].every(side=>competitors.filter(c=>c.homeAway===side&&canonical(c.team?.displayName||c.team?.name)===canonical(ctx[side])).length===1);
}

function projectedSummary(data){
  return {header:{id:data.header?.id,uid:data.header?.uid,season:data.header?.season,
    competitions:(data.header?.competitions||[]).map(c=>({id:c.id,uid:c.uid,date:c.date,competitors:(c.competitors||[]).map(p=>({homeAway:p.homeAway,team:{id:p.team?.id,displayName:p.team?.displayName,name:p.team?.name}}))}))},
    rosters:(data.rosters||[]).slice(0,2).map(r=>({homeAway:r.homeAway,team:{id:r.team?.id,displayName:r.team?.displayName},formation:r.formation,
      predicted:r.predicted,confirmed:r.confirmed,lineupType:r.lineupType,
      roster:(r.roster||[]).slice(0,50).map(p=>({starter:p.starter,athlete:{id:p.athlete?.id,displayName:p.athlete?.displayName,fullName:p.athlete?.fullName},position:{displayName:p.position?.displayName,name:p.position?.name},jersey:p.jersey}))}))};
}

async function cachedRequest(path,env,services,project){
  const now=services.now(),key='lineups:'+LINEUP_VERSION+':'+path;
  let pending=pendingByEnv.get(env);if(!pending){pending=new Map();pendingByEnv.set(env,pending);}
  let saved;
  try{saved=await env.FOOTPREDICT_CACHE?.get(key,'json');}catch{/* A cache failure does not establish a source report. */}
  if(saved?.version===LINEUP_VERSION&&saved.error&&now-Date.parse(saved.fetchedAt)>=0&&now-Date.parse(saved.fetchedAt)<600000)throw Object.assign(new Error('lineup_source_cached_unavailable'),{status:saved.status});
  if(saved&&saved.version===LINEUP_VERSION&&now-Date.parse(saved.fetchedAt)>=0&&now-Date.parse(saved.fetchedAt)<300000&&saved.data&&typeof saved.data==='object')return {...saved,cached:true};
  if(pending.has(key))return pending.get(key);
  const operation=(async()=>{try{
    if(services.reserve)await services.reserve('lineups:day:'+new Date(now).toISOString().slice(0,10),LINEUP_LIMITS.dailyRequests);
    const response=await services.fetch(API+path,{headers:{Accept:'application/json','User-Agent':'FootpredictResearch/1.0'},redirect:'manual',credentials:'omit',cache:'no-store',signal:AbortSignal.timeout(12000)});
    if(!response.ok)throw Object.assign(new Error('lineup_source_unavailable'),{status:response.status});
    if(Number(response.headers.get('content-length'))>1500000)throw new Error('lineup_source_too_large');
    const raw=services.readLimited?await services.readLimited(response.body,1500000):await response.text();
    if(raw.length>1500000)throw new Error('lineup_source_too_large');
    const data=project(JSON.parse(raw)),value={version:LINEUP_VERSION,fetchedAt:new Date(services.now()).toISOString(),data};
    if(services.cachePut)try{await services.cachePut(key,value,300);}catch{/* Keep a valid provider response when optional cache storage is full. */}
    return value;
  }catch(error){
    const value={version:LINEUP_VERSION,fetchedAt:new Date(services.now()).toISOString(),error:'source_unavailable',status:Number.isInteger(error?.status)?error.status:503};
    if(services.cachePut)try{await services.cachePut(key,value,600);}catch{/* A cache outage must not turn missing lineups into a fabricated roster. */}
    throw error;
  }
  })();
  pending.set(key,operation);
  try{return await operation;}finally{pending.delete(key);}
}

export async function getConfirmedLineups(env,services,fixture){
  const ctx=context(fixture),now=services.now();
  if(!ctx.valid)return result(ctx,'unavailable','invalid_fixture');
  if(ctx.kickoff-now>LINEUP_LIMITS.beforeKickoffMinutes*60000)return result(ctx,'not_announced','outside_lineup_window');
  if(now-ctx.kickoff>LINEUP_LIMITS.afterKickoffHours*3600000)return result(ctx,'unavailable','outside_lineup_window');
  try{
    const date=new Date(ctx.kickoff).toISOString().slice(0,10).replace(/-/g,'');
    const board=await cachedRequest('scoreboard?dates='+date+'&limit=50',env,services,data=>({
      leagues:(data.leagues||[]).map(l=>({id:l.id,slug:l.slug,season:{year:l.season?.year}})),
      events:(data.events||[]).slice(0,50).map(e=>({id:e.id,competitions:(e.competitions||[]).map(c=>({id:c.id,date:c.date,competitors:(c.competitors||[]).map(p=>({homeAway:p.homeAway,team:{id:p.team?.id,displayName:p.team?.displayName,name:p.team?.name}}))}))}))}));
    if(!board.data.leagues.some(l=>String(l.id)==='700'&&l.slug==='eng.1'&&l.season?.year===ctx.year))return result(ctx,'unavailable','season_or_league_mismatch',{fetchedAt:board.fetchedAt});
    const events=board.data.events.filter(e=>/^\d{1,15}$/.test(String(e.id))&&e.competitions?.some(c=>String(c.id)===String(e.id)&&matches(c,ctx)));
    if(events.length!==1)return result(ctx,'unavailable','fixture_not_matched',{fetchedAt:board.fetchedAt});
    const id=String(events[0].id),fixtureMatch={providerFixtureId:id,home:ctx.home,away:ctx.away,kickoff:new Date(ctx.kickoff).toISOString(),season:ctx.year,competition:'eng.1'};
    const summary=await cachedRequest('summary?event='+id,env,services,projectedSummary),data=summary.data;
    if(String(data.header?.id)!==id||data.header?.uid!=='s:600~l:700~e:'+id||data.header?.season?.year!==ctx.year||!data.header?.competitions?.some(c=>String(c.id)===id&&matches(c,ctx)))return result(ctx,'unavailable','summary_identity_mismatch',{fetchedAt:summary.fetchedAt,fixtureMatch});
    const base=result(ctx,'not_announced','starting_xi_not_received',{fixtureMatch,fetchedAt:summary.fetchedAt,cached:Boolean(board.cached&&summary.cached)});
    base.teams=base.teams.map(team=>{
      const eventTeam=data.header.competitions.find(c=>String(c.id)===id&&matches(c,ctx)).competitors.find(c=>c.homeAway===team.homeAway)?.team;
      const roster=data.rosters.filter(r=>r.homeAway===team.homeAway&&String(r.team?.id)===String(eventTeam?.id)&&canonical(r.team?.displayName)===canonical(team.team));
      if(roster.length!==1)return {...team,fetchedAt:summary.fetchedAt};
      const r=roster[0],formation=cleanText(r.formation,30),starters=(r.roster||[]).filter(p=>p.starter===true);
      // An exact 11 is necessary but not sufficient: formation and explicit
      // starter flags must accompany the event roster. Predicted metadata is rejected.
      if(!/^[1-5](?:-[1-5]){2,4}$/.test(formation)||formation.split('-').reduce((a,b)=>a+Number(b),0)!==10||r.predicted===true||/predict|probable|project/i.test(String(r.lineupType||''))||starters.length!==11)return {...team,fetchedAt:summary.fetchedAt};
      const players=starters.map(p=>({id:String(p.athlete?.id||''),name:cleanText(p.athlete?.displayName||p.athlete?.fullName,120),position:cleanText(p.position?.displayName||p.position?.name,50),shirtNumber:cleanText(p.jersey,5)}));
      if(players.some(p=>!/^\d{1,15}$/.test(p.id)||!p.name)||new Set(players.map(p=>p.id)).size!==11||new Set(players.map(p=>p.name.toLowerCase())).size!==11)return {...team,fetchedAt:summary.fetchedAt};
      return {...team,status:'reported_by_provider',confirmation:'provider_starting_xi',formation,players,fetchedAt:summary.fetchedAt};
    });
    if(base.teams.some(t=>t.status==='reported_by_provider')){base.status='reported_by_provider';base.reason=base.teams.every(t=>t.status==='reported_by_provider')?'both_starting_xis_received':'partial_starting_xi';}
    return base;
  }catch(error){return result(ctx,'unavailable',error?.status===429?'source_rate_limited':'source_unavailable');}
}
