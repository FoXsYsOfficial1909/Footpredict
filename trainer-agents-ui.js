import {rounded} from './forecast-display.js';
const REPORT='https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/trainer-agent-reports/latest.json';
const ARCHIVE=REPORT.slice(0,-'latest.json'.length);
const date=value=>Number.isFinite(Date.parse(value))?new Date(value).toLocaleString('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Moscow'})+' МСК':'—';
const methods=[['candidate','Нейросеть'],['base','Прежняя'],['frequency','Частоты']];
const states={pending:'Ждём результат',settled:'Матч завершён',review:'Требует проверки',unavailable:'Результат недоступен'};
const decimal=value=>value===null?'—':value.toFixed(3).replace('.',',');

function probabilities(p){
  if(!Array.isArray(p)||p.length!==3||!p.every(v=>typeof v==='number'&&Number.isFinite(v)&&v>=0&&v<=1)||
    Math.abs(p.reduce((a,b)=>a+b,0)-1)>1e-8)throw new Error('Invalid frozen probabilities');
  return p;
}

export function probabilityPercentages(values){
  probabilities(values);
  return rounded(values).map(value=>value.replace('.',','));
}

export function validateTrainerDashboard(view,reference=null){
  const c=view?.counts;
  if(view?.schemaVersion!==1||view.protocol!=='neural-agent-dashboard-1'||view.league!=='PL'||
    view.maximumShown!==100||view.selection!=='latest_kickoff_then_fixture_id'||
    !['registered','settled','pending','review','unavailable'].every(k=>Number.isSafeInteger(c?.[k])&&c[k]>=0&&c[k]<=5000)||
    c.registered!==c.settled+c.pending+c.review+c.unavailable||!Array.isArray(view.rows)||
    view.rows.length!==Math.min(c.registered,100)||!Number.isSafeInteger(view.v1FallbackSettled)||
    view.v1FallbackSettled<0||view.v1FallbackSettled>c.settled)throw new Error('Invalid neural series coverage');
  if(reference&&Object.keys(c).some(k=>reference.prospective?.[k]!==c[k]))throw new Error('Mixed neural snapshots');
  for(const [key] of methods){
    const m=view.metrics?.[key];
    if(m?.n!==c.settled||!['correct','topEligible','topTies'].every(k=>Number.isSafeInteger(m[k])&&m[k]>=0&&m[k]<=m.n)||
      m.topEligible+m.topTies!==m.n||
      (m.n?!(Number.isFinite(m.brier)&&m.brier>=0&&m.brier<=2&&Number.isFinite(m.logLoss)&&m.logLoss>=0&&
        Number.isFinite(m.accuracy)&&m.accuracy>=0&&m.accuracy<=1):m.brier!==null||m.logLoss!==null||m.accuracy!==null)||
      (m.topEligible?!(Number.isFinite(m.uniqueTopAccuracy)&&m.uniqueTopAccuracy>=0&&m.uniqueTopAccuracy<=1):m.uniqueTopAccuracy!==null))
      throw new Error('Invalid paired neural metrics');
    if(reference&&Object.keys(m).some(k=>reference.prospective.metrics?.[key]?.[k]!==m[k]))throw new Error('Mixed neural metrics');
  }
  const ids=new Set(),shown={pending:0,settled:0,review:0,unavailable:0};let previous=null;
  for(const row of view.rows){
    const kickoff=Date.parse(row.kickoff),saved=Date.parse(row.registeredAt);
    if(!Number.isSafeInteger(row.fixtureId)||row.fixtureId<=0||ids.has(row.fixtureId)||!Object.hasOwn(states,row.status)||
      !['home','away','modelVersion'].every(k=>typeof row[k]==='string'&&row[k].length>0&&row[k].length<=200)||
      row.home===row.away||typeof row.v1_available!=='boolean'||!Number.isFinite(kickoff)||!Number.isFinite(saved)||
      kickoff-saved<24*3600000||previous&&(kickoff<previous.time||kickoff===previous.time&&row.fixtureId<=previous.id))
      throw new Error('Invalid frozen neural fixture');
    for(const [key] of methods)probabilities(row[key]);
    if(row.status==='settled'){
      const s=row.score;
      if(!s||!['home','away'].every(k=>Number.isSafeInteger(s[k])&&s[k]>=0&&s[k]<=30)||
        row.actual!==(s.home>s.away?0:s.home===s.away?1:2)||!Number.isFinite(Date.parse(row.checkedAt))||
        Date.parse(row.checkedAt)<kickoff+4*3600000)throw new Error('Invalid confirmed neural result');
    }else if(row.score!==undefined||row.actual!==undefined)throw new Error('Unconfirmed neural score');
    ids.add(row.fixtureId);shown[row.status]++;previous={time:kickoff,id:row.fixtureId};
  }
  if(Object.keys(shown).some(k=>shown[k]>c[k]||view.rows.length===c.registered&&shown[k]!==c[k]))
    throw new Error('Incomplete neural display counts');
  return view;
}

export async function verifyTrainerDashboard(raw,digest,reference){
  if(!/^[0-9a-f]{64}$/.test(digest))throw new Error('Invalid neural display digest');
  const bytes=new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(raw)));
  if(Array.from(bytes,v=>v.toString(16).padStart(2,'0')).join('')!==digest)throw new Error('Neural display checksum changed');
  return validateTrainerDashboard(JSON.parse(raw),reference);
}

function el(tag,text,className){const node=document.createElement(tag);if(text!==undefined)node.textContent=text;if(className)node.className=className;return node;}
function note(text){return el('p',text,'research-note');}
function table(headers,rows,caption){
  const t=el('table',undefined,'accuracy-table');t.append(el('caption',caption));
  const head=el('thead'),heading=el('tr');
  for(const text of headers){const cell=el('th',text);cell.scope='col';heading.append(cell);}head.append(heading);t.append(head);
  const body=el('tbody');
  for(const values of rows){const r=el('tr');values.forEach((v,i)=>{const cell=el(i?'td':'th',v);if(!i)cell.scope='row';r.append(cell);});body.append(r);}
  t.append(body);const scroll=el('div',undefined,'table-scroll');scroll.append(t);return scroll;
}

function renderDashboard(target,view){
  const c=view.counts;
  target.replaceChildren(el('h4','Нейросеть · проверка будущих матчей АПЛ'));
  target.append(note('Сохранено '+c.registered+' · завершено '+c.settled+' · ожидают '+c.pending+
    ' · требуют проверки '+c.review+' · недоступны '+c.unavailable+'.'));
  if(!c.settled)target.append(el('p','Ждём результаты. Точность новой серии пока неизвестна.','quality-empty'));
  target.append(table(['Модель','Log Loss ↓','Brier ↓','Совпал исход'],methods.map(([key,label])=>{
    const m=view.metrics[key],a=m.uniqueTopAccuracy;
    return [label,decimal(m.logLoss),decimal(m.brier),a===null?'—':
      (a*100).toFixed(1).replace('.',',')+'% ('+Math.round(a*m.topEligible)+'/'+m.topEligible+')'];
  }),'Одни и те же завершённые матчи для всех трёх моделей.'));
  target.append(note('Log Loss и Brier: меньше — лучше. Brier — сумма трёх квадратов ошибок, шкала 0–2. '+
    '«Прежняя» — v1 на той же истории CSV; при нехватке истории используются прошлые частоты лиги. Таких завершённых игр: '+view.v1FallbackSettled+'.'));
  const ties=methods.filter(([key])=>view.metrics[key].topTies).map(([key,label])=>label+': '+view.metrics[key].topTies);
  if(ties.length)target.append(note('Равные максимумы исключены только из доли совпадений ('+ties.join('; ')+'). В Log Loss и Brier остаются все завершённые игры.'));
  target.append(note('Эта серия пересекается с прежними проверками АПЛ. Её нельзя считать дополнительными независимыми матчами.'));
  const list=el('details',undefined,'quality-help');list.id='trainerAgentsFixtures';
  list.append(el('summary','Матчи серии · '+view.rows.length));
  for(const row of view.rows){
    const item=el('article',undefined,'quality-fixture');item.dataset.fixtureId=row.fixtureId;
    item.append(el('h4',row.home+' — '+row.away),note(date(row.kickoff)+' · '+states[row.status]));
    item.append(table(['Модель','П1','Х','П2'],methods.map(([key,label])=>[label,...probabilityPercentages(row[key])]),
      'Исходные вероятности; сумма после округления до 0,1% — 100%.'));
    item.append(note('Сохранено '+date(row.registeredAt)+'. Версия: '+row.modelVersion+'.'));
    if(!row.v1_available)item.append(note('Для этого матча прежняя вероятность заменена частотами из-за нехватки истории.'));
    if(row.status==='settled')item.append(note('Итог основного времени: '+row.score.home+':'+row.score.away+'.'));
    const link=el('a','Исходный прогноз JSON');link.href=ARCHIVE+'forecasts/'+row.fixtureId+'.json';
    link.target='_blank';link.rel='noopener noreferrer';item.append(link);list.append(item);
  }
  target.append(list);
  if(c.registered>view.rows.length)target.append(note('Показаны последние '+view.rows.length+' из '+c.registered+
    ' матчей по времени начала. Метрики учитывают всю серию.'));
  const download=el('button','Скачать проверку нейросети');download.id='downloadTrainerAgents';download.type='button';
  download.addEventListener('click',()=>{
    const url=URL.createObjectURL(new Blob([JSON.stringify(view,null,2)],{type:'application/json'})),link=el('a');
    link.href=url;link.download='footpredict-neural-series.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });target.append(download);
}

export function describeTrainerAgents(packet,now=Date.now()){
  const attempt=packet?.latestAttempt,success=packet?.latestSuccessful;
  if(packet?.schemaVersion!==1||packet.protocol!=='trainer-agents-1'||!attempt||
    !['success','failed'].includes(attempt.status)||attempt.promotedToDefault!==false||
    attempt.policy?.automaticPromotion!==false||attempt.policy?.paidAIRequests!==0||
    !Number.isFinite(Date.parse(attempt.checkedAt)))throw new Error('Invalid trainer agent report');
  const lines=['Автотренеры · проверка каждые 6 часов',
    'Последняя проверка: '+date(attempt.checkedAt)+'.'];
  if(now-Date.parse(attempt.checkedAt)>12*3600000)lines.push('Проверка старше 12 часов. Состояние запуска доступно в журнале GitHub.');
  if(attempt.status==='failed')lines.push('Последний цикл завершился ошибкой. Предыдущие веса и прогнозы сохранены.');
  if(!success){lines.push('Первый успешный цикл ещё не опубликован.');return lines.join('\n');}
  const trainer=success.roles?.trainer,observer=success.prospective;
  if(success.status!=='success'||success.protocol!=='trainer-agents-1'||success.promotedToDefault!==false||
    success.roles?.examiner?.publicPromotionAllowed!==false||trainer?.parameterCount!==963||
    ![0,2].includes(trainer.fits)||!Number.isFinite(Date.parse(success.trainedAt))||
    !['registered','settled','pending','review','unavailable'].every(k=>Number.isSafeInteger(observer?.[k])&&observer[k]>=0)||
    observer.registered!==observer.settled+observer.pending+observer.review+observer.unavailable)throw new Error('Incomplete trainer state');
  lines.push('Нейросеть: '+success.modelVersion+'.',
    'Последнее обучение: '+date(success.trainedAt)+'; примеров: '+success.trainingMatches+'.');
  if(trainer.status==='unchanged')lines.push('Данные и рецепт прежние; повторная подгонка не требовалась.');
  if(trainer.converged===false&&trainer.stopReason==='iteration_limit')lines.push('Обучение остановлено по заданному лимиту итераций; сходимость не заявляется.');
  lines.push('Будущая проверка АПЛ: сохранено '+observer.registered+', ожидают '+observer.pending+', завершено '+observer.settled+'.',
    'Контролёр оставляет сеть исследовательским кандидатом. Основная модель автоматически не заменяется.');
  if(observer.review+observer.unavailable)lines.push('Требуют проверки или недоступны: '+(observer.review+observer.unavailable)+'.');
  return lines.join('\n');
}

  async function read(url){
    const response=await fetch(url,{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(20000)});
    if(!response.ok)throw new Error('Report unavailable');
    const raw=await response.text();if(raw.length>500000)throw new Error('Report too large');return raw;
  }

let pendingReport=null,lastReport=null,readAt=0;
export function loadTrainerReport(refresh=false){
  if(pendingReport)return pendingReport;
  if(!refresh&&lastReport&&Date.now()-readAt<300000)return Promise.resolve(lastReport);
  pendingReport=read(REPORT+'?t='+Date.now()).then(raw=>{
    const packet=JSON.parse(raw);describeTrainerAgents(packet);
    lastReport=packet;readAt=Date.now();return packet;
  }).finally(()=>{pendingReport=null;});
  return pendingReport;
}
if(typeof document!=='undefined'){
  let loaded=false,requestId=0,cached=null,cachedDigest=null;
  async function refresh(){
    const token=++requestId,target=document.getElementById('trainerAgentsBody'),status=document.getElementById('trainerAgentsViewStatus');
    status.hidden=false;status.textContent='Обновляю проверку нейросети…';
    try{
      const packet=await loadTrainerReport(true),message=describeTrainerAgents(packet);
      if(token!==requestId)return;target.textContent=message;
      const success=packet.latestSuccessful,digest=success?.prospectiveDashboardSha256;
      if(!digest){
        status.textContent=success?'Список матчей появится после следующего успешного цикла автотренеров.':'Первый успешный цикл ещё не опубликован.';
        loaded=true;return;
      }
      if(!/^[0-9a-f]{64}$/.test(digest))throw new Error('Invalid neural display digest');
      const view=digest===cachedDigest?validateTrainerDashboard(cached,success):
        await verifyTrainerDashboard(await read(ARCHIVE+'dashboards/'+digest+'.json'),digest,success);
      if(token!==requestId)return;
      cached=view;cachedDigest=digest;renderDashboard(document.getElementById('trainerAgentsComparison'),view);
      status.hidden=true;loaded=true;
    }catch{
      if(token!==requestId)return;
      status.textContent='Не удалось обновить проверку нейросети. '+(cached?'Ниже сохранена предыдущая успешная загрузка.':'Попробуй обновить отчёт позже.');
    }
  }
  const parent=document.getElementById('trainingStatus');
  parent.addEventListener('toggle',()=>{if(parent.open&&!loaded)refresh();});
  document.getElementById('refreshTraining').addEventListener('click',refresh);
}
