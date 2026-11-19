import {loadQuality, peekQuality} from './quality-data-v2.js';
import {rounded, topOutcome, validProbabilities} from './forecast-display.js';

const $ = id => document.getElementById(id);
const modelNames = {v1:'Основная · Пуассон', candidate:'Ночной кандидат', base:'Частоты лиги'};
const colors = {v1:'var(--green)', candidate:'var(--blue)', base:'var(--yellow)'};
const outcomes = ['П1', 'Х', 'П2'];
const pct = v => Number.isFinite(v) ? (v * 100).toFixed(1).replace('.', ',') + '%' : '—';
const decimal = v => Number.isFinite(v) ? v.toFixed(3).replace('.', ',') : '—';
const date = t => new Date(t).toLocaleString('ru-RU', {timeZone:'Europe/Moscow', dateStyle:'medium', timeStyle:'short'}) + ' МСК';

function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}

export function selectWindow(packet, source, league, days) {
  const windows = source === 'nightly' ? packet?.nightly?.windows : packet?.windows;
  if (windows?.schemaVersion !== 1 || !Object.hasOwn(windows.groups, league)) return null;
  return windows.groups[league][days] || null;
}

function table(headers, rows, caption) {
  const wrap = el('div', undefined, 'table-scroll'), t = el('table', undefined, 'accuracy-table v2-series-table');
  if (caption) t.append(el('caption', caption));
  const head = t.createTHead().insertRow();
  headers.forEach(h => { const th = el('th', h); th.scope = 'col'; head.append(th); });
  const body = t.createTBody();
  rows.forEach(values => {
    const tr = body.insertRow();
    values.forEach((v, i) => { const td = el(i === 0 ? 'th' : 'td', v); if (i === 0) td.scope = 'row'; tr.append(td); });
  });
  wrap.append(t); return wrap;
}

function chart(points, keys) {
  const figure = el('figure', undefined, 'v2-figure');
  const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 520 126'); svg.setAttribute('role', 'img'); svg.classList.add('v2-chart');
  svg.setAttribute('aria-label', 'Средний Log Loss с начала выбранного периода');
  const max = Math.max(.1, ...points.flatMap(p => keys.map(k => p.loss[k])));
  const start = Date.parse(points[0].date), span = Date.parse(points.at(-1).date) - start || 1;
  const axis = document.createElementNS(ns, 'path');
  axis.setAttribute('d','M12 6V114H506'); axis.setAttribute('stroke','var(--line)'); axis.setAttribute('fill','none'); svg.append(axis);
  for (const key of keys) {
    const line = document.createElementNS(ns, 'polyline');
    line.setAttribute('fill', 'none'); line.setAttribute('stroke', colors[key]); line.setAttribute('stroke-width', '3');
    if (key !== 'v1') line.setAttribute('stroke-dasharray', key === 'base' ? '3 4' : '10 4');
    line.setAttribute('points', points.map(pt => (12 + 494 * (Date.parse(pt.date) - start) / span) + ',' + (113 - 99 * pt.loss[key] / max)).join(' '));
    svg.append(line);
  }
  figure.append(el('figcaption', 'Накопленный Log Loss · ' + points[0].date + ' — ' + points.at(-1).date), svg);
  const legend = el('p', undefined, 'v2-legend');
  keys.forEach(k => { const label = el('span', modelNames[k] + ' ' + decimal(points.at(-1).loss[k])); label.style.borderColor = colors[k]; legend.append(label); });
  figure.append(legend); return figure;
}

function select(id, title, values) {
  const field = el('label', title), element = el('select'); element.id = id;
  for (const [value, text] of values) { const opt = el('option', text); opt.value = value; element.append(opt); }
  field.append(element); return {field, element};
}

function publicTraining() {
  const details = el('details', undefined, 'quality-panel quality-help');
  details.id='v2PublicTraining'; details.append(el('summary','Обучение и версии моделей'));
  const body = el('div'); body.setAttribute('role', 'status'); details.append(body);
  let ready = false, pending = false;
  details.ontoggle = async () => {
    if (!details.open || ready || pending) return;
    pending = true; body.textContent = 'Загружаю журнал обучения…';
    try {
      const {loadTrainerReport} = await import('./trainer-agents-ui.js?v=1.13');
      const report = await loadTrainerReport(), latest = report.latestSuccessful;
      body.replaceChildren(el('p', 'Последняя проверка · ' + date(report.latestAttempt.checkedAt) +
        (report.latestAttempt.status === 'failed' ? ' · Ошибка запуска' : ' · Выполнена')));
      if (latest) {
        body.append(el('p', latest.modelVersion + ' · обучение ' + date(latest.trainedAt) + ' · ' + latest.trainingMatches + ' обучающих матчей.'),
          el('p', 'Нейросеть на новых матчах АПЛ · сохранено ' + latest.prospective.registered +
          ', проверено ' + latest.prospective.settled + ', ожидают ' + latest.prospective.pending + '.'),
          el('p', latest.roles.examiner.recipeHistoricalGatePassed ? 'Кандидат проходит отдельную проверку на будущих играх.' :
          'Нейросеть оставлена кандидатом. В исторической проверке выросла ошибка на матчах Чемпионшипа.'));
      } else body.append(el('p', 'В журнале ещё нет успешного обучения.'));
      const link = el('a', 'История запусков и сохранённых версий');
      link.href = 'https://github.com/FoXsYsOfficial1909/Footpredict/tree/trainer-agent-reports'; link.target = '_blank'; link.rel = 'noopener noreferrer';
      body.append(link); ready = true;
    } catch { body.textContent = 'Журнал обучения сейчас недоступен. Открой этот блок повторно для новой попытки.'; }
    finally { pending = false; }
  };
  return details;
}

function renderGoalQuality(target, report) {
  target.replaceChildren();
  if (report?.protocol !== 'poisson-goal-markets-1') {
    target.append(el('p','Журнал голевых рынков временно недоступен.','muted'));
    return;
  }
  const n = report.counts;
  target.append(el('p', 'АПЛ · зафиксировано ' + n.registered + ', сверено ' + n.settled + ', ожидают ' + n.pending +
    '. Требуют проверки или исключены ' + (n.review + n.excluded) + '.', 'muted'));
  if (n.settled) {
    for (const [key, label] of [['over25','Тотал больше 2,5'], ['bothScore','Обе забьют']]) {
      const m = report.markets[key];
      target.append(el('h4',label), el('p','Фактическая частота · ' + pct(m.frequency),'muted small'));
      target.append(table(['Модель','Средний шанс','Log Loss ↓','Brier ↓'], [
        ['Пуассон', pct(m.model.meanProbability), decimal(m.model.logLoss), decimal(m.model.brier)],
        ['Частоты АПЛ', pct(m.base.meanProbability), decimal(m.base.logLoss), decimal(m.base.brier)]
      ]));
      const calibration = el('details',undefined,'quality-help');
      calibration.append(el('summary','Калибровка Пуассона'));
      const rows = m.model.calibration.flatMap((b,i) => b.n ? [[(i*20)+'–'+((i+1)*20)+'%',b.n,pct(b.predicted),pct(b.observed)]] : []);
      calibration.append(table(['Диапазон','Игры','Прогноз','Частота'], rows));
      target.append(calibration);
    }
    target.append(el('p','Для Log Loss и Brier меньше — лучше. Шкала Brier от 0 до 2.','muted small'));
  } else {
    target.append(el('p','Вероятности фиксируются минимум за сутки до игры. Метрики появятся после сверки первых результатов.'));
  }
  const audit = el('a','Сравнение на истории АПЛ');
  audit.href = 'https://github.com/FoXsYsOfficial1909/Footpredict/blob/main/docs/goal-markets-quality.md';
  audit.target = '_blank'; audit.rel = 'noopener noreferrer'; target.append(audit);
}

if (typeof document !== 'undefined') {
  const section = el('section', undefined, 'quality-panel'); section.id = 'v2Analytics';
  section.append(el('h3','Качество по периоду и лиге'));
  const controls = el('div', undefined, 'v2-filters');
  const source = select('v2QualitySource', 'Серия прогнозов', [['nightly','Совместная проверка АПЛ'],['archive','Первоначальный журнал v1']]);
  const period = select('v2QualityPeriod','Период по времени начала', [['0','Весь период'],['7','Последние 7 дней'],['30','Последние 30 дней']]);
  const league = select('v2QualityLeague','Чемпионат', [['all','Все доступные']]);
  controls.append(source.field, period.field, league.field);
  const body = el('div'); body.id = 'v2AnalyticsBody'; body.setAttribute('aria-live','polite');
  const releaseInfo = el('details', undefined, 'quality-panel quality-help');
  releaseInfo.append(el('summary', 'Смена основной модели'), el('p', 'Загружаю решение…'));
  const goals = el('details', undefined, 'quality-panel quality-help'); goals.id = 'v2GoalQuality';
  const goalBody = el('div'); goalBody.append(el('p','Загружаю журнал голевых рынков…','muted'));
  goals.append(el('summary','Тотал 2,5 и «обе забьют»'), goalBody);
  section.append(controls, body, goals, releaseInfo, publicTraining()); $('qualityOverview').after(section);

  function render() {
    const packet = peekQuality(); if (!packet) return;
    const series = source.element.value, keys = series === 'nightly' ? ['v1','candidate','base'] : ['v1','base'];
    const summary = selectWindow(packet, series, league.element.value, period.element.value);
    body.replaceChildren();
    if (!summary) { body.append(el('p','Детализация выбранного периода пока недоступна. Общие результаты серии показаны выше.')); return; }
    body.append(el('p','Начались ' + summary.archived + ' зарегистрированных матчей. Проверено ' + summary.settled +
      ', ожидают сверки ' + summary.pending + ', требуют проверки ' + summary.review + ', исключено ' + summary.excluded +
      '. Для сравнения доступны все вероятности на ' + summary.paired + ' матчах.'));
    if (!summary.paired) { body.append(el('p','В этом периоде ещё нет результатов для сравнения.')); return; }
    body.append(table(['Модель','Попадания','Log Loss ↓','Brier ↓'], keys.map(k => {
      const m = summary.models[k]; return [modelNames[k],pct(m.accuracy),decimal(m.logLoss),decimal(m.brier)];
    }), 'Одни и те же матчи. Меньшая ошибка вероятностей лучше. Brier измеряется от 0 до 2.'));
    const ties=keys.filter(k=>summary.models[k].ties);
    if(ties.length)body.append(el('p','Равные максимумы исключены из доли попаданий · '+ties.map(k=>modelNames[k]+' '+summary.models[k].ties).join(', ')+'.'));
    if (summary.timeline.length > 1) body.append(chart(summary.timeline, keys));
    if (summary.paired < 100) body.append(el('p','Матчей пока мало для устойчивого вывода о превосходстве модели.','muted'));
    const cal = el('details', undefined, 'quality-help'); cal.append(el('summary','Калибровка основной модели по исходам'));
    const cells = summary.calibration.flatMap((bins,k) => bins.flatMap((b,i) =>
      b.n ? [[outcomes[k],(i*20)+'–'+((i+1)*20)+'%',b.n,pct(b.predicted),pct(b.observed)]] : []));
    cal.append(table(['Исход','Диапазон','Игры','Прогноз','Частота'],cells,'Диапазоны с наблюдениями на общей выборке.'));
    const diag = el('details', undefined, 'quality-help'); diag.append(el('summary','Диагностика ошибок'));
    summary.diagnostics.forEach((d,i) => {
      const name = [...outcomes, 'Фаворит'][i];
      const text = d.days < 30 ? 'доступно ' + d.days + ' игровых дней из необходимых 30' :
        d.signal ? (d.mean > 0 ? 'вероятность систематически занижена' : 'вероятность систематически завышена') :
        'значимого отклонения от частоты не выявлено';
      diag.append(el('p', name + ' · ' + text + '.'));
    });
    diag.append(el('p','Сравниваются прогноз и факт по игровым дням с поправкой на четыре проверки. Зависимость между турами ограничивает вывод. Вес недавних матчей проверяется отдельно в историческом эксперименте.','muted'));
    body.append(cal, diag);
  }
  function selectLeagues() {
    const p = peekQuality(), current = league.element.value;
    const report = source.element.value === 'nightly' ? p?.nightly : p;
    const leagues = Object.keys(report?.windows?.groups || {}).filter(l=>l!=='all');
    league.element.replaceChildren(new Option('Все доступные','all'), ...leagues.map(l=>new Option(l==='PL'?'Английская Премьер-лига':l,l)));
    if(leagues.includes(current))league.element.value=current;
    render();
  }
  let historyLimit = 10;
  function renderSaved(packet) {
    const archive = packet.nightly;
    const list = $('v2SavedList'), more = $('v2SavedMore'), status = $('v2SavedStatus');
    list.replaceChildren(); more.hidden = true;
    if (!archive?.fixtures?.length) {
      status.textContent = 'В совместной серии АПЛ пока нет сохранённых прогнозов.';
      return;
    }
    status.textContent = 'Сохранено ' + archive.totals.registered + ', проверено ' + archive.totals.settled + ', ожидают ' + archive.totals.pending + '.';
    if (archive.fixtureScope.returned < archive.fixtureScope.total)
      status.textContent += ' В списке последние ' + archive.fixtureScope.returned + ' записей.';
    for (const row of archive.fixtures.slice(0, historyLimit)) {
      const item = el('article', undefined, 'v2-saved-item');
      item.dataset.fixtureId = row.fixtureId;
      const p = row.models?.[row.primaryModel === 'candidate' ? 'candidate' : 'v1']?.probabilities;
      if (!Number.isSafeInteger(row.fixtureId) || row.fixtureId <= 0 ||
          !Number.isFinite(Date.parse(row.kickoff)) || !Number.isFinite(Date.parse(row.savedAt))) continue;
      const heading = el('h3', row.home + ' — ' + row.away);
      const params = new URLSearchParams({match:String(row.fixtureId)});
      if (Number.isInteger(row.year)) params.set('season', String(row.year));
      const link = el('a', 'Открыть матч');
      link.href = './?' + params + '#matches';
      item.append(heading, el('p', 'Начало · ' + date(row.kickoff) + ' · сохранено ' + date(row.savedAt), 'muted small'));
      if (row.state === 'excluded' || row.state === 'review' || !validProbabilities(p)) {
        item.append(el('p', 'Запись требует проверки.'));
      } else {
        item.append(el('p', rounded(p).map((v,i) => outcomes[i] + ' ' + v.replace('.', ',')).join(' · ')));
        if (row.state === 'settled' && row.latest?.score) {
          const {home, away} = row.latest.score, best = topOutcome(p);
          const actual = home > away ? 0 : home === away ? 1 : 2;
          item.append(el('p', 'Итог ' + home + ' : ' + away + ' · ' +
            (best === null ? 'равный максимум' : best === actual ? 'прогноз совпал' : 'прогноз не совпал')));
        } else item.append(el('p', 'Ожидается итоговая сверка.', 'muted'));
      }
      item.append(link); list.append(item);
    }
    more.hidden = archive.fixtures.length <= historyLimit;
  }
  $('v2SavedMore').onclick = () => {
    historyLimit += 10; const latest = peekQuality(); if (latest) renderSaved(latest);
  };

  function updateOverview(packet) {
    renderGoalQuality(goalBody, packet.nightly?.goalMarkets);
    const m=packet.nightly?.models?.v1;
    const release=packet.nightly?.release;
    if(release){
      const reasons={
        collecting:'Идёт сбор результатов для решения о смене модели.',
        awaiting_review:'Ожидается плановая проверка.',
        approved:'Ансамбль допущен к новым прогнозам АПЛ.',
        rejected:release.reason==='insufficient_coverage'?'Пуассон используется дальше. Покрытие заранее выбранных матчей слишком мало.':
          'Пуассон используется дальше. Преимущество ансамбля в вероятностном прогнозе не подтверждено.'
      };
      releaseInfo.lastElementChild.textContent=reasons[release.status] + ' Сверено ' + release.settled + ' из ' +
        release.calendar + ' матчей. Проверка запланирована ' + date(release.schedule.decisionAt) + '.' +
        (release.paused?' Для новых прогнозов включён Пуассон.':'');
      $('v2Model').textContent=release.useCandidateForNewForecasts?'Ансамбль в АПЛ · Пуассон в остальных лигах':'Пуассон';
    }else releaseInfo.lastElementChild.textContent='Журнал решения пока недоступен.';
    $('v2Checked').textContent=m?.n ?? '—'; $('v2Accuracy').textContent=pct(m?.accuracy);
    if ($('tab-quality').getAttribute('aria-selected') === 'true') selectLeagues();
    if ($('tab-history').getAttribute('aria-selected') === 'true') renderSaved(packet);
  }
  source.element.onchange=selectLeagues; league.element.onchange=render; period.element.onchange=render;
  window.addEventListener('footpredict:quality-loaded', e=>updateOverview(e.detail));
  window.addEventListener('footpredict:tab-selected', e=>{
    const packet=peekQuality();
    if(e.detail.tab==='quality'&&packet)selectLeagues();
    if(e.detail.tab==='history'){
      if(packet)renderSaved(packet);
      loadQuality().then(renderSaved).catch(()=>{
        $('v2SavedStatus').textContent=packet?'Показана последняя доступная копия совместной серии.':'Совместная серия временно недоступна.';
      });
    }
  });
  loadQuality().then(updateOverview).catch(()=>{body.textContent='Архив сейчас недоступен. Используй «Обновить результаты» для повторной загрузки.';});
}
