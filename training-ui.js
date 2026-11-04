const REPORT='https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/training-reports/latest.json';
const $=id=>document.getElementById(id);
let loaded=false,latest=null;
const el=(tag,text,cls)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=String(text);if(cls)node.className=cls;return node;};
const note=text=>el('p',text,'research-note');
const number=value=>typeof value==='number'&&Number.isFinite(value)?value.toFixed(3).replace('.',','):'—';
const date=value=>Number.isFinite(Date.parse(value))?new Date(value).toLocaleString('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Moscow'})+' МСК':'—';
function link(text,address){const a=el('a',text);try{const url=new URL(address);if(url.protocol==='https:'&&url.hostname==='github.com'&&url.pathname.startsWith('/FoXsYsOfficial1909/Footpredict/actions/runs/')){a.href=url.href;a.target='_blank';a.rel='noopener noreferrer';}}catch{}return a;}

function render(data){
  if(data.schemaVersion!==1||!data.latestAttempt||!['success','failed'].includes(data.latestAttempt.status))throw new Error('Invalid report');
  const attempt=data.latestAttempt,success=data.latestSuccessful,target=$('trainingBody');target.replaceChildren();
  $('trainingMessage').textContent=attempt.status==='success'?'Последний цикл обучения завершён.':'Последний цикл завершился ошибкой. Предыдущий успешный результат сохранён.';
  $('trainingMessage').classList.toggle('danger',attempt.status!=='success');
  target.append(note('Последняя попытка: '+date(attempt.generatedAt)+'. Плановый запуск — каждую ночь около 03:23 МСК; возможна задержка расписания.'));
  if(Date.now()-Date.parse(attempt.generatedAt)>36*3600000)target.append(note('Отчёт старше 36 часов. Проверь состояние последнего запуска.'));
  target.append(link('Открыть последний запуск',attempt.runUrl));
  if(!success){target.append(note('Успешный кандидат пока не опубликован.'));return;}
  target.append(el('h3','Кандидат на '+success.asOf),note('Версия: '+success.modelVersion),
    note('Использовано '+success.trainingMatches+' матчей. Последний матч в обучении: '+success.latestTrainingMatch+'.'),
    note('Обучение и проверка: '+number(success.computeSeconds??success.durationSeconds)+' с; цикл с подготовкой данных: '+number(success.durationSeconds)+' с.'));
  const evaluation=success.evaluation;
  if(evaluation?.metrics){
    const scroll=el('div',undefined,'table-scroll'),table=el('table',undefined,'accuracy-table');
    table.append(el('caption','Исторический мониторинг · '+evaluation.commonMatches+' общих матчей.'));
    const header=el('tr');for(const text of ['Оценка','Кандидат','v1','База']){const th=el('th',text);th.scope='col';header.append(th);}
    const head=el('thead');head.append(header);table.append(head);const body=el('tbody');
    for(const [key,label] of [['brier','Brier ↓'],['logloss','Log Loss ↓'],['accuracy','Совпал исход']]){
      const row=el('tr'),th=el('th',label);th.scope='row';row.append(th);
      for(const name of ['candidate','v1','base']){const value=evaluation.metrics[name]?.[key];row.append(el('td',key==='accuracy'&&typeof value==='number'?(value*100).toFixed(1).replace('.',',')+'%':number(value)));}
      body.append(row);
    }
    table.append(body);scroll.append(table);target.append(scroll);
  }
  target.append(note('Эти показатели оценивают процедуру обучения на прошлых матчах. Для финальных весов кандидата ещё нужна проверка на будущих играх.'),
    note('Кандидат не заменяет действующую модель. Новости и травмы пока не входят в его вероятности.'),link('Отчёт и файлы кандидата',success.artifactsUrl));
}

async function refresh(){
  $('refreshTraining').disabled=true;$('trainingMessage').textContent='Загружаю отчёт обучения…';
  try{
    const response=await fetch(REPORT+'?t='+Date.now(),{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(20000)});
    if(response.status===404){$('trainingMessage').textContent='Первый отчёт обучения ещё не опубликован.';loaded=false;return;}
    if(!response.ok)throw new Error('HTTP');
    const raw=await response.text();if(raw.length>200000)throw new Error('Report too large');
    const data=JSON.parse(raw);render(data);latest=data;loaded=true;$('downloadTraining').disabled=false;
  }catch{$('trainingMessage').textContent='Не удалось обновить отчёт обучения.'+(latest?' Показаны предыдущие данные.':' Попробуй позже.');loaded=false;}
  finally{$('refreshTraining').disabled=false;}
}
$('trainingStatus').addEventListener('toggle',()=>{if($('trainingStatus').open&&!loaded)refresh();});
$('refreshTraining').addEventListener('click',refresh);
$('downloadTraining').addEventListener('click',()=>{
  if(!latest)return;const url=URL.createObjectURL(new Blob([JSON.stringify(latest,null,2)],{type:'application/json'})),a=el('a');
  a.href=url;a.download='footpredict-training-report.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
