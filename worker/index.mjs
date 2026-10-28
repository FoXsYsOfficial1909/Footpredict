import {getResearch, storedResearch, researchLimits, RESEARCH_VERSION} from './research.mjs';
import {predictV2, modelV2Info} from './model-v2.mjs';
import {AUTOMATION_VERSION, runAutomation, automationStatus, dailyReport, shadowHistory, shadowEvidence} from './automation.mjs';

const PRIOR_SEASON_WEIGHT = 0.35;
const HALF_LIFE_DAYS = 120;
const REGULARIZATION_GAMES = 5;
const MIN_LEAGUE_GAMES = 15;
const MIN_TEAM_SIDE_GAMES = 2;
const FINISH_SAFETY_HOURS = 4;
const DAY_MS = 86400000;

function completedBefore(games,target,year) {
  const start = Date.parse(target.utcDate);
  // Kickoff is known; true final whistle time is not included in the API.
  // To avoid same-time match leakage, allow only games kicked off 4+ hours earlier.
  const cutoff = Math.min(Date.now(),start) - FINISH_SAFETY_HOURS*3600000;
  if (!Number.isFinite(cutoff)) return [];
  return games.filter(g => {
    const t = Date.parse(g.utcDate);
    return g.id !== target.id && g.status === 'FINISHED' && g.score?.duration === 'REGULAR' &&
      Number.isFinite(t) && t <= cutoff &&
      (g.__year === year || g.__year === year-1) &&
      g.homeTeam?.id && g.awayTeam?.id &&
      Number.isInteger(g.score?.fullTime?.home) && g.score.fullTime.home >= 0 &&
      Number.isInteger(g.score?.fullTime?.away) && g.score.fullTime.away >= 0;
  });
}
function weight(g,target,year) {
  const ageDays = Math.max(0,(Date.parse(target.utcDate)-Date.parse(g.utcDate))/DAY_MS);
  const recency = Math.exp(-Math.LN2 * ageDays / HALF_LIFE_DAYS);
  return recency*(g.__year === year ? 1 : PRIOR_SEASON_WEIGHT);
}
function summary(games,side,weights) {
  let count = 0, sum = 0, goals = 0;
  for (const g of games) {
    const w = weights.get(g);
    if (!(w > 0)) continue;
    count++;
    sum += w;
    goals += w * g.score.fullTime[side];
  }
  return {count,sum,goals};
}
function estimateStats(games,target,year) {
  const prior = completedBefore(games,target,year);
  const home = prior.filter(g => g.homeTeam.id === target.homeTeam.id);
  const away = prior.filter(g => g.awayTeam.id === target.awayTeam.id);
  if (prior.length < MIN_LEAGUE_GAMES ||
      home.length < MIN_TEAM_SIDE_GAMES || away.length < MIN_TEAM_SIDE_GAMES) {
    throw new Error('Мало прошлых матчей: нужны минимум 15 игр лиги и по 2 домашних/гостевых встречи команд.');
  }
  const weights = new Map(prior.map(g => [g,weight(g,target,year)]));
  const leagueHome = summary(prior,'home',weights);
  const leagueAway = summary(prior,'away',weights);
  if (leagueHome.sum < 0.01 || leagueAway.sum < 0.01) {
    throw new Error('Недостаточная суммарная статистическая масса выборки.');
  }
  const leagueH = leagueHome.goals/leagueHome.sum;
  const leagueA = leagueAway.goals/leagueAway.sum;
  if (leagueH < 0.25 || leagueA < 0.25) throw new Error('Слишком низкие средние голы для расчёта.');
  const hs = summary(home,'home',weights);
  const hc = summary(home,'away',weights);
  const as = summary(away,'away',weights);
  const ac = summary(away,'home',weights);
  const smooth = (x,base) => (x.goals+REGULARIZATION_GAMES*base)/(x.sum+REGULARIZATION_GAMES);
  return {
    homeScored:smooth(hs,leagueH), homeConceded:smooth(hc,leagueA),
    awayScored:smooth(as,leagueA), awayConceded:smooth(ac,leagueH),
    leagueHome:leagueH, leagueAway:leagueA,
    leagueCount:prior.length, homeCount:home.length, awayCount:away.length,
    priorLeagueCount:prior.filter(g=>g.__year===year-1).length,
    weightedHomeCount:hs.sum, weightedAwayCount:as.sum,
    baseCounts:(() => {
      const c=[1,1,1];
      for (const g of prior) {
        const h=g.score.fullTime.home,a=g.score.fullTime.away;
        c[h>a?0:h===a?1:2]+=weights.get(g);
      }
      const sum=c.reduce((a,b)=>a+b,0);
      return c.map(n=>n/sum);
    })()
  };
}
function poisson(k,lambda) {
  let p=Math.exp(-lambda);
  for (let i=1;i<=k;i++) p*=lambda/i;
  return p;
}
function predict(stats) {
  const lh=stats.leagueHome,la=stats.leagueAway;
  if (!(lh>0 && la>0)) throw new Error('Средние голы лиги должны быть больше нуля.');
  const h=Math.max(.05,Math.min(5,stats.homeScored*stats.awayConceded/lh));
  const a=Math.max(.05,Math.min(5,stats.awayScored*stats.homeConceded/la));
  let p1=0,px=0,p2=0,best=-1,score='';
  for(let i=0;i<=20;i++) for(let j=0;j<=20;j++) {
    const p=poisson(i,h)*poisson(j,a);
    if(i>j) p1+=p;
    else if(i===j) px+=p;
    else p2+=p;
    if(p>best){best=p;score=i+' : '+j;}
  }
  const total=p1+px+p2;
  return {prob:[p1/total,px/total,p2/total],homeGoals:h,awayGoals:a,score};
}

const OLD = {
  '/leagues': {
    ttl: 86400,
    params: [
      'id', 'name', 'country', 'code',
      'season', 'team', 'type', 'current', 'search'
    ]
  },
  '/teams': {
    ttl: 21600,
    params: [
      'id', 'name', 'league', 'season',
      'country', 'code', 'search'
    ]
  },
  '/fixtures': {
    ttl: 300,
    params: [
      'id', 'ids', 'live', 'date', 'league',
      'season', 'team', 'last', 'next', 'from',
      'to', 'round', 'status', 'venue', 'timezone'
    ]
  }
};

function route(path) {
  if (OLD[path]) {
    return {kind: 'old', path, ...OLD[path]};
  }

  if (path === '/fd/competitions') {
    return {
      kind: 'fd',
      path: '/competitions',
      ttl: 86400,
      params: []
    };
  }

  const m =
    /^\/fd\/competitions\/([A-Za-z0-9]{2,5})\/(teams|matches|standings)$/
      .exec(path);

  if (!m) return null;

  const part = m[2];

  return {
    kind: 'fd',
    path: `/competitions/${m[1].toUpperCase()}/${part}`,
    ttl:
      part === 'teams' ? 43200 :
      part === 'standings' ? 1800 : 300,
    params:
      part === 'teams'
        ? ['season']
        : part === 'standings'
          ? ['season', 'matchday']
          : [
              'season', 'matchday', 'status',
              'dateFrom', 'dateTo', 'stage', 'group'
            ]
  };
}


function tracking(k) {
  return (
    k.startsWith('utm_') ||
    ['fbclid', 'gclid', 'msclkid'].includes(k)
  );
}

function valid(kind, k, v) {
  if (!v || v.length > 80) return false;

  if (kind === 'old') return true;

  if (k === 'season') {
    return /^\d{4}$/.test(v);
  }

  if (k === 'matchday') {
    return /^\d{1,2}$/.test(v) && Number(v) >= 1;
  }

  if (k === 'dateFrom' || k === 'dateTo') {
    return /^\d{4}-\d{2}-\d{2}$/.test(v);
  }

  if (k === 'status') {
    return /^(SCHEDULED|TIMED|LIVE|IN_PLAY|PAUSED|FINISHED|POSTPONED|SUSPENDED|CANCELLED)$/
      .test(v);
  }

  return /^[A-Z0-9_]{1,40}$/.test(v);
}


// FootPredict 1.0. Single-file ES module for Cloudflare Workers.
// Existing proxy routes are retained. D1 is authoritative; KV is a cache only.
const MODEL_VERSION='poisson-1.0.0';
const MARKET='1X2_REGULATION';
const BUFFER=5*60000;
class HttpError extends Error {constructor(status,message){super(message);this.status=status;}}
function fail(status,message){throw new HttpError(status,message);}
function positiveId(v){if(!/^\d{1,12}$/.test(String(v))||Number(v)<1)fail(400,'Некорректный ID.');return Number(v);}
function yearValue(v){if(!/^\d{4}$/.test(String(v))||Number(v)<2000||Number(v)>new Date().getUTCFullYear()+1)fail(400,'Некорректный сезон.');return Number(v);}
function db(env){if(!env.FOOTPREDICT_DB)fail(503,'Не подключена база FOOTPREDICT_DB.');return env.FOOTPREDICT_DB;}
function primary(env){const d=db(env);return d.withSession?d.withSession('first-primary'):d;}
function authorize(req,env){
  if(!env.ADMIN_TOKEN||env.ADMIN_TOKEN.length<32)fail(503,'Токен владельца не настроен.');
  const expected='Bearer '+env.ADMIN_TOKEN,actual=req.headers.get('Authorization')||'';
  // Fixed-time digest comparison; do not log either token.
  return Promise.all([actual,expected].map(s=>crypto.subtle.digest('SHA-256',new TextEncoder().encode(s)))).then(([a,b])=>{
    const x=new Uint8Array(a),y=new Uint8Array(b);let n=0;for(let i=0;i<x.length;i++)n|=x[i]^y[i];if(n)fail(401,'Неверный токен владельца.');
  });
}
function setting(env,key,fallback,min,max){const n=Number(env[key]??fallback);if(!Number.isInteger(n)||n<min||n>max)fail(503,'Некорректная настройка '+key);return n;}
async function reserve(env,name,limit){
  const row=await primary(env).prepare(`INSERT INTO budgets (name,n) VALUES (?1,1)
    ON CONFLICT(name) DO UPDATE SET n=n+1 WHERE n<?2 RETURNING n`).bind(name,limit).first();
  if(!row)fail(429,name.includes(':day:')?'Дневной бюджет запросов исчерпан. Сброс в 00:00 UTC (03:00 по Москве).':'Минутный бюджет запросов исчерпан. Подожди до следующей минуты; не нажимай повторно много раз.');
}
async function providerBudget(env,provider){
  const now=new Date().toISOString(),day=now.slice(0,10),minute=now.slice(0,16);
  const maxDay=setting(env,provider==='fd'?'FD_DAILY_LIMIT':'AF_DAILY_LIMIT',provider==='fd'?200:80,1,10000);
  const maxMinute=setting(env,provider==='fd'?'FD_MINUTE_LIMIT':'AF_MINUTE_LIMIT',provider==='fd'?8:8,1,100);
  // Conservative accounting: a denied minute can still consume daily allowance.
  await reserve(env,provider+':day:'+day,maxDay);
  await reserve(env,provider+':minute:'+minute,maxMinute);
}
async function upstream(env,provider,path,{fresh=false,ttl=300}={}){
  const key='fp10:cache:'+provider+':'+path;
  if(!fresh&&env.FOOTPREDICT_CACHE){
    try{const cached=await env.FOOTPREDICT_CACHE.get(key,'json');if(cached&&Date.now()-Date.parse(cached.fetchedAt)<ttl*1000)return {...cached,source:'KV_CACHE'};}catch{/* Cache failure cannot fabricate data. */}
  }
  const secret=provider==='fd'?env.FOOTBALL_DATA_KEY:env.API_FOOTBALL_KEY;
  if(!secret)fail(503,'Не настроен ключ '+(provider==='fd'?'FOOTBALL_DATA_KEY':'API_FOOTBALL_KEY'));
  await providerBudget(env,provider);
  const base=provider==='fd'?'https://api.football-data.org/v4':'https://v3.football.api-sports.io';
  let r;
  try{r=await fetch(base+path,{headers:provider==='fd'?{'X-Auth-Token':secret}:{'x-apisports-key':secret},signal:AbortSignal.timeout(15000)});}
  catch{fail(502,'Провайдер не ответил вовремя. Прогноз не сохранён.');}
  if(r.status===429)fail(429,'Лимит провайдера. Подожди; автоматических повторов нет.');
  if(r.status===401||r.status===403)fail(403,'Ключ или тариф не разрешает эти данные.');
  if(!r.ok)fail(502,'Провайдер вернул HTTP '+r.status);
  const raw=await readLimited(r.body,2500000);
  let data;try{data=JSON.parse(raw);}catch{fail(502,'Провайдер вернул некорректный JSON.');}
  if(data.errors&&Object.keys(data.errors).length){
    if(data.errors.rateLimit)fail(429,'Лимит API-Football. Подожди; автоматических повторов нет.');
    const explanation=Object.values(data.errors).filter(value=>typeof value==='string').join('; ').replace(/<[^>]*>/g,'').replace(/Bearer\s+[^\s,;]+/gi,'Bearer [redacted]').replace(/((?:api[_-]?key|token|secret|password)\s*[:=]\s*)[^\s,;]+/gi,'$1[redacted]').slice(0,250);
    fail(422,explanation?'API-Football: '+explanation:'API-Football вернул ошибку данных или тарифа.');
  }
  const result={data,fetchedAt:new Date().toISOString(),source:'UPSTREAM'};
  if(!fresh&&env.FOOTPREDICT_CACHE){
    // All KV cache writes are budgeted together, leaving room on the Free plan.
    try{await reserve(env,'kv:day:'+new Date().toISOString().slice(0,10),setting(env,'KV_DAILY_WRITES',700,1,900));await env.FOOTPREDICT_CACHE.put(key,JSON.stringify(result),{expirationTtl:Math.max(60,ttl)});}catch{/* Read-through cache is optional. */}
  }
  return result;
}
function validateUpcoming(g,now=Date.now()){
  if(!g||!['TIMED','SCHEDULED'].includes(g.status)||!Number.isFinite(Date.parse(g.utcDate))||Date.parse(g.utcDate)<=now+BUFFER)fail(409,'Матч уже начался, статус не предматчевый или осталось меньше 5 минут.');
  positiveId(g.id);positiveId(g.homeTeam?.id);positiveId(g.awayTeam?.id);
}
function normalized(g,year){return {id:g.id,utcDate:g.utcDate,status:g.status,homeTeam:{id:g.homeTeam?.id},awayTeam:{id:g.awayTeam?.id},score:g.score,__year:year};}
async function getSaved(env,id){
  const d=primary(env),r=await d.prepare('SELECT snapshot,saved_at FROM predictions WHERE id=?1').bind(id).first();
  if(!r)return null;
  const observation=await d.prepare('SELECT payload FROM observations WHERE prediction_id=?1 ORDER BY seq DESC LIMIT 1').bind(id).first();
  return {prediction:{...JSON.parse(r.snapshot),savedAt:r.saved_at},latest:observation?JSON.parse(observation.payload):{status:'pending'}};
}
async function createPrediction(env,fixtureId){
  fixtureId=positiveId(fixtureId);
  const id='fd:'+fixtureId+':'+MODEL_VERSION;
  const existing=await getSaved(env,id);if(existing)return {...existing,existing:true};
  const first=await upstream(env,'fd','/matches/'+fixtureId,{fresh:true});
  const g=first.data;validateUpcoming(g);
  if(g.id!==fixtureId)fail(502,'Провайдер вернул другой матч.');
  const league=g.competition?.code;
  if(!/^[A-Z0-9]{2,8}$/.test(league||''))fail(422,'Не определён код соревнования.');
  const year=yearValue(String(g.season?.startDate||'').slice(0,4));
  const current=await upstream(env,'fd',`/competitions/${league}/matches?season=${year}`);
  if(!Array.isArray(current.data.matches))fail(502,'Нет массива матчей сезона.');
  let games=current.data.matches.map(x=>normalized(x,year));
  const sources=[{provider:'football-data.org',path:`/competitions/${league}/matches?season=${year}`,fetchedAt:current.fetchedAt}];
  const notices=[];
  try{
    const old=await upstream(env,'fd',`/competitions/${league}/matches?season=${year-1}`,{ttl:3600});
    if(!Array.isArray(old.data.matches))fail(502,'Нет массива матчей прошлого сезона.');
    games=games.concat(old.data.matches.map(x=>normalized(x,year-1)));
    sources.push({provider:'football-data.org',path:`/competitions/${league}/matches?season=${year-1}`,fetchedAt:old.fetchedAt});
  }catch(e){if(e.status!==403)throw e;notices.push('Предыдущий сезон недоступен по ключу/тарифу. Использован текущий.');}
  let stats;try{stats=estimateStats(games,g,year);}catch(e){fail(422,e.message);}
  const result=predict(stats);
  const second=await upstream(env,'fd','/matches/'+fixtureId,{fresh:true});
  const final=second.data;validateUpcoming(final);
  if(final.id!==g.id||final.utcDate!==g.utcDate||final.homeTeam?.id!==g.homeTeam.id||final.awayTeam?.id!==g.awayTeam.id||final.competition?.code!==league)fail(409,'Матч изменился во время расчёта. Выбери его заново.');
  const createdAt=new Date().toISOString();validateUpcoming(final,Date.parse(createdAt));
  const snapshot={schemaVersion:1,predictionId:id,provider:'football-data.org',fixtureId,modelVersion:MODEL_VERSION,market:MARKET,
    createdAt,kickoffAt:g.utcDate,homeId:g.homeTeam.id,awayId:g.awayTeam.id,homeName:g.homeTeam.name,awayName:g.awayTeam.name,league,year,
    probabilities:result.prob,expectedGoals:[result.homeGoals,result.awayGoals],mostLikelyScore:result.score,stats,
    inputs:completedBefore(games,g,year),sources,fixtureChecks:[{fetchedAt:first.fetchedAt,status:g.status},{fetchedAt:second.fetchedAt,status:final.status}],notices,
    parameters:{priorSeasonWeight:PRIOR_SEASON_WEIGHT,halfLifeDays:HALF_LIFE_DAYS,regularizationGames:REGULARIZATION_GAMES,minLeagueGames:MIN_LEAGUE_GAMES,minTeamSideGames:MIN_TEAM_SIDE_GAMES,finishSafetyHours:FINISH_SAFETY_HOURS,goalCap:5,poissonMax:20},trust:'server-recorded'};
  if(JSON.stringify(snapshot).length>1000000)fail(422,'Снимок входов слишком большой.');
  const deadline=Math.floor((Date.parse(g.utcDate)-BUFFER)/1000);
  // Unique provider/fixture/model key plus SQL-time check prevent concurrent overwrite and late commit.
  const r=await primary(env).prepare(`INSERT INTO predictions (id,fixture_id,model_version,kickoff_at,snapshot)
    SELECT ?1,?2,?3,?4,?5 WHERE unixepoch('now') < ?6
    ON CONFLICT(id) DO NOTHING RETURNING id`).bind(id,fixtureId,MODEL_VERSION,g.utcDate,JSON.stringify(snapshot),deadline).first();
  const saved=await getSaved(env,id);
  if(!saved)fail(409,'Не удалось записать прогноз до предматчевого ограничения.');
  return {...saved,existing:!r};
}
function compareResult(p,g,checkedAt){
  let status='pending',result=null,reason='';
  const kickoff=Date.parse(g.utcDate),original=Date.parse(p.kickoffAt);
  if(Number(g.id)!==Number(p.fixtureId)||g.homeTeam?.id!==p.homeId||g.awayTeam?.id!==p.awayId||!Number.isFinite(kickoff)){status='review_required';reason='Не совпадают идентификатор, команды или дата.';}
  else if(kickoff!==original){status='review_required';reason='Время матча изменилось. Старый прогноз исключён из оценки.';}
  else if(kickoff<=Date.parse(p.savedAt||p.createdAt)+BUFFER){status='review_required';reason='Недостаточный запас до начала матча.';}
  else if(g.status==='FINISHED'){
    const score=g.score?.duration==='REGULAR'?g.score.fullTime:g.score?.regularTime;
    if(!score||!Number.isInteger(score.home)||!Number.isInteger(score.away)||score.home<0||score.away<0){status='review_required';reason='Нет надёжного счёта основного времени.';}
    else {status='settled';result={home:score.home,away:score.away};}
  }else if(g.status==='CANCELLED')status='cancelled';
  else if(g.status==='POSTPONED')status='postponed';
  else if(['IN_PLAY','PAUSED','EXTRA_TIME','PENALTY_SHOOTOUT'].includes(g.status))status='live';
  else if(!['TIMED','SCHEDULED'].includes(g.status)){status='review_required';reason='Статус провайдера: '+g.status;}
  const r={status,result,reason,checkedAt,providerStatus:g.status,kickoffAt:g.utcDate};
  if(result){const a=result.home>result.away?0:result.home===result.away?1:2;
    r.metrics={actualIndex:a,brier:p.probabilities.reduce((s,v,i)=>s+(v-(i===a?1:0))**2,0),logLoss:-Math.log(Math.max(1e-12,p.probabilities[a]))};}
  return r;
}
async function checkPrediction(env,id){
  // Checks need only identity, timing and probabilities. Keep large immutable input
  // snapshots in D1 instead of parsing them in the Free Worker's CPU budget.
  const row=await primary(env).prepare(`SELECT fixture_id,kickoff_at,saved_at,
    json_extract(snapshot,'$.homeId') AS home_id,json_extract(snapshot,'$.awayId') AS away_id,
    json_extract(snapshot,'$.probabilities') AS probabilities,
    (SELECT payload FROM observations WHERE prediction_id=p.id ORDER BY seq DESC LIMIT 1) AS latest
    FROM predictions p WHERE id=?1`).bind(id).first();
  if(!row)fail(404,'Прогноз не найден.');
  const saved={prediction:{fixtureId:row.fixture_id,homeId:row.home_id,awayId:row.away_id,kickoffAt:row.kickoff_at,savedAt:row.saved_at,probabilities:JSON.parse(row.probabilities)},latest:row.latest?JSON.parse(row.latest):{status:'pending'}};
  const p=saved.prediction;
  if(Date.now()<Date.parse(p.kickoffAt)+4*3600000)return {latest:saved.latest,message:'Сверка доступна через 4 часа после начала.'};
  const d=primary(env);
  const lease=await d.prepare(`INSERT INTO checks (prediction_id,next_check) VALUES (?1,unixepoch('now')+900)
    ON CONFLICT(prediction_id) DO UPDATE SET next_check=unixepoch('now')+900
    WHERE next_check<=unixepoch('now') RETURNING prediction_id`).bind(id).first();
  if(!lease)return {latest:saved.latest,message:'Повторная сверка не чаще раза в 15 минут.'};
  const response=await upstream(env,'fd','/matches/'+p.fixtureId,{fresh:true});
  const observation=compareResult(p,response.data,response.fetchedAt);
  // Append observations, never modify the forecast. Unique digest deduplicates unchanged state.
  const state={...observation};delete state.checkedAt;
  const digest=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(state))))).map(x=>x.toString(16).padStart(2,'0')).join('');
  // Include predecessor seq so a provider correction back to an earlier score is still recorded.
  const prev=await d.prepare('SELECT seq FROM observations WHERE prediction_id=?1 ORDER BY seq DESC LIMIT 1').bind(id).first();
  const previousState={...saved.latest};delete previousState.checkedAt;
  if(JSON.stringify(previousState)!==JSON.stringify(state)){
    await d.prepare('INSERT INTO observations (prediction_id,state_key,payload) VALUES (?1,?2,?3) ON CONFLICT(prediction_id,state_key) DO NOTHING').bind(id,(prev?.seq||0)+':'+digest,JSON.stringify(observation)).run();
  }
  return {latest:observation,message:'Сверка выполнена. Исходный прогноз сохранён без изменений.'};
}
async function listHistory(env,cursor){
  let before='9999',beforeId='~';
  if(cursor){try{const x=JSON.parse(atob(cursor));if(typeof x[0]!=='string'||typeof x[1]!=='string'||x[0].length>40||x[1].length>100)throw 0;[before,beforeId]=x;}catch{fail(400,'Некорректная страница.');}}
  const d=primary(env);
  const r=await d.prepare(`SELECT id,snapshot,saved_at FROM predictions
    WHERE saved_at < ?1 OR (saved_at=?1 AND id<?2) ORDER BY saved_at DESC,id DESC LIMIT 21`).bind(before,beforeId).all();
  const page=r.results.slice(0,20),items=[];
  for(const row of page){const o=await d.prepare('SELECT payload FROM observations WHERE prediction_id=?1 ORDER BY seq DESC LIMIT 1').bind(row.id).first();items.push({prediction:{...JSON.parse(row.snapshot),savedAt:row.saved_at},latest:o?JSON.parse(o.payload):{status:'pending'}});}
  const last=page.at(-1);
  return {items,nextCursor:r.results.length>20?btoa(JSON.stringify([last.saved_at,last.id])):null};
}
function proxyRoute(url){
  const single=url.pathname.match(/^\/fd\/matches\/(\d{1,12})$/);
  if(single){if(url.search)fail(400,'Неизвестный параметр запроса.');return {provider:'fd',path:'/matches/'+positiveId(single[1]),ttl:300};}
  // Reuse the exact route and parameter allowlists recovered from the existing V5.1 Worker.
  const r=route(url.pathname);if(!r)fail(404,'Маршрут не найден.');
  const pairs=[],seen=new Set();
  for(const [k,v] of url.searchParams){
    if(tracking(k))continue;
    if(seen.has(k)||!r.params.includes(k)||!valid(r.kind,k,v))fail(400,'Некорректный параметр '+k);
    seen.add(k);pairs.push([k,v]);
  }
  if((r.kind==='old'&&!pairs.length)||pairs.length>6)fail(400,'Некорректное число параметров.');
  pairs.sort(([a],[b])=>a.localeCompare(b));
  const q=new URLSearchParams(pairs).toString();
  return {provider:r.kind==='old'?'af':'fd',path:r.path+(q?'?'+q:''),ttl:r.ttl,legacyCacheKey:(r.kind==='old'?'v4:data:':'v5:fd:')+r.path+':'+q};
}
async function readLimited(stream,limit){
  if(!stream)return '';
  const reader=stream.getReader(),chunks=[];let size=0;
  try{while(true){const {value,done}=await reader.read();if(done)break;size+=value.byteLength;if(size>limit){await reader.cancel();fail(413,'Слишком большой запрос или ответ.');}chunks.push(value);}}finally{reader.releaseLock();}
  const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.byteLength;}return new TextDecoder().decode(bytes);
}
async function jsonBody(req){
  if(!req.headers.get('Content-Type')?.startsWith('application/json'))fail(415,'Нужен JSON.');
  const raw=await readLimited(req.body,1024);
  try{return JSON.parse(raw);}catch{fail(400,'Некорректный JSON.');}
}
async function runScheduled(env){
  // The dashboard's single two-hour trigger provides at most 60 attempts/day.
  // next_check remains the shared 15-minute lease, so manual checks stay available.
  const d=primary(env),now=Date.now();
  const rows=await d.prepare(`WITH candidates AS (
    SELECT p.id,p.kickoff_at,c.next_check,
      COALESCE((SELECT json_extract(payload,'$.status') FROM observations
        WHERE prediction_id=p.id ORDER BY seq DESC LIMIT 1),'pending') AS status
    FROM predictions p LEFT JOIN checks c ON c.prediction_id=p.id
    WHERE p.kickoff_at<=?1 AND p.kickoff_at>=?2
  ) SELECT id FROM candidates
    WHERE (status NOT IN ('settled','cancelled') OR kickoff_at>=?3)
      AND (next_check IS NULL OR next_check<=unixepoch('now')-
        (CASE WHEN status IN ('pending','live') THEN 7200 ELSE 86400 END-900))
    ORDER BY COALESCE(next_check,0),kickoff_at,id LIMIT 5`)
    .bind(new Date(now-4*3600000).toISOString(),new Date(now-30*DAY_MS).toISOString(),new Date(now-7*DAY_MS).toISOString()).all();
  const summary={selected:rows.results.length,processed:0,failed:0,rateLimited:false};
  for(const r of rows.results){
    try{await checkPrediction(env,r.id);summary.processed++;}
    catch(e){summary.failed++;console.warn('FootPredict auto-check failed',e.status||503);if(e.status===429){summary.rateLimited=true;break;}}
  }
  // Only transient budget rows are cleaned. Forecasts and observations are append-only.
  await d.prepare('DELETE FROM budgets WHERE name < ?1 AND name LIKE ?2').bind('fd:minute:'+new Date(now-2*DAY_MS).toISOString().slice(0,16),'fd:minute:%').run();
  console.log('FootPredict auto-check',JSON.stringify(summary));
  if(summary.failed)throw new Error('FootPredict auto-check incomplete: '+summary.failed+' failed; no immediate retries.');
  return summary;
}
function researchServices(env){
  return {now:()=>Date.now(),fetch:(...args)=>fetch(...args),readLimited,settleV1:()=>runScheduled(env),
    predictCandidate:predictV2,
    upstream:(provider,path)=>upstream(env,provider,path),
    reserve:(name,limit)=>reserve(env,name,limit),
    budgetUsage:async name=>Number((await primary(env).prepare('SELECT n FROM budgets WHERE name=?1').bind(name).first())?.n||0),
    cachePut:async(key,value,ttl)=>{
      if(!env.FOOTPREDICT_CACHE)return;
      await reserve(env,'kv:day:'+new Date().toISOString().slice(0,10),setting(env,'KV_DAILY_WRITES',700,1,900));
      await env.FOOTPREDICT_CACHE.put(key,JSON.stringify(value),{expirationTtl:Math.max(60,ttl)});
    }};
}
export default {
 async fetch(req,env){
  const headers={'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Vary':'Origin'};
  const origin=req.headers.get('Origin');
  const allowedOrigins=new Set([env.ALLOWED_ORIGIN,'http://127.0.0.1:8000']);
  if(origin&&allowedOrigins.has(origin)){headers['Access-Control-Allow-Origin']=origin;headers['Access-Control-Allow-Methods']='GET, POST, OPTIONS';headers['Access-Control-Allow-Headers']='Content-Type, Authorization';headers['Access-Control-Expose-Headers']='X-FootPredict-Source';}
  const reply=(data,status=200)=>new Response(JSON.stringify(data),{status,headers});
  try{
    if(origin&&!allowedOrigins.has(origin))fail(403,'Этот origin не разрешён.');
    if(req.method==='OPTIONS')return new Response(null,{status:204,headers});
    const u=new URL(req.url);
    if(u.pathname==='/v1/health'&&req.method==='GET'){
      let usage=null;
      if(env.FOOTPREDICT_DB)try{const services=researchServices(env),day=new Date().toISOString().slice(0,10);usage={cards:await services.budgetUsage('research:day:'+day),ai:await services.budgetUsage('research-ai:day:'+day)};}catch{/* Health remains useful when optional counters cannot be read. */}
      return reply({version:'1.2.0',history:'D1',automationVersion:AUTOMATION_VERSION,research:{version:RESEARCH_VERSION,aiBinding:!!env.AI,limits:researchLimits,usage},configured:!!env.FOOTPREDICT_DB&&!!env.FOOTPREDICT_CACHE&&!!env.FOOTBALL_DATA_KEY&&!!env.API_FOOTBALL_KEY&&!!env.ALLOWED_ORIGIN&&!!env.ADMIN_TOKEN&&env.ADMIN_TOKEN.length>=32});
    }
    if(u.pathname==='/v1/automation/status'&&req.method==='GET'){if(u.search)fail(400,'Неизвестный параметр.');return reply(await automationStatus(env,researchServices(env)));}
    if(u.pathname==='/v1/automation/report'&&req.method==='GET'){
      if([...u.searchParams.keys()].some(key=>key!=='day')||u.searchParams.getAll('day').length>1)fail(400,'Неизвестный параметр.');
      return reply(await dailyReport(env,u.searchParams.get('day')));
    }
    if(u.pathname==='/v1/automation/forecasts'&&req.method==='GET'){
      if([...u.searchParams.keys()].some(key=>key!=='cursor')||u.searchParams.getAll('cursor').length>1)fail(400,'Неизвестный параметр.');
      return reply(await shadowHistory(env,u.searchParams.get('cursor')));
    }
    const evidence=u.pathname.match(/^\/v1\/automation\/forecasts\/(\d{1,12})\/evidence$/);
    if(evidence&&req.method==='GET'){if(u.search)fail(400,'Неизвестный параметр.');return reply(await shadowEvidence(env,positiveId(evidence[1])));}
    if(u.pathname==='/v1/model-v2'&&req.method==='GET')return reply(modelV2Info());
    const researchMatch=u.pathname.match(/^\/v1\/research\/(\d{1,12})(\/(?:refresh|stored))?$/);
    if(researchMatch){
      if(u.search)fail(400,'Неизвестный параметр исследования.');
      const refresh=researchMatch[2]==='/refresh',stored=researchMatch[2]==='/stored';
      if(refresh){if(req.method!=='POST')fail(405,'Метод не разрешён.');await authorize(req,env);const body=await jsonBody(req);if(Object.keys(body||{}).length)fail(400,'Тело запроса должно быть пустым объектом.');}
      else if(req.method!=='GET')fail(405,'Метод не разрешён.');
      const card=stored?await storedResearch(positiveId(researchMatch[1]),env):await getResearch(positiveId(researchMatch[1]),env,researchServices(env),{force:refresh});
      return reply(card);
    }
    if(['/health','/diagnostics'].includes(u.pathname)&&req.method==='GET')return reply({version:'FOOTPREDICT-V6.0',keyConfigured:!!env.API_FOOTBALL_KEY,footballDataKeyConfigured:!!env.FOOTBALL_DATA_KEY,storageConfigured:!!env.FOOTPREDICT_CACHE,historyConfigured:!!env.FOOTPREDICT_DB,note:'Diagnostics do not contact providers; request budgets are enforced in D1.'});
    if(u.pathname==='/v1/history'&&req.method==='GET')return reply(await listHistory(env,u.searchParams.get('cursor')));
    if(u.pathname==='/v1/predictions'&&req.method==='POST'){
      await authorize(req,env);const body=await jsonBody(req);
      if(!body||Object.keys(body).some(k=>k!=='fixtureId'))fail(400,'Нужен только fixtureId.');
      return reply(await createPrediction(env,body.fixtureId));
    }
    const m=u.pathname.match(/^\/v1\/history\/([^/]+)\/check$/);
    if(m&&req.method==='POST'){await authorize(req,env);await jsonBody(req);return reply(await checkPrediction(env,decodeURIComponent(m[1])));}
    if(req.method!=='GET')fail(405,'Метод не разрешён.');
    const proxy=proxyRoute(u);
    // Only the public legacy display routes may reuse V5.1's still-unexpired KV entries.
    // Prediction creation never passes here: its inputs require recorded fetchedAt provenance.
    if(proxy.legacyCacheKey&&env.FOOTPREDICT_CACHE){
      try{const old=await env.FOOTPREDICT_CACHE.get(proxy.legacyCacheKey,'json');if(old!==null&&old!==undefined){headers['X-FootPredict-Source']='LEGACY_KV_CACHE';return reply(old);}}catch{/* Continue with the current cache and provider checks. */}
    }
    const r=await upstream(env,proxy.provider,proxy.path,{ttl:proxy.ttl});headers['X-FootPredict-Source']=r.source;return reply(r.data);
  }catch(e){if(e.status===429)headers['Retry-After']='60';return reply({error:e.status?'REQUEST_FAILED':'INTERNAL',message:e.status?e.message:req.method==='GET'?'Ошибка хранилища или конфигурации Worker. Не удалось загрузить данные.':'Ошибка хранилища или конфигурации. Запись не подтверждена; проверь историю перед повтором.'},e.status||503);}
 },
 async scheduled(event,env,ctx){
   ctx.waitUntil(runAutomation(env,researchServices(env),{reason:event.cron||'scheduled',scheduledTime:event.scheduledTime}));
 }
};
