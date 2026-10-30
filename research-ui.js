const $ = id => document.getElementById(id);
let selected = null, packet = null, generation = 0, controller = null;

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
  generation++;controller?.abort();controller = null;selected = null;packet = null;
  $('loadResearch').disabled = true;$('exportResearch').disabled = true;
  $('researchResult').hidden = true;
  status('Выбери предстоящий матч АПЛ, затем нажми «Собрать сведения».');
}

window.addEventListener('footpredict:selection-reset', reset);
window.addEventListener('footpredict:match-selected', event => {
  reset();
  const detail = event.detail;
  if (detail?.league !== 'PL') { status('Разбор пока доступен для матчей АПЛ.');return; }
  if (!Number.isSafeInteger(detail.fixtureId) || detail.fixtureId < 1) return;
  selected = detail;
  $('loadResearch').disabled = false;
  status('Матч выбран. Соберём форму, доступный календарь и свежие сообщения с источниками.');
});

function coverage(data) {
  const labels = [['form','Форма'],['news','Новости'],['availabilityReports','Сообщения о доступности'],['officialNews','Официальные сводки'],['ai','ИИ-разбор'],['injuries','Сводный список травм'],['lineups','Стартовые составы'],['completeCalendar','Полный календарь']];
  $('researchCoverage').replaceChildren(...labels.map(([key,label]) => node('span', (data.coverage?.[key] ? '✓ ' : '○ ') + label, 'coverage-chip' + (data.coverage?.[key] ? ' available' : ''))));
}

function teamCard(team) {
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
  card.append(strip,node('p','Последние завершённые матчи АПЛ, новые слева.','research-note'));
  const list = node('dl',undefined,'research-facts');
  const fact = (title,text) => list.append(node('dt',title),node('dd',text));
  const average = value => typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2).replace('.',',') : '—';
  fact('Дома · забивают / пропускают',average(team.home?.goalsFor) + ' / ' + average(team.home?.goalsAgainst) + ' · игр: ' + (team.home?.games || 0));
  fact('В гостях · забивают / пропускают',average(team.away?.goalsFor) + ' / ' + average(team.away?.goalsAgainst) + ' · игр: ' + (team.away?.games || 0));
  const calendar = team.calendar;
  fact('Интервал в доступном календаре',typeof calendar?.gapDays === 'number' ? calendar.gapDays.toFixed(1).replace('.',',') + ' дн.' + (calendar.basedOnScheduledMatch ? ' после запланированной игры' : ' после последней известной игры') : 'Полный календарь не получен.');
  const injuries = team.injuries;
  fact('Сводка о травмах',injuries?.reports?.length ? injuries.reports.map(item => item.player + ': ' + item.reason).join('; ') : injuries?.status === 'no_reports_found' ? 'Источник не вернул сообщений. Это не подтверждает отсутствие травм.' : team.availability?.reports?.length ? 'Полного списка нет. Доступные сообщения клубов и прессы — ниже.' : 'Источник пока недоступен. Статус игроков неизвестен.');
  fact('Состав',team.lineup?.players?.length ? team.lineup.players.map(player=>player.name).join(', ') : 'Не получен. Стартовый состав не подтверждён.');
  if(team.availability)fact('Сообщения о доступности',team.availability.reports?.length?'Найдено '+team.availability.reports.length+'. Цитаты и даты — ниже. Готовность к матчу не подтверждена.':'В собранных фрагментах подходящих сообщений нет. Статус игроков остаётся неизвестным.');
  card.append(list);
  const details = node('details',undefined,'research-form-details');details.append(node('summary','Матчи, из которых собрана форма'));
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
  coverage(data);$('researchTeams').replaceChildren(...data.teams.map(teamCard));renderCandidate(data.candidate);
  const ai = $('researchAi');ai.replaceChildren(node('h3','Сведения о доступности игроков'));
  const reports=new Map();
  for(const fact of [...(data.availabilityReports||[]),...(data.ai?.facts||[])])reports.set(fact.url+'\n'+fact.quote,fact);
  const categoryLabels={injury:'Сообщение о травме',suspension:'Дисциплинарное сообщение',availability:'Тренировки и доступность',lineup:'Новости состава',context:'Контекст'};
  for(const fact of reports.values()) {
    const quote = node('blockquote',undefined,'research-quote');
    quote.append(node('p',(fact.subject?fact.subject+' · ':'')+fact.team),
      node('span',(fact.sourceType==='official_club'?'Официальный клуб':'Сообщение прессы')+' · '+(categoryLabels[fact.category]||'Сообщение'),'evidence-label'),
      node('p','«'+fact.quote+'»'),articleLink((fact.source||'Источник')+' · '+publicationText(fact),fact.url),
      node('p',fact.extractionMethod==='ai_selection'?'Фрагмент выбран ИИ; цитата взята из источника.':'Короткий фрагмент выбран по тексту источника.','research-note'));
    ai.append(quote);
  }
  if(!data.ai?.facts?.length) {
    const messages = {not_connected:'ИИ-разбор пока не подключён. Новости с источниками доступны ниже.',
      unavailable:'ИИ не вернул завершённый проверяемый разбор. Доступные фрагменты источников показаны отдельно.',
      filtered:'Выбранные ИИ фрагменты исключены из сведений о доступности по типу источника. Это не подтверждает отсутствие травм.',
      budget_reached:'Дневной лимит ИИ-разбора достигнут. Статистика и новости доступны.',
      no_sources:'Нет доступных материалов для ИИ-разбора.',rejected:'Ответ ИИ не прошёл проверку цитат. Неподтверждённые сведения не показаны.',complete:'В доступных материалах ИИ не выделил фактов с проверяемой цитатой. Это не означает отсутствия травм.'};
    messages.no_relevant_evidence='Для ИИ нет подходящих сообщений о доступности. Лимит вызовов не расходуется.';
    ai.append(node('p',messages[data.ai?.status] || 'Нет проверяемых цитат для этой карточки.','research-note'));
  }
  ai.append(node('p','Цитата может относиться к прошлой игре или другому турниру. Она не подтверждает готовность игрока или стартовый состав на выбранный матч.','research-note'));
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
  $('researchUpdated').textContent = 'Сведения получены '+dateText(data.collectedAt)+(data.stale ? '. Обновление отложено из-за лимита запросов.' : '. Карточка обновляется не чаще раза в час.');
  status('Карточка готова. Пробелы в данных отмечены отдельно.');
  packet=data;$('exportResearch').disabled=false;
}

$('loadResearch').addEventListener('click',async()=>{
  if(!selected)return;
  const token=generation,current=selected;controller=new AbortController();
  const timeout=setTimeout(()=>controller?.abort(),60000);
  $('loadResearch').disabled=true;status('Собираю статистику и сообщения с источниками…');
  try {
    const response=await fetch(current.apiBase+'/v1/research/'+current.fixtureId,{cache:'no-store',signal:controller.signal});
    let data;try{data=await response.json();}catch{throw new Error('Сервис вернул ответ без данных карточки.');}
    if(!response.ok)throw new Error(response.status===429?'Достигнут лимит сбора сведений. Повтори позже.':data.message||'Не удалось собрать сведения.');
    if(token!==generation)return;
    render(data);
  } catch(error) {
    if(token===generation)status(error.name==='AbortError'?'Сбор занимает слишком много времени. Попробуй позже; основной прогноз доступен.':error.message,true);
  } finally {
    clearTimeout(timeout);if(token===generation){controller=null;$('loadResearch').disabled=!selected;}
  }
});

$('exportResearch').addEventListener('click',()=>{
  if(!packet)return;
  const url=URL.createObjectURL(new Blob([JSON.stringify(packet,null,2)],{type:'application/json'}));
  const link=node('a');link.href=url;link.download='footpredict-research-'+packet.fixture.id+'.json';link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
});

reset();
