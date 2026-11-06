const $ = id => document.getElementById(id);
let selected = null, packet = null, context = null, generation = 0, evidenceRevision = 0, controller = null;
const pending = new Set();

function node(tag, text, className) {
  const value = document.createElement(tag);
  if (text !== undefined) value.textContent = String(text);
  if (className) value.className = className;
  return value;
}

function dateText(value) {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString('ru-RU', {dateStyle:'medium', timeStyle:'short', timeZone:'Europe/Moscow'}) + ' МСК' : 'время не указано';
}

function publicationText(fact){
  if(fact.publishedPrecision==='day')return new Date(fact.publishedAt).toLocaleDateString('ru-RU',{dateStyle:'medium',timeZone:'UTC'})+' · время не указано';
  return dateText(fact.publishedAt);
}

function articleLink(text, address) {
  const anchor = node('a', text);
  try {
    const url = new URL(address);
    if (url.protocol === 'https:' && !url.username && !url.password) {
      anchor.href = url.href;anchor.target = '_blank';anchor.rel = 'noopener noreferrer';
    }
  } catch { /* Untrusted source values never become executable URLs. */ }
  return anchor;
}

function status(message, error = false) {
  $('researchStatus').textContent = message;
  $('researchStatus').classList.toggle('danger', error);
}

function reset() {
  $('matchResearch').hidden=true;
  generation++;evidenceRevision++;for(const request of pending)request.abort();pending.clear();controller = null;selected = null;packet = null;context = null;
  $('loadResearch').disabled = true;$('exportResearch').disabled = true;
  $('researchResult').hidden = true;
  $('researchEvidence').hidden=true;$('researchNewsDetails').hidden=true;
  $('researchAi').replaceChildren();$('researchNews').replaceChildren();
  $('researchTeams').replaceChildren();$('researchPlayerSources').replaceChildren();$('researchUpdated').textContent='';
  status('Выбери матч АПЛ — сведения появятся автоматически.');
}

window.addEventListener('footpredict:selection-reset', reset);
window.addEventListener('footpredict:match-selected', event => {
  reset();
  const detail = event.detail;
  if (detail?.league !== 'PL') { status('Разбор пока доступен для матчей АПЛ.');return; }
  if (!Number.isSafeInteger(detail.fixtureId) || detail.fixtureId < 1) return;
  selected = detail;
  $('matchResearch').hidden=false;
  $('loadResearch').disabled = false;
  status('Загружаю форму, сведения об игроках и составы…');
  void loadAutomatic();
});

function coverage(data) {
  const labels = [['form','Форма'],['news','Новости'],['playerStatuses','Статусы игроков FPL'],['availabilityReports','Сообщения о доступности'],['officialNews','Официальные сводки'],['ai','ИИ-разбор'],['injuries','Сводный список травм'],['lineups','Стартовые составы'],['completeCalendar','Полный календарь']];
  $('researchCoverage').replaceChildren(...labels.map(([key,label]) => node('span', (data.coverage?.[key] ? '✓ ' : '○ ') + label, 'coverage-chip' + (data.coverage?.[key] ? ' available' : ''))));
}

function sameTeam(value, team, index) {
  return value?.teams?.find(item => (item.teamId != null && item.teamId === team.id) ||
    item.team === team.name || item.name === team.name) || value?.teams?.find(item=>!item.team && !item.name && item.homeAway===(index===0?'home':'away'));
}

function playerAvailability(team, index) {
  const result=context?.availability || packet?.playerAvailability;
  if(!result)return null;
  const entry=sameTeam(result,team,index),wrapper=node('div',undefined,'player-availability');
  const reports=Array.isArray(entry?.reports)?entry.reports:[];
  const fresh=reports.filter(report=>!report.stale && !report.newsStale);
  const staleCount=reports.length-fresh.length;
  const labels={d:'Под вопросом',i:'Сообщение о травме',s:'Дисквалификация',u:'Недоступен по данным источника',
    doubtful:'Под вопросом',injured:'Сообщение о травме',suspended:'Дисквалификация',unavailable:'Недоступен по данным источника'};
  function reportItem(report,full){
    const name=report.player || report.name || report.playerName || 'Игрок';
    const state=labels[report.providerStatus || report.status] || report.statusLabel || 'Сообщение о доступности';
    const item=node('li');item.append(node('strong',name),node('span',' · '+state));
    if(full){
      const news=report.news || report.reason;
      if(news)item.append(node('p',news,'research-note'));
      if(report.stale || report.newsStale)item.append(node('p','Сообщение давно не обновлялось; актуальность требует проверки.','research-note'));
      const date=report.newsAdded || report.newsAddedAt || report.publishedAt;
      item.append(node('p',date?'Дата сообщения: '+dateText(date):'Дата сообщения не указана.','research-note'));
    }
    return item;
  }
  wrapper.append(node('strong','Состояние игроков'));
  if(fresh.length){
    const list=node('ul');list.style.paddingLeft='18px';
    list.append(...fresh.slice(0,3).map(report=>reportItem(report,false)));wrapper.append(list);
  }else wrapper.append(node('p',reports.length?'Свежих сообщений нет. Ранее полученные сведения — ниже.':
    entry?.status==='no_supported_reports' || entry?.status==='reports_found'
      ?'Источник не вернул сообщений о потерях. Это не подтверждает отсутствие травм.'
      :'Свежие сведения об игроках не получены. Их готовность неизвестна.','research-note'));
  if(reports.length){
    const details=node('details',undefined,'research-form-details');
    details.append(node('summary','Все сообщения ('+reports.length+(staleCount?'; давних: '+staleCount:'')+')'));
    const list=node('ul');list.append(...[...fresh,...reports.filter(report=>report.stale || report.newsStale)].map(report=>reportItem(report,true)));
    details.append(list);wrapper.append(details);
  }
  return wrapper;
}

function renderPlayerSources(){
  const target=$('researchPlayerSources');target.replaceChildren();
  const availability=context?.availability || packet?.playerAvailability;
  const lineups=context?.lineups || packet?.reportedLineups;
  if(availability){
    const source=availability.source || {},line=node('p');
    const label=typeof source==='string'?'Fantasy Premier League':source.name || source.provider || 'Fantasy Premier League';
    line.append(articleLink(label,source.url || availability.url || 'https://fantasy.premierleague.com/'));
    if(availability.fetchedAt)line.append(node('span',' · проверено '+dateText(availability.fetchedAt)));
    else line.append(node('span',' · свежие данные не получены'));
    target.append(line,node('p','Отметки источника не подтверждают пропуск именно этого матча. Полного списка травм может не быть.'));
  }
  if(lineups){
    const line=node('p');line.append(articleLink(lineups.source?.provider || 'ESPN',lineups.source?.url));
    if(lineups.fetchedAt)line.append(node('span',' · проверено '+dateText(lineups.fetchedAt)));
    else line.append(node('span',' · состав запрашивается незадолго до начала матча'));
    target.append(line);
  }
}

function publishedLineup(team,index){
  const result=context?.lineups || packet?.reportedLineups;
  if(!result)return null;
  const entry=sameTeam(result,team,index),wrapper=node('div',undefined,'player-lineup');
  const players=Array.isArray(entry?.players)?entry.players:[];
  if(entry?.status==='reported_by_provider' && players.length===11){
    wrapper.append(node('strong','Стартовый состав по ESPN'));
    wrapper.append(node('p',players.map(player=>player.name).join(', ')));
    if(entry.formation)wrapper.append(node('p','Расстановка: '+entry.formation,'research-note'));
    wrapper.append(node('p','Опубликован поставщиком; отдельное подтверждение клуба не получено.','research-note'));
  }else{
    wrapper.append(node('strong','Стартовый состав'));
    wrapper.append(node('p',entry?.status==='not_announced' || result.status==='not_announced'
      ? 'Пока не получен. Состав обычно появляется незадолго до начала матча.'
      : 'Получить состав не удалось. Стартовые 11 неизвестны.','research-note'));
  }
  return wrapper;
}

function teamCard(team,index) {
  const card = node('section', undefined, 'research-team');
  card.append(node('h3', team.name));
  const strip = node('div', undefined, 'form-strip');
  for (const game of team.form || []) {
    const labels = {W:['В','Победа'],D:['Н','Ничья'],L:['П','Поражение']};
    const label = labels[game.outcome] || ['?','Неизвестно'];
    const mark = node('span',label[0],'form-mark');mark.dataset.outcome = game.outcome;
    mark.title = label[1] + ': ' + game.opponent + ', ' + game.goalsFor + ':' + game.goalsAgainst;
    mark.setAttribute('aria-label', mark.title);strip.append(mark);
  }
  strip.append(node('span',(team.points ?? '—') + ' очк. / ' + (team.form?.length || 0) + ' игр','form-points'));
  card.append(strip,node('p','Последние матчи АПЛ · новые слева.','research-note'));
  const list = node('dl',undefined,'research-facts');
  const fact = (title,text) => list.append(node('dt',title),node('dd',text));
  const average = value => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2).replace('.',',') : '—';
  fact('Дома · забивают / пропускают',average(team.home?.goalsFor) + ' / ' + average(team.home?.goalsAgainst) + ' · игр: ' + (team.home?.games || 0));
  fact('В гостях · забивают / пропускают',average(team.away?.goalsFor) + ' / ' + average(team.away?.goalsAgainst) + ' · игр: ' + (team.away?.games || 0));
  const calendar = team.calendar;
  fact('Интервал в доступном календаре',typeof calendar?.gapDays === 'number' ? calendar.gapDays.toFixed(1).replace('.',',') + ' дн.' + (calendar.basedOnScheduledMatch ? ' после запланированной игры' : ' после последней известной игры') : 'Полный календарь не получен.');
  const availability=playerAvailability(team,index),lineup=publishedLineup(team,index);
  if(!availability){
    const injuries = team.injuries;
    fact('Сведения об игроках',injuries?.reports?.length ? injuries.reports.map(item => item.player + ': ' + item.reason).join('; ') : 'Статус игроков неизвестен.');
  }
  if(!lineup)fact('Стартовый состав',team.lineup?.players?.length ? team.lineup.players.map(player=>player.name).join(', ') : 'Не получен. Стартовые 11 неизвестны.');
  if(availability)card.append(availability);if(lineup)card.append(lineup);
  const details = node('details',undefined,'research-form-details');details.append(node('summary','Статистика и последние матчи'),list);
  if(!calendar?.gapDays && typeof team.leagueKickoffGapDays==='number')details.append(node('p','Интервал после последней известной игры АПЛ: '+team.leagueKickoffGapDays.toFixed(1).replace('.',',')+' дн.','research-note'));
  details.append(node('p','Кубковые игры могут отсутствовать. Интервал в календаре не равен подтверждённому времени отдыха.','research-note'));
  const games = node('ul');
  for(const game of team.form || []) games.append(node('li',dateText(game.kickoff) + ' · ' + game.opponent + ' · ' + game.goalsFor + ':' + game.goalsAgainst + (game.venue === 'home' ? ' дома' : ' в гостях')));
  details.append(games);card.append(details);return card;
}

function renderCandidate(candidate) {
  const target = $('researchCandidate');target.replaceChildren();
  if(candidate?.status !== 'experimental' || !Array.isArray(candidate.probabilities)) {target.hidden = true;return;}
  target.hidden = false;target.append(node('summary','Экспериментальная модель v2 · отдельно от основного прогноза'));
  const probabilities = node('div',undefined,'candidate-probabilities');
  for(const [index,label] of ['Победа хозяев','Ничья','Победа гостей'].entries()) {
    const item = node('div');item.append(node('span',label),node('strong',(100*candidate.probabilities[index]).toFixed(1).replace('.',',')+'%'));probabilities.append(item);
  }
  target.append(probabilities,node('p','Срез модели: ' + candidate.asOf + '. Учитываются прошлые результаты, сила соперников и домашнее преимущество. Новости и травмы пока не меняют эти вероятности.','research-note'));
  if(candidate.unseenTeams?.length)target.append(node('p','Для новых команд используется средняя сила: '+candidate.unseenTeams.join(', '),'research-note'));
  const bench = candidate.benchmark;
  if(bench?.metrics && bench?.base_metrics) {
    const table = node('table',undefined,'accuracy-table');
    const caption = node('caption','Историческая проверка ' + bench.period + ': '+bench.common_fixtures+' общих матчей.');table.append(caption);
    const head = node('thead'), header = node('tr');for(const label of ['Метрика','v2','v1','Простая база'])header.append(node('th',label));head.append(header);table.append(head);
    const body = node('tbody');
    for(const [metric,label] of [['brier','Brier ↓'],['logloss','Log Loss ↓'],['accuracy','Совпал исход']]) {
      const row = node('tr');row.append(node('th',label));
      for(const values of [bench.metrics,bench.v1_metrics,bench.base_metrics])row.append(node('td',typeof values?.[metric] === 'number' ? (metric === 'accuracy' ? (100*values[metric]).toFixed(1)+'%' : values[metric].toFixed(3)) : '—'));
      body.append(row);
    }
    table.append(body);target.append(table,node('p','Настройки выбраны на более ранних сезонах. Этот исторический период мог изучаться ранее; следующая проверка — на новых матчах. Улучшение вероятностей не означает, что доля угаданных исходов выросла.','research-note'));
  }
}

function render(data) {
  if (!data.fixture || data.fixture.id !== selected?.fixtureId || !Array.isArray(data.teams) || data.teams.length !== 2) throw new Error('Сервис вернул карточку другого матча или неполные данные.');
  $('researchResult').hidden = false;
  $('researchMatch').textContent = data.fixture.home+' — '+data.fixture.away+' · '+dateText(data.fixture.kickoff);
  packet=data;
  coverage(data);$('researchTeams').replaceChildren(...(context?.teams || data.teams).map(teamCard));renderCandidate(data.candidate);
  $('researchEvidence').hidden=false;$('researchNewsDetails').hidden=false;renderPlayerSources();
  const ai = $('researchAi');ai.replaceChildren(node('h3','Сведения о доступности игроков'));
  const reports=new Map();
  for(const fact of [...(data.availabilityReports||[]),...(data.ai?.facts||[])])reports.set(fact.url+'\n'+fact.quote,fact);
  const categoryLabels={injury:'Сообщение о травме',suspension:'Дисциплинарное сообщение',availability:'Тренировки и доступность',lineup:'Новости состава',context:'Контекст'};
  for(const fact of reports.values()) {
    const quote = node('blockquote',undefined,'research-quote');
    quote.append(node('p',(fact.subject?fact.subject+' · ':'')+fact.team),
      node('span',(fact.sourceType==='official_club'?'Официальный клуб':'Сообщение прессы')+' · '+(categoryLabels[fact.category]||'Сообщение'),'evidence-label'),
      node('p','«'+fact.quote+'»'),articleLink((fact.source||'Источник')+' · '+publicationText(fact),fact.url),
      node('p',fact.extractionMethod==='ai_selection_cached'?'Использован ранее проверенный выбор ИИ'+(fact.aiSelectedAt?' от '+dateText(fact.aiSelectedAt):'')+'. Цитата сверена с текущим текстом источника.':
        fact.extractionMethod==='ai_selection'?'Фрагмент выбран ИИ; цитата взята из источника.':'Короткий фрагмент выбран по тексту источника.','research-note'));
    ai.append(quote);
  }
  if(!data.ai?.facts?.length) {
    const messages = {not_connected:'ИИ-разбор пока не подключён. Новости с источниками доступны ниже.',
      unavailable:'ИИ не вернул завершённый проверяемый разбор. Доступные фрагменты источников показаны отдельно.',
      filtered:'Выбранные ИИ фрагменты исключены из сведений о доступности по типу источника. Это не подтверждает отсутствие травм.',
      budget_reached:'Дневной лимит ИИ-разбора достигнут. Статистика и новости доступны.',
      pending:'Эти материалы уже обрабатывает другой запрос. Доступные фрагменты источников показаны отдельно.',
      no_sources:'Нет доступных материалов для ИИ-разбора.',rejected:'Ответ ИИ не прошёл проверку цитат. Неподтверждённые сведения не показаны.',complete:'В доступных материалах ИИ не выделил фактов с проверяемой цитатой. Это не означает отсутствия травм.'};
    messages.no_relevant_evidence='Для ИИ нет подходящих сообщений о доступности. Лимит вызовов не расходуется.';
    ai.append(node('p',messages[data.ai?.status] || 'Нет проверяемых цитат для этой карточки.','research-note'));
  }
  ai.append(node('p','Цитата может относиться к прошлой игре или другому турниру. Она не подтверждает готовность игрока или стартовый состав на выбранный матч.','research-note'));
  if(data.ai?.deferredReason==='budget_reached')ai.append(node('p','Использованы ранее проверенные сведения. Для новых материалов дневной лимит ИИ уже достигнут.','research-note'));
  if(data.sourcePolicy?.reportsExcluded||data.sourcePolicy?.aiFactsExcluded)ai.append(node('p','Материалы о фэнтези-футболе и другие неподходящие фрагменты не учитываются как сведения о доступности игроков.','research-note'));
  for(const source of data.officialSources||[]){
    const labels={available:'сводка получена',pending_collection:'ожидается сбор',no_relevant_reports:'в просмотренной ленте подходящих сообщений нет',no_usable_metadata:'подходящие данные не удалось прочитать',unavailable:'сайт сейчас недоступен',not_configured:'источник пока не подключён'};
    ai.append(node('p',source.team+' · официальный сайт: '+(labels[source.status]||'статус неизвестен')+(source.fetchedAt?' · '+dateText(source.fetchedAt):'')+(source.stale?' · ожидается обновление':''),'research-note'));
  }
  $('researchNewsTitle').textContent = 'Новости и источники · '+(data.articles?.length || 0);
  const news = $('researchNews');news.replaceChildren();
  for(const article of data.articles || []) {
    const item = node('article',undefined,'research-article');item.append(articleLink(article.title,article.url),node('p',article.source+' · '+article.team+' · '+publicationText(article)));
    if(article.summary)item.append(node('p',article.summary));news.append(item);
  }
  const errors = $('researchErrors');errors.replaceChildren();
  for(const error of data.sourceErrors || []) errors.append(node('li',error.source+': '+(error.reason === 'rate_limited' || error.reason === 'rate_limit_cooldown' ? 'временное ограничение запросов' : 'данные не получены')));
  $('researchUpdated').textContent = 'Подробный разбор: '+dateText(data.collectedAt)+(data.stale ? ' · ожидает обновления.' : '.');
  status('Доступные сведения загружены.');
  $('exportResearch').disabled=false;
}

function validateCard(data){
  if(!data?.fixture || data.fixture.id!==selected?.fixtureId || !Array.isArray(data.teams) || data.teams.length!==2)
    throw new Error('Сервис вернул сведения другого матча или неполные данные.');
}

function renderContext(data){
  validateCard(data);context=data;
  $('researchResult').hidden=false;
  $('researchMatch').textContent=data.fixture.home+' — '+data.fixture.away+' · '+dateText(data.fixture.kickoff);
  $('researchTeams').replaceChildren(...data.teams.map(teamCard));renderPlayerSources();
  if(!packet)$('researchUpdated').textContent='';
}

async function readCard(path,signal){
  const response=await fetch(selected.apiBase+path,{cache:'no-store',signal});
  let data;try{data=await response.json();}catch{throw new Error('Сервис временно не вернул сведения.');}
  if(!response.ok)throw Object.assign(new Error(response.status===429?'Источник временно ограничил запросы.':data.message || 'Сведения временно недоступны.'),{status:response.status});
  return data;
}

async function loadAutomatic(){
  const token=generation,revision=evidenceRevision,id=selected.fixtureId,request=new AbortController();pending.add(request);
  const timeout=setTimeout(()=>request.abort(),30000);
  let loaded=false;
  const tasks=[
    readCard('/v1/player-context/'+id,request.signal).then(data=>{
      if(token!==generation)return;renderContext(data);loaded=true;
    }),
    readCard('/v1/research/'+id+'/stored',request.signal).then(data=>{
      if(token!==generation || revision!==evidenceRevision)return;render(data);loaded=true;
    })
  ];
  try{
    await Promise.allSettled(tasks);
    if(token!==generation)return;
    status(loaded?'Доступные сведения загружены.':'Сведения временно недоступны. Основной прогноз доступен.');
  }finally{clearTimeout(timeout);pending.delete(request);}
}

$('loadResearch').addEventListener('click',async()=>{
  if(!selected)return;
  evidenceRevision++;
  const token=generation,current=selected;controller=new AbortController();const request=controller;pending.add(request);
  const timeout=setTimeout(()=>request.abort(),60000);
  $('loadResearch').disabled=true;status('Обновляю подробный разбор…');
  try {
    const data=await readCard('/v1/research/'+current.fixtureId,request.signal);
    if(token!==generation)return;
    render(data);
  } catch(error) {
    if(token===generation)status(error.name==='AbortError'?'Обновление занимает слишком много времени. Основной прогноз доступен.':error.message,true);
  } finally {
    clearTimeout(timeout);pending.delete(request);if(token===generation){controller=null;$('loadResearch').disabled=!selected;}
  }
});

$('exportResearch').addEventListener('click',()=>{
  if(!packet)return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(packet,null,2)],{type:'application/json'}));
  const link=node('a');link.href=url;link.download='footpredict-research-'+packet.fixture.id+'.json';link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
});

reset();
