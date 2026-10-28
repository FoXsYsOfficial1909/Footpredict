// Persistent, bounded scheduled work. No process in the editor is required.
import SOURCES from './research-sources.mjs';
import {ensureResearchStorage, getResearch, getNews, cleanText} from './research.mjs';
import {modelV2Info} from './model-v2.mjs';

export const AUTOMATION_VERSION = 'automation-1.0.0';
export const AUTOMATION_CRONS = Object.freeze(['17 */2 * * *', '7,37 * * * *', '50 4 * * *']);
const HOUR = 3600000, DAY = 24 * HOUR;
const initialization = new WeakMap();
const iso = value => new Date(value).toISOString();
const json = value => JSON.stringify(value);
const parse = value => value ? JSON.parse(value) : null;

export async function automationStorage(env) {
  const raw = env.FOOTPREDICT_DB;
  if (!raw) throw Object.assign(new Error('Не подключена база автоматических задач.'), {status:503});
  if (!initialization.has(raw)) {
    const promise = (async () => {
      await ensureResearchStorage(env);
      const statements = [
        `CREATE TABLE IF NOT EXISTS automation_jobs (
          id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'waiting',
          due_at INTEGER NOT NULL,lease_until INTEGER NOT NULL DEFAULT 0,lease_token TEXT,
          attempts INTEGER NOT NULL DEFAULT 0,total_runs INTEGER NOT NULL DEFAULT 0,
          last_started_at TEXT,last_finished_at TEXT,last_success_at TEXT,last_error TEXT,result TEXT)`,
        'CREATE INDEX IF NOT EXISTS automation_due ON automation_jobs(kind,status,due_at,lease_until)',
        `CREATE TABLE IF NOT EXISTS automation_runs (
          id TEXT PRIMARY KEY,job_id TEXT NOT NULL,kind TEXT NOT NULL,started_at TEXT NOT NULL,
          finished_at TEXT NOT NULL,outcome TEXT NOT NULL,result TEXT NOT NULL)`,
        'CREATE INDEX IF NOT EXISTS automation_runs_time ON automation_runs(finished_at)',
        'CREATE TABLE IF NOT EXISTS automation_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)',
        `CREATE TABLE IF NOT EXISTS automation_news (
          id TEXT PRIMARY KEY,team TEXT NOT NULL,url TEXT NOT NULL,published_at TEXT NOT NULL,
          first_seen_at TEXT NOT NULL,payload TEXT NOT NULL)`,
        'CREATE INDEX IF NOT EXISTS automation_news_seen ON automation_news(first_seen_at)',
        `CREATE TABLE IF NOT EXISTS automation_predictions (
          fixture_id INTEGER PRIMARY KEY,kickoff_at TEXT NOT NULL,saved_at TEXT NOT NULL,
          model_version TEXT NOT NULL,probabilities TEXT NOT NULL,snapshot_id TEXT NOT NULL,
          metadata TEXT NOT NULL)`,
        'CREATE INDEX IF NOT EXISTS automation_predictions_kickoff ON automation_predictions(kickoff_at)',
        `CREATE TABLE IF NOT EXISTS automation_results (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,fixture_id INTEGER NOT NULL,state_key TEXT NOT NULL,
          checked_at TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(fixture_id,state_key))`,
        'CREATE INDEX IF NOT EXISTS automation_results_fixture ON automation_results(fixture_id,seq DESC)',
        `CREATE TABLE IF NOT EXISTS automation_reports (
          day TEXT PRIMARY KEY,scheduled_for TEXT NOT NULL,generated_at TEXT NOT NULL,payload TEXT NOT NULL)`
      ];
      await raw.batch(statements.map(sql => raw.prepare(sql)));
      await raw.prepare(`INSERT INTO automation_meta (key,value) VALUES ('installed',?1)
        ON CONFLICT(key) DO NOTHING`).bind(json({installedAt:iso(Date.now())})).run();
    })();
    initialization.set(raw, promise);
    promise.catch(() => initialization.delete(raw));
  }
  await initialization.get(raw);
  return raw.withSession ? raw.withSession('first-primary') : raw;
}

export async function enqueue(db, id, kind, payload, dueAt) {
  await db.prepare(`INSERT INTO automation_jobs (id,kind,payload,due_at) VALUES (?1,?2,?3,?4)
    ON CONFLICT(id) DO UPDATE SET
      due_at=CASE WHEN automation_jobs.kind='research' AND json_extract(automation_jobs.payload,'$.kickoff')!=json_extract(excluded.payload,'$.kickoff')
        THEN MIN(automation_jobs.due_at,excluded.due_at) ELSE automation_jobs.due_at END,
      status=CASE WHEN automation_jobs.status='complete' AND automation_jobs.kind='research'
        AND json_extract(automation_jobs.payload,'$.kickoff')!=json_extract(excluded.payload,'$.kickoff')
        THEN 'waiting' ELSE automation_jobs.status END,
      payload=excluded.payload WHERE automation_jobs.status!='running'`)
    .bind(id,kind,json(payload),dueAt).run();
}

export async function claimJob(db, kind, now, {id=null, leaseMs=180000}={}) {
  const token = crypto.randomUUID();
  // Both selection and acquisition occur in one SQL statement, across isolates.
  const row = await db.prepare(`UPDATE automation_jobs SET status='running',lease_until=?1,
    lease_token=?2,attempts=attempts+1,last_started_at=?3
    WHERE id=(SELECT id FROM automation_jobs WHERE kind=?4 AND (?5 IS NULL OR id=?5)
      AND due_at<=?6 AND (status IN ('waiting','retry') OR (status='running' AND lease_until<=?6))
      ORDER BY due_at,id LIMIT 1)
    AND due_at<=?6 AND (status IN ('waiting','retry') OR (status='running' AND lease_until<=?6)) RETURNING *`)
    .bind(now+leaseMs,token,iso(now),kind,id,now).first();
  return row ? {...row,payload:parse(row.payload)} : null;
}

export function retryTime(error, now, attempt=1) {
  if (error?.retryAt && Number.isFinite(error.retryAt)) return error.retryAt;
  if (error?.status === 429 && /Дневной|daily_budget/.test(String(error.message))) {
    return Date.parse(iso(now).slice(0,10)+'T00:01:00Z')+DAY;
  }
  if (error?.status === 429 && /Минутный|minute_budget/.test(String(error.message))) return Math.floor(now/60000)*60000+120000;
  return now+Math.min(2*HOUR,15*60000*2**Math.min(4,Math.max(0,attempt-1)));
}

function publicError(error) {
  const message = cleanText(error?.message || 'Не удалось завершить задачу.',240)
    .replace(/Bearer\s+[^\s,;]+/gi,'Bearer [redacted]')
    .replace(/((?:api[_-]?key|token|secret)\s*[:=]\s*)[^\s,;]+/gi,'$1[redacted]');
  return {status:Number.isInteger(error?.status)?error.status:null,message};
}

export async function finishJob(db, job, now, {result={},nextAt=now+2*HOUR,complete=false,error=null}={}) {
  const outcome = error ? 'failed' : result.deferred ? 'deferred' : result.partial ? 'partial' : 'ok';
  const detail = error ? publicError(error) : result;
  const log = db.prepare(`INSERT INTO automation_runs (id,job_id,kind,started_at,finished_at,outcome,result)
    SELECT ?1,?2,?3,?4,?5,?6,?7 WHERE EXISTS (SELECT 1 FROM automation_jobs WHERE id=?2 AND lease_token=?1)
    ON CONFLICT(id) DO NOTHING`).bind(job.lease_token,job.id,job.kind,job.last_started_at,iso(now),outcome,json(detail));
  const update = db.prepare(`UPDATE automation_jobs SET status=?1,due_at=?2,lease_until=0,lease_token=NULL,
    attempts=CASE WHEN ?3 THEN attempts ELSE 0 END,total_runs=total_runs+1,last_finished_at=?4,
    last_success_at=CASE WHEN ?3 OR ?5 THEN last_success_at ELSE ?4 END,last_error=?6,result=?7
    WHERE id=?8 AND lease_token=?9`)
    .bind(error?'retry':complete?'complete':'waiting',error?retryTime(error,now,job.attempts):nextAt,Number(!!error),iso(now),Number(!!result.deferred),
      error?json(detail):null,json({...detail,outcome}),job.id,job.lease_token);
  const resultBatch=await db.batch([log,update]);
  return resultBatch[1].meta.changes>0; // An expired worker cannot overwrite its successor.
}

async function hash(value) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(json(value))))].map(v=>v.toString(16).padStart(2,'0')).join('');
}

export function reportWindow(now) {
  let scheduled = Date.parse(iso(now).slice(0,10)+'T04:50:00Z');
  if (scheduled>now) scheduled-=DAY;
  return {day:iso(scheduled).slice(0,10),scheduledFor:iso(scheduled),from:iso(scheduled-DAY),to:iso(scheduled)};
}

async function discover(db,env,services) {
  const now=services.now(),date=new Date(now),year=date.getUTCMonth()>=6?date.getUTCFullYear():date.getUTCFullYear()-1;
  const season=await services.upstream('fd',`/competitions/PL/matches?season=${year}`);
  if (!Array.isArray(season.data?.matches)) throw new Error('Нет календаря АПЛ.');
  const teams=new Map();
  const upcoming=season.data.matches.filter(game=>{
    for(const team of [game.homeTeam,game.awayTeam])if(team?.name)teams.set(team.name,team);
    return Number.isSafeInteger(game.id)&&['TIMED','SCHEDULED'].includes(game.status)&&
      Date.parse(game.utcDate)>now+5*60000&&Date.parse(game.utcDate)<now+7*DAY;
  }).sort((a,b)=>Date.parse(a.utcDate)-Date.parse(b.utcDate)||a.id-b.id).slice(0,20);
  for(const [name] of teams) {
    if(SOURCES.teams[name])await enqueue(db,'news:'+SOURCES.teams[name].bbc_slug,'news',{name},now);
  }
  for(const game of upcoming)await enqueue(db,'research:'+game.id,'research',{fixtureId:game.id,kickoff:game.utcDate},now);
  await db.prepare(`INSERT INTO automation_meta (key,value) VALUES ('league',?1)
    ON CONFLICT(key) DO UPDATE SET value=excluded.value`).bind(json({season:year,clubs:teams.size,mappedClubs:[...teams.keys()].filter(name=>SOURCES.teams[name]).length,upcoming:upcoming.length,fetchedAt:season.fetchedAt})).run();
  return {season:year,clubs:teams.size,upcoming:upcoming.length,partial:[...teams.keys()].some(name=>!SOURCES.teams[name])};
}

async function newsJob(db,env,services,team) {
  const news=await getNews(team,env,services,services.now());
  if(news.error)throw Object.assign(new Error('Лента BBC: '+news.error.reason),{status:news.error.status});
  const rows=[];
  for(const article of news.articles) {
    // Content-addressed immutable observations; unchanged headlines do not grow hourly.
    const id=await hash([article.team,article.url,article.publishedAt,article.title,article.summary]);
    rows.push(db.prepare(`INSERT INTO automation_news (id,team,url,published_at,first_seen_at,payload)
      VALUES (?1,?2,?3,?4,?5,?6) ON CONFLICT(id) DO NOTHING`)
      .bind(id,article.team,article.url,article.publishedAt,article.fetchedAt,json(article)));
  }
  if(rows.length)await db.batch(rows);
  return {team:team.name,articles:news.articles.length,source:'BBC Sport',collectedAt:iso(services.now())};
}

export async function recordPrimary(db, fixtureId, now) {
  const first=await db.prepare(`SELECT id,payload FROM research_snapshots WHERE fixture_id=?1
    AND json_extract(payload,'$.candidate.status')='experimental' ORDER BY collected_at,id LIMIT 1`).bind(fixtureId).first();
  if(!first)return false;
  const packet=parse(first.payload),forecast=packet.candidate,kickoff=Date.parse(packet.fixture.kickoff);
  const generated=Date.parse(forecast.generatedAt),cutoff=Date.parse(packet.featureCutoffAt);
  const p=forecast.probabilities;
  if(!Number.isFinite(generated)||generated!==cutoff||generated>now||kickoff-generated<24*HOUR||
    !Number.isFinite(Date.parse(forecast.asOf+'T00:00:00Z'))||Date.parse(forecast.asOf+'T00:00:00Z')>generated||
    !Array.isArray(p)||p.length!==3||p.some(v=>!Number.isFinite(v)||v<0||v>1)||Math.abs(p.reduce((a,b)=>a+b,0)-1)>1e-8)return false;
  // The first eligible forecast is immutable, including after a model deployment.
  await db.prepare(`INSERT INTO automation_predictions (fixture_id,kickoff_at,saved_at,model_version,probabilities,snapshot_id,metadata)
    VALUES (?1,?2,?3,?4,?5,?6,?7) ON CONFLICT(fixture_id) DO NOTHING`)
    .bind(fixtureId,packet.fixture.kickoff,forecast.generatedAt,forecast.modelVersion,json(p),first.id,
      json({home:packet.fixture.home,away:packet.fixture.away,homeId:packet.teams[0].id,awayId:packet.teams[1].id,
        trainingCutoff:forecast.asOf,horizonHours:(kickoff-generated)/HOUR,newsUsedInProbabilities:false})).run();
  return true;
}

async function researchJob(db,env,services,payload) {
  const now=services.now();
  if(Date.parse(payload.kickoff)<=now+5*60000)return {complete:true,result:{reason:'prediction_window_closed'}};
  const minute=iso(now).slice(0,16);
  if(await services.budgetUsage('fd:minute:'+minute)>2)return {nextAt:now+30*60000,result:{deferred:true,reason:'provider_minute_budget'}};
  await recordPrimary(db,payload.fixtureId,now);
  const packet=await getResearch(payload.fixtureId,env,services);
  await recordPrimary(db,payload.fixtureId,services.now());
  if(packet.stale) {
    const daily=packet.refreshReason==='daily_budget';
    return {nextAt:daily?retryTime({status:429,message:'daily_budget'},now):now+30*60000,result:{deferred:true,reason:packet.refreshReason,collectedAt:packet.collectedAt}};
  }
  const remaining=Date.parse(payload.kickoff)-now;
  return {nextAt:now+(remaining>DAY?DAY:remaining>6*HOUR?6*HOUR:HOUR),result:{fixtureId:payload.fixtureId,collectedAt:packet.collectedAt,
    articles:packet.articles.length,aiStatus:packet.ai.status,aiFacts:packet.ai.facts.length,coverage:packet.coverage,
    sourceErrors:packet.sourceErrors,partial:!packet.coverage.injuries||!packet.coverage.lineups||!packet.coverage.ai}};
}

export function scoreShadow(prediction,game,fetchedAt) {
  const metadata=parse(prediction.metadata),saved=Date.parse(prediction.saved_at),kickoff=Date.parse(prediction.kickoff_at);
  const identity=game.id===prediction.fixture_id&&game.homeTeam?.id===metadata.homeId&&game.awayTeam?.id===metadata.awayId;
  const base={checkedAt:fetchedAt,provider:'football-data.org',providerStatus:game.status};
  if(!identity||game.utcDate!==prediction.kickoff_at||kickoff-saved<24*HOUR)return {...base,status:'review_required',reason:'fixture_identity_or_time_changed'};
  if(game.status==='CANCELLED')return {...base,status:'cancelled'};
  if(game.status!=='FINISHED')return {...base,status:game.status==='POSTPONED'?'postponed':'pending'};
  const score=game.score?.duration==='REGULAR'?game.score.fullTime:game.score?.regularTime;
  if(!score||![score.home,score.away].every(value=>Number.isInteger(value)&&value>=0)||Date.parse(fetchedAt)<kickoff+4*HOUR)
    return {...base,status:'review_required',reason:'unverified_regular_time_score'};
  const p=parse(prediction.probabilities),actual=score.home>score.away?0:score.home===score.away?1:2;
  return {...base,status:'settled',score,actualIndex:actual,
    brier:p.reduce((sum,value,i)=>sum+(value-(i===actual?1:0))**2,0),logLoss:-Math.log(Math.max(1e-12,p[actual])),correct:Number(p.indexOf(Math.max(...p))===actual)};
}

async function settleShadow(db,services) {
  const now=services.now();
  const rows=await db.prepare(`SELECT p.*,(SELECT payload FROM automation_results WHERE fixture_id=p.fixture_id ORDER BY seq DESC LIMIT 1) AS latest
    FROM automation_predictions p WHERE kickoff_at<=?1 AND kickoff_at>=?2
    AND (NOT EXISTS (SELECT 1 FROM automation_results WHERE fixture_id=p.fixture_id)
      OR (SELECT checked_at FROM automation_results WHERE fixture_id=p.fixture_id ORDER BY seq DESC LIMIT 1)<?3)
    ORDER BY kickoff_at LIMIT 20`).bind(iso(now-4*HOUR),iso(now-30*DAY),iso(now-DAY)).all();
  const seasons=new Map();let checked=0;
  for(const row of rows.results) {
    const d=new Date(row.kickoff_at),year=d.getUTCMonth()>=6?d.getUTCFullYear():d.getUTCFullYear()-1;
    if(!seasons.has(year))seasons.set(year,await services.upstream('fd',`/competitions/PL/matches?season=${year}`));
    const source=seasons.get(year),game=source.data?.matches?.find(game=>game.id===row.fixture_id);
    if(!game)continue;
    const observation=scoreShadow(row,game,source.fetchedAt),previous=parse(row.latest);
    const state={...observation};delete state.checkedAt;
    const before=previous?{...previous}:null;if(before)delete before.checkedAt;
    if(json(state)!==json(before)) {
      const prior=await db.prepare('SELECT seq FROM automation_results WHERE fixture_id=?1 ORDER BY seq DESC LIMIT 1').bind(row.fixture_id).first();
      await db.prepare(`INSERT INTO automation_results (fixture_id,state_key,checked_at,payload)
        VALUES (?1,?2,?3,?4) ON CONFLICT(fixture_id,state_key) DO NOTHING`)
        .bind(row.fixture_id,(prior?.seq||0)+':'+await hash(state),source.fetchedAt,json(observation)).run();
    }
    checked++;
  }
  return {selected:rows.results.length,checked};
}

async function metricSummary(db,cutoff) {
  const rows=await db.prepare(`SELECT payload FROM automation_results r WHERE checked_at<=?1 AND seq=(
    SELECT MAX(seq) FROM automation_results WHERE fixture_id=r.fixture_id AND checked_at<=?1)`).bind(cutoff).all();
  const settled=rows.results.map(row=>parse(row.payload)).filter(item=>item.status==='settled');
  return {settled:settled.length,brier:settled.length?settled.reduce((s,r)=>s+r.brier,0)/settled.length:null,
    logLoss:settled.length?settled.reduce((s,r)=>s+r.logLoss,0)/settled.length:null,
    accuracy:settled.length?settled.reduce((s,r)=>s+r.correct,0)/settled.length:null};
}

async function v1Summary(db,cutoff) {
  const rows=await db.prepare(`SELECT json_extract(p.snapshot,'$.probabilities') AS probabilities,
    (SELECT payload FROM observations WHERE prediction_id=p.id AND json_extract(payload,'$.checkedAt')<=?1
      ORDER BY seq DESC LIMIT 1) AS latest FROM predictions p WHERE saved_at<=?1`).bind(cutoff).all();
  const settled=rows.results.map(row=>({p:parse(row.probabilities),o:parse(row.latest)}))
    .filter(row=>row.o?.status==='settled'&&row.o.metrics&&Array.isArray(row.p));
  return {total:rows.results.length,settled:settled.length,
    brier:settled.length?settled.reduce((sum,row)=>sum+row.o.metrics.brier,0)/settled.length:null,
    logLoss:settled.length?settled.reduce((sum,row)=>sum+row.o.metrics.logLoss,0)/settled.length:null,
    accuracy:settled.length?settled.reduce((sum,row)=>sum+Number(row.p.indexOf(Math.max(...row.p))===row.o.metrics.actualIndex),0)/settled.length:null};
}

async function statusFromDb(db,now) {
  const installation=parse((await db.prepare("SELECT value FROM automation_meta WHERE key='installed'").first())?.value);
  const heartbeat=parse((await db.prepare("SELECT value FROM automation_meta WHERE key='tick'").first())?.value);
  const league=parse((await db.prepare("SELECT value FROM automation_meta WHERE key='league'").first())?.value);
  const jobs=await db.prepare(`SELECT id,kind,status,due_at,lease_until,attempts,total_runs,last_started_at,
    last_finished_at,last_success_at,last_error,result FROM automation_jobs WHERE kind!='coordinator'
    AND (status!='complete' OR last_finished_at>=?1) ORDER BY kind,id LIMIT 150`).bind(iso(now-7*DAY)).all();
  const news=await db.prepare('SELECT COUNT(*) AS total,COUNT(DISTINCT team) AS teams,MAX(first_seen_at) AS latest FROM automation_news').first();
  const forecasts=await db.prepare('SELECT COUNT(*) AS total,MAX(saved_at) AS latest FROM automation_predictions').first();
  const cards=await db.prepare('SELECT COUNT(*) AS total,MAX(collected_at) AS latest FROM research_snapshots').first();
  const last=heartbeat?.finishedAt||heartbeat?.startedAt||installation?.installedAt;
  const age=last?now-Date.parse(last):0;
  const state=age>70*60000?'delayed':!heartbeat?'not_started':!heartbeat.finishedAt&&age>240000?'recovering':'running';
  return {version:AUTOMATION_VERSION,checkedAt:iso(now),state,
    installedAt:installation?.installedAt,heartbeat,league,news,forecasts,cards,model:modelV2Info(),
    jobs:jobs.results.map(row=>({...row,dueAt:iso(row.due_at),leaseUntil:row.lease_until?iso(row.lease_until):null,
      last_error:parse(row.last_error),result:parse(row.result)})),
    schedule:{expectedCrons:AUTOMATION_CRONS,collection:'Каждые 30 минут выполняется ограниченная часть очереди; текущие клубные ленты проверяются примерно раз в 2 часа.',
      dailyReport:'07:50 МСК; при пропуске формируется при следующем запуске с отметкой задержки.',timezone:'Europe/Moscow'},
    limitations:['Доступность источников и квоты ограничивают частоту обновлений.','Новости и травмы не меняют вероятности экспериментальной v2.']};
}

async function ensureReport(db,now) {
  const window=reportWindow(now);
  const existing=await db.prepare('SELECT day FROM automation_reports WHERE day=?1').bind(window.day).first();
  if(existing)return false;
  const activity=await db.prepare(`SELECT kind,outcome,COUNT(*) AS count FROM automation_runs
    WHERE finished_at>?1 AND finished_at<=?2 GROUP BY kind,outcome`).bind(window.from,window.to).all();
  const cards=await db.prepare(`SELECT COUNT(*) AS snapshots,COUNT(DISTINCT fixture_id) AS fixtures FROM research_snapshots
    WHERE collected_at>?1 AND collected_at<=?2`).bind(window.from,window.to).first();
  const news=await db.prepare(`SELECT COUNT(*) AS newEvidence,COUNT(DISTINCT team) AS teams FROM automation_news
    WHERE first_seen_at>?1 AND first_seen_at<=?2`).bind(window.from,window.to).first();
  const report={schemaVersion:1,day:window.day,scheduledFor:window.scheduledFor,generatedAt:iso(now),
    lateBySeconds:Math.floor((now-Date.parse(window.scheduledFor))/1000),window:{from:window.from,to:window.to},
    activity:activity.results,cards,news,originalHistory:await v1Summary(db,window.to),experimentalMetrics:await metricSummary(db,window.to),currentHealth:await statusFromDb(db,now),
    notes:['Период активности заканчивается в запланированное время отчёта. Состояние сервиса отражено на момент формирования.',
      'Эксперимент оценивается по первым сохранённым вероятностям не менее чем за 24 часа до игры. Основная история v1 отдельная.',
      'Отчёт доступен в приложении; отправка в мессенджеры не настроена.']};
  await db.prepare(`INSERT INTO automation_reports (day,scheduled_for,generated_at,payload) VALUES (?1,?2,?3,?4)
    ON CONFLICT(day) DO NOTHING`).bind(window.day,window.scheduledFor,report.generatedAt,json(report)).run();
  return true;
}

export async function automationStatus(env,services) {
  return statusFromDb(await automationStorage(env),services.now());
}

export async function dailyReport(env,day=null) {
  if(day&&!/^\d{4}-\d{2}-\d{2}$/.test(day))throw Object.assign(new Error('Нужна дата YYYY-MM-DD.'),{status:400});
  const db=await automationStorage(env);
  const row=day?await db.prepare('SELECT payload FROM automation_reports WHERE day=?1').bind(day).first():
    await db.prepare('SELECT payload FROM automation_reports ORDER BY day DESC LIMIT 1').first();
  return row?parse(row.payload):{status:'not_generated',message:'Первый отчёт появится после запуска расписания.'};
}

export async function shadowHistory(env,cursor=null) {
  const before=cursor?Number(cursor):Number.MAX_SAFE_INTEGER;
  if(!Number.isSafeInteger(before)||before<1)throw Object.assign(new Error('Некорректная страница.'),{status:400});
  const db=await automationStorage(env);
  const rows=await db.prepare(`SELECT p.*,(SELECT payload FROM automation_results WHERE fixture_id=p.fixture_id ORDER BY seq DESC LIMIT 1) AS latest
    FROM automation_predictions p WHERE fixture_id<?1 ORDER BY fixture_id DESC LIMIT 21`).bind(before).all();
  const page=rows.results.slice(0,20);
  return {items:page.map(row=>({fixtureId:row.fixture_id,kickoff:row.kickoff_at,savedAt:row.saved_at,modelVersion:row.model_version,
    probabilities:parse(row.probabilities),metadata:parse(row.metadata),latest:parse(row.latest)||{status:'pending'}})),
    nextCursor:rows.results.length>20?String(page.at(-1).fixture_id):null};
}

export async function shadowEvidence(env,fixtureId) {
  const db=await automationStorage(env);
  const row=await db.prepare(`SELECT s.payload FROM research_snapshots s JOIN automation_predictions p
    ON p.snapshot_id=s.id WHERE p.fixture_id=?1`).bind(fixtureId).first();
  if(!row)throw Object.assign(new Error('Для этого матча ещё нет зафиксированного снимка.'),{status:404});
  return parse(row.payload);
}

export async function runAutomation(env,services,{reason='scheduled',scheduledTime=null}={}) {
  const db=await automationStorage(env),started=services.now();
  await enqueue(db,'system:tick','coordinator',{},started);
  const lease=await claimJob(db,'coordinator',started,{leaseMs:240000});
  if(!lease)return {status:'already_running'};
  const tick={startedAt:iso(started),reason,scheduledFor:scheduledTime?iso(scheduledTime):null,outcomes:[]};
  const meta=async()=>db.prepare(`INSERT INTO automation_meta (key,value) VALUES ('tick',?1)
    ON CONFLICT(key) DO UPDATE SET value=excluded.value`).bind(json(tick)).run();
  await meta();
  try {
    // At the daily trigger, create the report before slow external feeds.
    if(reason==='50 4 * * *') {
      try {tick.reportCreated=await ensureReport(db,services.now());}
      catch(error){tick.reportError=publicError(error);}
    }
    await enqueue(db,'system:discover','discover',{},started);
    await enqueue(db,'system:settle-v1','settle-v1',{},started);
    await enqueue(db,'system:settle-v2','settle-v2',{},started);
    // Isolated job failures are persisted; no single provider prevents other kinds.
    const kinds=['settle-v1','discover','settle-v2','news','news','news','news','news','news','research'];
    for(const kind of kinds) {
      if(services.now()-started>90000)break;
      const job=await claimJob(db,kind,services.now());
      if(!job)continue;
      try {
        let finished;
        if(kind==='discover')finished={nextAt:services.now()+6*HOUR,result:await discover(db,env,services)};
        else if(kind==='news')finished={nextAt:services.now()+2*HOUR,result:await newsJob(db,env,services,job.payload)};
        else if(kind==='research')finished=await researchJob(db,env,services,job.payload);
        else if(kind==='settle-v1')finished={nextAt:services.now()+2*HOUR,result:await services.settleV1()};
        else finished={nextAt:services.now()+2*HOUR,result:await settleShadow(db,services)};
        await finishJob(db,job,services.now(),finished);
        tick.outcomes.push({id:job.id,outcome:finished.result?.deferred?'deferred':finished.result?.partial?'partial':'ok'});
      } catch(error) {
        await finishJob(db,job,services.now(),{error});
        tick.outcomes.push({id:job.id,outcome:'failed',error:publicError(error)});
      }
    }
    try {tick.reportCreated=await ensureReport(db,services.now())||!!tick.reportCreated;delete tick.reportError;}
    catch(error){tick.reportError=publicError(error);}
    // Bounded operational logs; immutable forecasts, source evidence and reports remain.
    await db.prepare('DELETE FROM automation_runs WHERE id IN (SELECT id FROM automation_runs WHERE finished_at<?1 LIMIT 200)').bind(iso(started-90*DAY)).run();
    tick.finishedAt=iso(services.now());
    await meta();
    return {status:'complete',...tick};
  } finally {
    await db.prepare(`UPDATE automation_jobs SET status='waiting',lease_until=0,lease_token=NULL,due_at=?1
      WHERE id='system:tick' AND lease_token=?2`).bind(services.now(),lease.lease_token).run();
  }
}
