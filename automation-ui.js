const API='https://footpredict-api.wzjp5747md.workers.dev';
const $=id=>document.getElementById(id);
let report=null,cursor=null;

function el(tag,text,className){const item=document.createElement(tag);if(text!==undefined)item.textContent=String(text);if(className)item.className=className;return item;}
function date(value){const d=new Date(value);return Number.isFinite(d.getTime())?d.toLocaleString('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Moscow'})+' МСК':'ещё не получены';}
async function get(path){const response=await fetch(API+path,{cache:'no-store',signal:AbortSignal.timeout(25000)});if(!response.ok)throw new Error('Не удалось получить состояние сервиса.');return response.json();}
function fact(label,value){const item=el('div');item.append(el('dt',label),el('dd',value));return item;}

function renderStatus(data){
  const labels={running:'Сбор выполняется по расписанию.',delayed:'Последний запуск задерживается. Сохранённые сведения доступны.',recovering:'Предыдущий запуск прервался. Задачи ожидают следующего прохода.',not_started:'Ожидается первый запуск расписания.'};
  $('automationStatus').textContent=labels[data.state]||'Состояние сбора неизвестно.';
  $('automationStatus').classList.toggle('danger',['delayed','recovering'].includes(data.state));
  const list=el('dl',undefined,'automation-stats');
  const feeds=(data.jobs||[]).filter(job=>job.kind==='news'),successful=feeds.filter(job=>job.last_success_at&&Date.now()-Date.parse(job.last_success_at)<3*3600000);
  list.append(fact('Последний проход',date(data.heartbeat?.finishedAt||data.heartbeat?.startedAt)),
    fact('Ленты проверены за 3 часа',successful.length+' / '+(data.league?.clubs||feeds.length||'—')),
    fact('Сохранённых публикаций',data.news?.total??0),fact('Прогнозов v2 до матча',data.forecasts?.total??0));
  $('automationOverview').replaceChildren(list);
  const issues=new Set();
  if(data.heartbeat?.reportError)issues.add('Ежедневный отчёт пока не сформирован. Попытка повторится при следующем запуске.');
  for(const job of data.jobs||[]){
    if(job.status==='retry')issues.add(job.kind==='news'?'Одна из новостных лент недоступна. Повтор запланирован.':'Часть сбора отложена после ошибки; повтор запланирован.');
    if(job.result?.deferred)issues.add(job.result.reason==='daily_budget'?'Дневной лимит сбора достигнут. Задачи продолжатся после сброса квоты.':'Часть обновлений отложена из-за лимита запросов.');
    if(job.result?.aiStatus==='budget_reached')issues.add('Дневной лимит ИИ-разбора достигнут; статистика и новости сохраняются.');
    if(['unavailable','rejected'].includes(job.result?.aiStatus))issues.add('Часть ответов ИИ не получена или не прошла проверку.');
    for(const error of job.result?.sourceErrors||[]){
      if(error.reason==='subscription_restriction'||/free plans/i.test(error.detail||''))issues.add('Тариф источника травм не открывает текущий сезон. Статус игроков остаётся неизвестным.');
      else if(error.source==='API-Football injuries')issues.add('Источник травм временно недоступен. Статус игроков остаётся неизвестным.');
    }
  }
  const modelAge=(Date.now()-Date.parse(data.model?.asOf+'T00:00:00Z'))/86400000;
  if(modelAge>7)issues.add('Параметры v2 обучены более недели назад. Автоматическое переобучение пока не включено.');
  $('automationIssues').replaceChildren(...[...issues].map(text=>el('li',text)));
}

function renderReport(data){
  const target=$('automationReportBody');target.replaceChildren();
  if(data.status==='not_generated'){target.append(el('p',data.message,'research-note'));report=null;$('downloadDailyReport').disabled=true;return;}
  report=data;$('downloadDailyReport').disabled=false;
  target.append(el('p','Отчёт за '+data.day+'. Запланирован: '+date(data.scheduledFor)+'. Сформирован: '+date(data.generatedAt)+'.','research-note'));
  if(data.lateBySeconds>300)target.append(el('p','Отчёт сформирован с задержкой. Время выше отражает фактический запуск.','research-note'));
  const list=el('dl',undefined,'automation-stats');
  list.append(fact('Карточек за период',data.cards?.snapshots??0),fact('Матчей с карточками',data.cards?.fixtures??0),
    fact('Новых публикаций',data.news?.newEvidence??0),fact('Завершённых матчей v2',data.experimentalMetrics?.settled??0));target.append(list);
  const metrics=data.experimentalMetrics;
  if(metrics?.settled)target.append(el('p','Brier: '+metrics.brier.toFixed(3)+' · Log Loss: '+metrics.logLoss.toFixed(3)+' · совпал исход: '+(100*metrics.accuracy).toFixed(1)+'%.','research-note'));
  else target.append(el('p','Итогов для оценки v2 пока нет. Исторические метрики доступны в разборе матча.','research-note'));
  const v1=data.originalHistory;
  if(v1)target.append(el('p','Основная история v1: '+v1.total+' прогнозов, '+v1.settled+' с итогом'+(v1.settled?' · совпал исход: '+(100*v1.accuracy).toFixed(1)+'%':'')+'.','research-note'));
  const failures=(data.activity||[]).filter(row=>row.outcome==='failed').reduce((sum,row)=>sum+row.count,0);
  if(failures)target.append(el('p','Неудачных попыток за период: '+failures+'. Состояние повторных попыток показывается выше.','research-note'));
  target.append(el('p','Активность за период: '+date(data.window.from)+' — '+date(data.window.to)+'.','research-note'));
}

function renderForecasts(data,append=false){
  const target=$('automationForecastList');if(!append)target.replaceChildren();
  for(const row of data.items||[]){
    const item=el('article',undefined,'automation-forecast');
    item.append(el('h3',row.metadata.home+' — '+row.metadata.away),el('p','Матч: '+date(row.kickoff)+' · прогноз сохранён: '+date(row.savedAt),'research-note'),
      el('p',row.probabilities.map((p,i)=>['П1','Х','П2'][i]+': '+(p*100).toFixed(1)+'%').join(' · ')));
    const latest=row.latest||{},labels={pending:'Ожидается результат',cancelled:'Матч отменён',postponed:'Матч перенесён',review_required:'Нужна проверка изменений матча'};
    item.append(el('p',latest.status==='settled'?'Итог: '+latest.score.home+':'+latest.score.away+' · Brier '+latest.brier.toFixed(3):labels[latest.status]||'Результат пока не подтверждён','research-note'));
    target.append(item);
  }
  if(!target.children.length)target.append(el('p','Первые прогнозы появятся после сбора карточек.','research-note'));
  cursor=data.nextCursor;$('moreShadowForecasts').hidden=!cursor;
}

async function refresh(){
  const button=$('refreshAutomation');button.disabled=true;
  const results=await Promise.allSettled([get('/v1/automation/status'),get('/v1/automation/report'),get('/v1/automation/forecasts')]);
  if(results[0].status==='fulfilled')renderStatus(results[0].value);
  else{$('automationStatus').textContent='Не удалось проверить состояние сбора. Это не подтверждает остановку сервиса.';$('automationStatus').classList.add('danger');}
  if(results[1].status==='fulfilled')renderReport(results[1].value);
  else $('automationReportBody').replaceChildren(el('p','Отчёт сейчас недоступен. Попробуй обновить статус.','research-note'));
  if(results[2].status==='fulfilled')renderForecasts(results[2].value);
  else $('automationForecastList').replaceChildren(el('p','Список прогнозов сейчас недоступен.','research-note'));
  button.disabled=false;
}

$('refreshAutomation').addEventListener('click',refresh);
$('moreShadowForecasts').addEventListener('click',async()=>{
  const button=$('moreShadowForecasts');button.disabled=true;
  try{renderForecasts(await get('/v1/automation/forecasts?cursor='+encodeURIComponent(cursor)),true);}
  catch{$('automationForecastList').append(el('p','Не удалось загрузить следующую страницу.','research-note'));}
  finally{button.disabled=false;}
});
$('downloadDailyReport').addEventListener('click',()=>{
  if(!report)return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));
  const anchor=el('a');anchor.href=url;anchor.download='footpredict-report-'+report.day+'.json';anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
refresh();
