const API='https://footpredict-api.wzjp5747md.workers.dev';
const $=id=>document.getElementById(id);
const percent=value=>typeof value==='number'?(value*100).toFixed(1).replace('.',',')+'%':'—';
const decimal=value=>typeof value==='number'?value.toFixed(3).replace('.',','):'—';
const date=value=>value&&Number.isFinite(Date.parse(value))?new Date(value).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})+' МСК':'—';
const labels=['П1','Х','П2'];
const reasons={fixture_mismatch:'Не совпадают команды, лига или время матча',result_requires_review:'Результат требует проверки',
  invalid_record:'Неполная или неподтверждённая запись',v1_less_than_24h:'v1 сохранён менее чем за 24 часа',
  missing_base:'Нет сохранённой простой базы',score_disagreement:'Источники истории вернули разные счета',
  invalid_probabilities:'Некорректные вероятности',invalid_identity:'Не подтверждены команды',invalid_prediction_time:'Не подтверждено время сохранения',
  unverified_prediction:'Не подтверждён серверный прогноз',unverified_result:'Не подтверждён итог основного времени',changed_kickoff:'Изменилось время матча'};
let report=null,shown=20;

function el(tag,text,className){const element=document.createElement(tag);if(text!==undefined)element.textContent=text;if(className)element.className=className;return element;}
function note(text){return el('p',text,'research-note');}

function metricTable(target,models,caption,historical=false){
  const table=el('table',undefined,'accuracy-table');table.append(el('caption',caption));
  const header=el('tr');
  for(const text of ['Показатель',...models.map(model=>model.label)]){const cell=el('th',text);cell.scope='col';header.append(cell);}
  const head=el('thead');head.append(header);table.append(head);
  const body=el('tbody');
  for(const [key,label] of [['n','Матчей'],['brier','Brier ↓'],['logLoss','Log Loss ↓'],['accuracy','Совпал исход']]){
    const row=el('tr'),heading=el('th',label);heading.scope='row';row.append(heading);
    for(const model of models){
      const metrics=model.metrics,value=metrics?.[historical&&key==='logLoss'?'logloss':key];
      row.append(el('td',key==='n'?String(value??0):key==='accuracy'?percent(value):decimal(value)));
    }
    body.append(row);
  }
  table.append(body);
  const scroll=el('div',undefined,'table-scroll');scroll.append(table);target.append(scroll);
}

function renderCohorts(){
  const target=$('qualityCohorts');target.replaceChildren();
  for(const [key,title] of [['v1','Основная v1'],['v2','Экспериментальная v2']]){
    const c=report.cohorts[key],m=c.metrics,card=el('article',undefined,'quality-model');card.dataset.model=key;
    card.append(el('h3',title),el('p',m.n?percent(m.accuracy):'Ждём результаты','quality-number'));
    card.append(note(m.topEligible?'Совпал наиболее вероятный исход: '+m.correct+' из '+m.topEligible+'.':m.n?'Нет прогнозов с единственным наиболее вероятным исходом.':'Завершённых матчей для оценки пока нет.'));
    const list=el('dl',undefined,'quality-facts');
    for(const [label,value] of [['Сохранено',c.total],['В оценке',m.n],['Ожидают',c.pending],['Проверка / исключены',c.review+c.excluded]]){
      const item=el('div');item.append(el('dt',label),el('dd',String(value)));list.append(item);
    }
    card.append(list,note('Brier '+decimal(m.brier)+' · Log Loss '+decimal(m.logLoss)+'.'));
    if(m.ties)card.append(note('Равный максимум у '+m.ties+' прогнозов: учтены в ошибках вероятностей, исключены из доли совпадений.'));
    card.append(note('Лиги: '+c.leagues.join(', ')+'. '+(key==='v1'?'Весь серверный архив этой версии.':'Первые сохранённые прогнозы минимум за 24 часа.')));
    target.append(card);
  }
}

function renderPaired(){
  const p=report.paired,target=$('qualityPaired');target.replaceChildren(el('h3','Сравнение на общих матчах'));
  target.append(note('Общих матчей: '+p.common+' · в оценке: '+p.models.v1.n+' · ожидают: '+p.pending+' · проверка / исключены: '+(p.review+p.excluded)+'.'));
  if(!p.models.v1.n)target.append(el('p','Пока рано выбирать победителя: нет общих подтверждённых результатов.','quality-empty'));
  metricTable(target,[{label:'v1',metrics:p.models.v1},{label:'v2',metrics:p.models.v2},{label:'База',metrics:p.models.base}],
    'Одинаковые матчи и счета, оба прогноза сохранены минимум за 24 часа.');
  const ties=Object.entries(p.models).filter(([,m])=>m.ties).map(([key,m])=>key+': '+m.ties).join(', ');
  if(ties)target.append(note('Равные максимумы исключены из доли совпадений отдельно у каждой модели ('+ties+'). В Brier и Log Loss остаются все общие матчи.'));
  if(p.models.v1.n){
    const diff=p.models.v2.brier-p.models.v1.brier;
    target.append(note('Разность Brier v2 − v1: '+(diff>0?'+':'')+decimal(diff)+'. Отрицательная — меньшая ошибка v2. Малая выборка не доказывает устойчивого преимущества.'));
  }
  for(const [reason,count] of Object.entries(p.reasons))target.append(note((reasons[reason]||'Нужна проверка')+': '+count+'.'));
  target.append(note('База — сохранённые частоты исходов лиги из v1. Время сохранения двух моделей может различаться. Новости пока не меняют вероятности v2.'));
}

function renderProspective(nightly=false){
  const target=$(nightly?'qualityNightly':'qualityProspective'),data=nightly?report.nightly:report.prospective;
  target.replaceChildren(el('h3',nightly?'Ночной кандидат · будущие матчи':'Совместная проверка v1 и v2'));
  if(!data){target.append(note('Данные новой серии пока недоступны. Попробуй обновить позже.'));return;}
  const names=nightly?{candidate:'Кандидат',v1:'v1',v2:'v2',base:'База'}:{v1:'v1',v2:'v2',base:'База'};
  const keys=Object.keys(names),endpoint=nightly?'candidate-comparison':'comparison';
  const t=data.totals;
  target.append(note('Зафиксировано матчей: '+t.registered+' · с результатом: '+t.settled+' · ожидают: '+t.pending+' · проверка / исключены: '+(t.review+t.excluded)+'.'));
  target.append(note(nightly?'Ночной кандидат, v1, текущая v2 и база сохраняются одновременно минимум за сутки до матча. У всех общий набор результатов.':
    'v1, v2 и простая база сохраняются одновременно минимум за сутки до матча. Все три оцениваются по одинаковым результатам.'));
  metricTable(target,keys.map(key=>({label:names[key],metrics:data.models?.[key]})),
    nightly?'Общая выборка для четырёх моделей.':'Новая серия · общая выборка для трёх моделей.');
  if(nightly)target.append(note('Для каждой игры берётся последний проверенный кандидат не старше 36 часов. Версия и исходные веса фиксируются вместе с прогнозом. Общая таблица оценивает процедуру ночного обновления.'));
  if(!t.settled)target.append(note(t.registered?'Ждём завершения матчей новой серии.':'Первые совместные прогнозы появятся после очередного автоматического сбора. Прошедшие матчи в эту серию не добавляются.'));
  const ties=Object.entries(data.models||{}).filter(([,m])=>m.ties).map(([key,m])=>(names[key]||key)+': '+m.ties).join(', ');
  if(ties)target.append(note('Равные максимумы исключены только из доли совпадений: '+ties+'.'));
  if(data.fixtures?.length){
    const details=el('details',undefined,'quality-help');details.append(el('summary','Матчи совместной серии'));
    for(const fixture of data.fixtures){
      const item=el('article',undefined,'automation-forecast');item.append(el('h4',fixture.home+' — '+fixture.away),note(date(fixture.kickoff)+' · сохранено '+date(fixture.savedAt)));
      if(nightly){
        const candidate=fixture.models?.candidate;
        item.append(note('Кандидат: '+(candidate?.modelVersion||'версия не подтверждена')+'. Обучение: '+date(candidate?.trainingGeneratedAt)+'.'));
        item.append(note('Данные строго до '+(candidate?.trainingCutoffExclusive||'—')+' UTC; последняя игра '+(candidate?.latestTrainingMatch||'—')+'.'));
      }
      for(const key of keys){
        const probabilities=fixture.models?.[key]?.probabilities;
        item.append(note(names[key]+': '+(Array.isArray(probabilities)&&probabilities.length===3&&probabilities.every(p=>typeof p==='number'&&Number.isFinite(p))?
          probabilities.map((p,i)=>labels[i]+' '+percent(p)).join(' · '):'неполная запись, исключена из сравнения')));
      }
      item.append(note(fixture.state==='settled'?'Итог: '+fixture.latest.score.home+':'+fixture.latest.score.away:fixture.state==='pending'?'Ожидается результат':'Итог требует проверки.'));
      const button=el('button','Скачать исходный расчёт');button.type='button';
      button.addEventListener('click',async()=>{
        button.disabled=true;
        try{if(!Number.isSafeInteger(fixture.fixtureId)||fixture.fixtureId<1)throw new Error('Invalid fixture');
          const response=await fetch(API+'/v1/'+endpoint+'/'+fixture.fixtureId+'/evidence',{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(20000)});
          if(!response.ok)throw new Error('HTTP');const packet=await response.json();
          const url=URL.createObjectURL(new Blob([JSON.stringify(packet,null,2)],{type:'application/json'})),link=el('a');
          link.href=url;link.download='footpredict-'+endpoint+'-'+fixture.fixtureId+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
        }catch{item.append(note('Не удалось скачать исходный расчёт. Попробуй позже.'));}finally{button.disabled=false;}
      });item.append(button);details.append(item);
    }
    target.append(details);
    if(data.fixtureScope?.total>data.fixtureScope.returned)target.append(note('В списке последние '+data.fixtureScope.returned+' матчей из '+data.fixtureScope.total+'. Сводные метрики учитывают всю серию.'));
  }
  if(nightly&&data.versions?.length){
    const details=el('details',undefined,'quality-help');details.append(el('summary','Результаты по версиям кандидата · '+data.versions.length));
    for(const version of data.versions.slice().sort((a,b)=>String(b.trainingGeneratedAt).localeCompare(String(a.trainingGeneratedAt))).slice(0,20)){
      const item=el('article',undefined,'automation-forecast');item.append(el('h4',version.modelVersion||'Неполная версия'));
      item.append(note('Зафиксировано '+version.totals.registered+' · с результатом '+version.totals.settled+' · ожидают '+version.totals.pending+'.'));
      metricTable(item,keys.map(key=>({label:names[key],metrics:version.models?.[key]})),'Одни и те же матчи этой версии кандидата.');
      details.append(item);
    }
    if(data.versions.length>20)details.append(note('Здесь последние 20 версий. Полный список есть в JSON-отчёте.'));
    target.append(details);
  }
  target.append(note(nightly?'Кандидат остаётся экспериментальным. Для оценки нужны будущие результаты; новости, травмы и составы пока не входят в вероятности.':
    'v2 использует свои зафиксированные параметры обучения. Новости не меняют вероятности; модель автоматически основной не становится.'));
}

function renderHistorical(){
  const b=report.historical,target=$('qualityHistorical');target.replaceChildren(el('h3','Исторический эксперимент · '+b.period));
  target.append(note('Общая выборка: '+b.common_fixtures+' из 380 матчей. 11 игр с недостаточной историей для v1 исключены у всех моделей.'));
  metricTable(target,[{label:'v1',metrics:b.v1_metrics},{label:'v2',metrics:b.metrics},{label:'База',metrics:b.base_metrics}],
    'Переобучение по ходу исторического периода; это отдельный эксперимент.',true);
  target.append(note('На этом периоде v2 уменьшила ошибки вероятностей, но реже угадывала самый вероятный исход, чем v1. Поэтому одной «точностью» качество не описать.'));
  target.append(note('Настройки выбраны на сезонах 2023/24–2024/25. Период 2025/26 мог изучаться ранее; нужна проверка на новых матчах. Травмы, новости и составы в расчёт не входят. v2 остаётся экспериментальной.'));
  const link=el('a','Протокол и ограничения');link.href='./docs/model-card.md';target.append(link);
}

function modelDetail(record,key){
  const card=el('div',undefined,'quality-pick');card.append(el('h4',key));
  if(!record){card.append(note('Прогноз не сохранялся.'));return card;}
  if(record.probabilities){
    const max=Math.max(...record.probabilities),tops=record.probabilities.map((p,i)=>Math.abs(p-max)<=1e-12?labels[i]:null).filter(Boolean);
    card.append(el('p',tops.join(' / ')+' · '+percent(max),'quality-pick-top'));
    card.append(note(record.probabilities.map((p,i)=>labels[i]+' '+percent(p)).join(' · ')));
  }
  if(record.metrics){
    const m=record.metrics;
    card.append(el('p',m.correct===null?'Равный максимум':m.correct?'Исход совпал':'Исход не совпал',m.correct===false?'quality-miss':'quality-hit'));
    card.append(note('Факт: '+record.score.home+':'+record.score.away+' ('+labels[m.actual]+') · вероятность факта '+percent(m.actualProbability)+'.'));
    card.append(note('Brier '+decimal(m.brier)+' · Log Loss '+decimal(m.logLoss)+'.'));
  }else card.append(note(record.state==='pending'?'Результат ожидается.':reasons[record.reason]||'Результат требует проверки.'));
  card.append(note('Сохранён '+date(record.savedAt)+'.'));
  return card;
}

function renderFixtures(reset=false){
  if(!report)return;
  if(reset)shown=20;
  const filter=$('qualityFilter').value,model=$('qualityModelFilter').value;
  const records=fixture=>model==='all'?[fixture.v1,fixture.v2].filter(Boolean):[fixture[model]].filter(Boolean);
  let list=report.fixtures.filter(fixture=>{
    const rows=records(fixture);
    return rows.length&&(filter==='all'||filter==='errors'&&rows.some(r=>r.metrics?.correct===false)||
      filter==='settled'&&rows.some(r=>r.metrics)||filter==='pending'&&rows.some(r=>r.state==='pending')||
      filter==='review'&&(rows.some(r=>['review','excluded'].includes(r.state))||model==='all'&&['review','excluded'].includes(fixture.pairState)));
  });
  if(filter==='errors')list=list.sort((a,b)=>Math.max(...records(b).map(r=>r.metrics?.correct===false?r.metrics.topProbability:0))-Math.max(...records(a).map(r=>r.metrics?.correct===false?r.metrics.topProbability:0)));
  const target=$('qualityFixtures');target.replaceChildren();
  for(const fixture of list.slice(0,shown)){
    const card=el('details',undefined,'quality-fixture'),summary=el('summary');
    summary.append(el('span',fixture.home+' — '+fixture.away,'quality-fixture-name'));
    summary.append(el('span',records(fixture).map(r=>r.key+': '+(r.metrics?r.metrics.correct===false?'ошибка · '+percent(r.metrics.topProbability):r.metrics.correct?'исход совпал':'равный максимум':r.state==='pending'?'ожидает':'проверка')).join(' · '),'quality-summary-state'));
    card.append(summary,note(date(fixture.kickoff)+' · '+fixture.league));
    const grid=el('div',undefined,'quality-picks');
    for(const key of model==='all'?['v1','v2']:[model])grid.append(modelDetail(fixture[key],key));
    card.append(grid);
    if(['review','excluded'].includes(fixture.pairState)&&model==='all')card.append(note('Общее сравнение: '+(reasons[fixture.pairReason]||'Нужна проверка')+'.'));
    target.append(card);
  }
  if(!list.length)target.append(note(filter==='errors'?'В выбранной части архива нет ошибок наиболее вероятного исхода. Ожидающие и равные максимумы сюда не попадают.':'Под этот фильтр матчи пока не попали.'));
  $('qualityFixtureCount').textContent='Показано '+Math.min(shown,list.length)+' из '+list.length+' подходящих матчей.'+(filter==='errors'?' Сначала ошибки с большей уверенностью модели.':'')+
    (report.fixtureScope.total>report.fixtureScope.returned?' Список ограничен последними '+report.fixtureScope.returned+' матчами из '+report.fixtureScope.total+'; сводки выше учитывают весь архив.':'');
  $('moreQualityFixtures').hidden=list.length<=shown;
}

function view(){
  const historical=$('qualityView').value==='historical';
  $('qualityLive').hidden=historical;$('qualityHistorical').hidden=!historical;
}

function renderOverview(){
  const c=report.cohorts.v1,m=c.metrics,target=$('qualityOverview');target.replaceChildren();
  for(const [label,value] of [['Завершено',String(m.n)],['Верных исходов',String(m.correct??0)],['Точность',m.topEligible?percent(m.accuracy):'—']]){
    const item=el('div',undefined,'overview-stat');
    item.append(el('span',label,'overview-label'),el('strong',value,'overview-value'));target.append(item);
  }
  target.append(note(m.topEligible?'Доля совпадений среди '+m.topEligible+' прогнозов с однозначным наиболее вероятным исходом. Небольшая выборка не гарантирует такую же точность дальше.':'Ждём подтверждённые результаты. Ожидающие матчи не считаются ошибками.'));
  if(c.leagues?.length>1)target.append(note('Общая история: '+c.leagues.map(league=>({PL:'АПЛ',BL1:'Бундеслига'}[league]||league)).join(', ')+'.'));
  if(c.pending)target.append(note('Ожидают результата: '+c.pending+'.'));
}

async function refresh(){
  $('refreshQualityPublic').disabled=true;
  const button=$('refreshQuality');button.disabled=true;
  $('qualityStatus').textContent='Загружаю сохранённые прогнозы и последние результаты…';
  $('qualityStatus').classList.remove('danger');
  try{
    const response=await fetch(API+'/v1/quality',{cache:'no-store',credentials:'omit'});
    if(!response.ok)throw new Error('HTTP '+response.status);
    const data=await response.json();
    if(data.schemaVersion!==1||!data.cohorts?.v1||!data.cohorts?.v2||!data.paired||!Array.isArray(data.fixtures))throw new Error('Incomplete report');
    report=data;renderOverview();renderProspective(true);renderProspective();renderCohorts();renderPaired();renderHistorical();renderFixtures(true);view();
    $('qualityResult').hidden=false;$('downloadQuality').disabled=false;
    const pending=data.fixtures.filter(f=>f.v2?.state==='pending').sort((a,b)=>Date.parse(a.kickoff)-Date.parse(b.kickoff));
    $('qualityNextMatch').textContent=pending.length?'Ближайшая игра с ожидающим прогнозом v2: '+pending[0].home+' — '+pending[0].away+', '+date(pending[0].kickoff)+'. Сверка доступна через 4 часа после начала.':'';
    $('qualityStatus').textContent='Обновлено: '+date(data.generatedAt)+'.';
  }catch{
    $('qualityStatus').textContent='Не удалось обновить качество прогнозов.'+(report?' Показаны предыдущие данные от '+date(report.generatedAt)+'.':' Попробуй обновить позже; отсутствие ответа не означает нулевую точность.');
    $('qualityStatus').classList.add('danger');
  }finally{button.disabled=false;$('refreshQualityPublic').disabled=false;}
}

$('qualityView').addEventListener('change',view);
for(const id of ['qualityFilter','qualityModelFilter'])$(id).addEventListener('change',()=>renderFixtures(true));
$('moreQualityFixtures').addEventListener('click',()=>{shown+=20;renderFixtures();});
$('refreshQuality').addEventListener('click',refresh);
$('refreshQualityPublic').addEventListener('click',refresh);
$('downloadQuality').addEventListener('click',()=>{
  if(!report)return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'})),link=el('a');
  link.href=url;link.download='footpredict-quality-'+report.generatedAt.slice(0,10)+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
let opened=false;
function open(){if(opened)return;opened=true;refresh();}
window.addEventListener('footpredict:tab-selected',event=>{if(event.detail?.tab==='quality')open();});
if(document.getElementById('tab-quality')?.getAttribute('aria-selected')==='true')open();
