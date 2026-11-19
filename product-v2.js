import {rounded, topOutcome} from './forecast-display.js';
import {MODEL, getRecord, assessConfidence} from './v2-evidence.js';
import {API,peekQuality} from './quality-data-v2.js';
import {forecastMarkets,marketGroups,marketQuote} from './forecast-markets.js';
export {rounded} from './forecast-display.js';

const DAY = 86400000;
const $ = id => document.getElementById(id);
const outcomes = ['П1', 'Ничья', 'П2'];
const risks = {low:'Низкий', medium:'Средний', high:'Высокий'};
const marketPercent = p => p > .9995 && p < 1 ? '>99,9%' : p < .0005 && p > 0 ? '<0,1%' :
  (p * 100).toFixed(1).replace('.', ',') + '%';
const statusNames = {FINISHED:'Завершён', IN_PLAY:'Идёт матч', PAUSED:'Перерыв',
  EXTRA_TIME:'Дополнительное время', PENALTY_SHOOTOUT:'Серия пенальти', TIMED:'По расписанию',
  SCHEDULED:'Запланирован', POSTPONED:'Перенесён', CANCELLED:'Отменён', SUSPENDED:'Приостановлен',
  AWARDED:'Технический результат'};
const live = new Set(['IN_PLAY', 'PAUSED', 'EXTRA_TIME', 'PENALTY_SHOOTOUT']);
const scheduled = g => ['TIMED', 'SCHEDULED'].includes(g.status);
const cardTime = new Intl.DateTimeFormat('ru-RU',{
  timeZone:'Europe/Moscow', day:'numeric', month:'short', hour:'2-digit', minute:'2-digit'
});
const ruDateTime = new Intl.DateTimeFormat('ru-RU', {timeZone:'Europe/Moscow', dateStyle:'medium', timeStyle:'short'});
const dayFormat = new Intl.DateTimeFormat('en-CA', {timeZone:'Europe/Moscow'});
const when = t => ruDateTime.format(new Date(t)) + ' МСК';
const validScore = s => s && [s.home, s.away].every(v => Number.isInteger(v) && v >= 0 && v <= 30);
const regulation = g => g.score?.duration === 'REGULAR' ? g.score.fullTime : g.score?.regularTime;
const actual = s => s.home > s.away ? 0 : s.home === s.away ? 1 : 2;
const label = g => g.homeTeam.name + ' — ' + g.awayTeam.name;
const favoriteKey = 'footpredict:favorites:v2';
let dispose = () => {};

function el(tag, text, cls) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (cls) element.className = cls;
  return element;
}

function status(g, now) {
  if (g.status === 'FINISHED') return 'finished';
  if (live.has(g.status)) return 'live';
  if (scheduled(g) && Date.parse(g.utcDate) > now) return 'upcoming';
  return 'changed';
}

export function selectMatches(rows, filters, now = Date.now(), favorites = []) {
  const query = filters.search.trim().toLocaleLowerCase('ru');
  const result = rows.filter(r => {
    const g = r.match, time = Date.parse(g.utcDate);
    if (filters.status !== 'all' && status(g, now) !== filters.status) return false;
    if (filters.confidence !== 'all' && filters.confidence !== r.confidence) return false;
    if (filters.favorite && !['match:' + g.id, 'team:' + g.homeTeam.id, 'team:' + g.awayTeam.id].some(k => favorites.includes(k))) return false;
    if (![label(g), r.league, r.leagueCode].join(' ').toLocaleLowerCase('ru').includes(query)) return false;
    if (filters.period === 'today') return dayFormat.format(time) === dayFormat.format(now);
    if (filters.period === 'tomorrow') return dayFormat.format(time) === dayFormat.format(now + DAY);
    return filters.period !== 'week' || (time >= now && time <= now + 7 * DAY);
  });
  const outcomeChance = row => Math.max(...(row.p || [-1]));
  const rank = filters.sort === 'market' ? row => row.markets?.best.p ?? -1 : outcomeChance;
  return result.sort((a, b) => (filters.sort === 'time' ? 0 : rank(b) - rank(a)) ||
    Date.parse(a.match.utcDate) - Date.parse(b.match.utcDate) || a.match.id - b.match.id);
}

function readFavorites() {
  try {
    const list = JSON.parse(localStorage.getItem(favoriteKey) || '[]');
    return Array.isArray(list) ? list.filter(v => /^(team|match):\d+$/.test(v)).slice(-500) : [];
  } catch { return []; }
}

function favoriteButton(key, title, onChange, compact = false) {
  const button = el('button', undefined, 'favorite secondary');
  button.type = 'button';
  if (compact) button.classList.add('v2-favorite-icon');
  const sync = () => {
    const saved = readFavorites().includes(key);
    button.setAttribute('aria-pressed', String(saved));
    button.setAttribute('aria-label', (saved ? 'Убрать из избранного · ' : 'В избранное · ') + title);
    button.textContent = compact ? (saved ? '★' : '☆') : (saved ? '★ В избранном' : '☆ В избранное');
  };
  sync();
  button.onclick = () => {
    let list = readFavorites();
    list = list.includes(key) ? list.filter(v => v !== key) : [...list, key].slice(-500);
    try { localStorage.setItem(favoriteKey, JSON.stringify(list)); sync(); onChange(); }
    catch { button.title = 'Хранилище недоступно'; }
  };
  return button;
}

function probabilityBar(p) {
  const wrap = el('div', undefined, 'v2-distribution'), bar = el('div', undefined, 'v2-segments');
  const labels = el('div', undefined, 'v2-prob-labels');
  const best = topOutcome(p);
  bar.setAttribute('aria-hidden', 'true');
  rounded(p).forEach((value, i) => {
    const segment = el('span'); segment.style.width = p[i] * 100 + '%'; bar.append(segment);
    const cell = el('div', undefined, i === best ? 'v2-leading' : '');
    cell.append(el('span', outcomes[i]), el('strong', value.replace('.', ',')));
    labels.append(cell);
  });
  wrap.append(bar, labels);
  return wrap;
}

function crest(team) {
  try {
    const url = new URL(team.crest);
    if (url.origin !== 'https://crests.football-data.org') return null;
    const img = el('img');
    img.src = url.href; img.width = 22; img.height = 22; img.alt = ''; img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer'; img.onerror = () => img.remove();
    return img;
  } catch { return null; }
}

export function recentGames(history, team, cutoff, n = 10) {
  const games = new Map();
  for (const g of history) {
    if (g.status !== 'FINISHED' || g.score?.duration !== 'REGULAR' || !validScore(g.score.fullTime) ||
        !(Date.parse(g.utcDate) <= cutoff - 4 * 3600000) || (g.homeTeam?.id !== team && g.awayTeam?.id !== team)) continue;
    const old = games.get(g.id);
    if (old && (old.utcDate !== g.utcDate || old.homeTeam.id !== g.homeTeam.id || old.awayTeam.id !== g.awayTeam.id ||
      old.score.fullTime.home !== g.score.fullTime.home || old.score.fullTime.away !== g.score.fullTime.away)) return [];
    games.set(g.id, g);
  }
  return [...games.values()].sort((a, b) => Date.parse(b.utcDate) - Date.parse(a.utcDate) || b.id - a.id).slice(0, n);
}

function sourceText(row) {
  if (row.record) return 'Сохранено до игры · ' + when(row.record.savedAt) +
    (row.record.source === 'nightly' ? ' · совместная серия АПЛ' : ' · журнал v1');
  return 'Предварительный расчёт · ' + when(row.asOf);
}

function matchProvenance(row) {
  const root = el('div', undefined, 'v2-model-data');
  if (row.p) {
    root.append(el('p', sourceText(row) + ' · ' + (row.record?.modelVersion || MODEL), 'muted small'));
    root.append(el('p', 'Уверенность модели · ' + row.assessment.label + '. ' + row.assessment.explanation, 'v2-confidence'));
  } else root.append(el('p', row.message, 'muted'));
  if (row.record?.source === 'nightly') {
    const link = el('a', 'Первоначальный расчёт и использованные матчи');
    link.href = API + '/v1/candidate-comparison/' + row.match.id + '/evidence';
    link.target = '_blank'; link.rel = 'noopener noreferrer'; root.append(link);
  }
  if (row.record && row.record.modelVersion !== MODEL) {
    root.append(el('p', 'Ансамбль объединяет обученный Пуассон с рейтингами Elo и учитывает силы соперников и домашнее преимущество.'));
  }
  if (row.stats && row.p) {
    root.append(el('p', 'Пуассон сопоставляет результативность хозяев дома и гостей на выезде со средними голами лиги.'));
    const facts = el('dl', undefined, 'v2-facts');
    for (const [title, value] of [['Хозяева забивают дома', row.stats.homeScored], ['Хозяева пропускают дома', row.stats.homeConceded],
      ['Гости забивают на выезде', row.stats.awayScored], ['Гости пропускают на выезде', row.stats.awayConceded]]) {
      if (!Number.isFinite(value)) continue;
      const item = el('div'); item.append(el('dt', title), el('dd', value.toFixed(2).replace('.', ','))); facts.append(item);
    }
    root.append(facts, el('p', 'Игр в расчёте · хозяева дома ' + row.stats.homeCount + ', гости на выезде ' + row.stats.awayCount + '.', 'muted small'));
  }
  return root;
}

function teamHistory(row, history, onFavorite) {
  const g = row.match, root = el('div');
  const grid = el('div', undefined, 'v2-team-grid');
  for (const team of [g.homeTeam, g.awayTeam]) {
    const block = el('section');
    block.append(el('h3', team.name), favoriteButton('team:' + team.id, team.name, onFavorite));
    const games = recentGames(history, team.id, row.asOf);
    if (games.length) {
      for (const size of [5, 10]) {
        const sample = games.slice(0, size);
        const points = sample.reduce((sum, result) => {
          const y = actual(result.score.fullTime), home = result.homeTeam.id === team.id;
          return sum + (y === 1 ? 1 : y === (home ? 0 : 2) ? 3 : 0);
        }, 0);
        block.append(el('p', 'Форма за ' + size + ' игр · ' + points + ' очков, в истории ' + sample.length + ' из ' + size, 'muted'));
      }
    }
    const list = el('ol', undefined, 'v2-recent');
    games.forEach(m => list.append(el('li', m.homeTeam.name + ' ' + m.score.fullTime.home + ' : ' +
      m.score.fullTime.away + ' ' + m.awayTeam.name + ' · ' + when(m.utcDate))));
    block.append(list); if (!games.length) block.append(el('p', 'Прошлых результатов в доступной истории нет.'));
    grid.append(block);
  }
  root.append(grid);
  const meetings = recentGames(history.filter(m =>
    (m.homeTeam?.id === g.homeTeam.id && m.awayTeam?.id === g.awayTeam.id) ||
    (m.awayTeam?.id === g.homeTeam.id && m.homeTeam?.id === g.awayTeam.id)), g.homeTeam.id, row.asOf, 5);
  root.append(el('h4','Очные встречи'));
  const list = el('ul', undefined, 'v2-recent');
  for (const m of meetings) list.append(el('li', m.homeTeam.name + ' ' + m.score.fullTime.home + ' : ' +
    m.score.fullTime.away + ' ' + m.awayTeam.name + ' · ' + when(m.utcDate)));
  root.append(meetings.length ? list : el('p','Очные встречи в доступной истории лиги не найдены.','muted small'));
  return root;
}

function marketTable(rows, odds = null) {
  const table = el('table', undefined, 'v2-market-table');
  const header = table.createTHead().insertRow();
  for (const label of ['Рынок', 'Шанс', 'Риск']) {
    const cell = el('th', label); cell.scope = 'col'; header.append(cell);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const tr = body.insertRow(); tr.dataset.market = row.id;
    const title = el('th', row.label); title.scope='row';
    const quote = marketQuote(row, odds);
    if (quote) {
      const sign = quote.expectedReturn > 0 ? '+' : '';
      title.append(el('small', 'Кэф. ' + quote.price.toLocaleString('ru-RU') + ' · модельная отдача ' +
        sign + (quote.expectedReturn * 100).toFixed(1).replace('.', ',') + '%', 'v2-quote'));
    }
    const risk = el('td', undefined, 'v2-market-risk');
    risk.append(el('span', risks[row.risk], 'v2-risk v2-risk-' + row.risk));
    tr.append(title, el('td', marketPercent(row.p), 'v2-market-chance'), risk);
  }
  return table;
}

function compareMarkets(row, odds, fetchedAt) {
  const view = el('div'), label = el('label', 'Риск непрохода', 'v2-market-filter');
  const filter = el('select');
  for (const [key, name] of [['all', 'Все уровни'], ...Object.entries(risks)]) filter.add(new Option(name, key));
  label.append(filter); view.append(label);
  const ranked = el('div');
  const show = el('button', 'Показать ещё рынки', 'secondary v2-more-markets'); show.type = 'button';
  let limit = 12;
  const draw = () => {
    const items = row.markets.all.filter(m => filter.value === 'all' || m.risk === filter.value);
    ranked.replaceChildren(items.length ? marketTable(items.slice(0, limit), odds) :
      el('p','Рынки этого уровня риска отсутствуют.','muted small'));
    show.hidden = items.length <= limit;
    if (show.hidden && document.activeElement === show) filter.focus();
  };
  filter.onchange = () => { limit = 12; draw(); };
  show.onclick = () => { limit += 12; draw(); };
  const quoteNote = row.markets.all.some(m => marketQuote(m, odds)) ?
    'Коэффициенты П1/Х/П2 из календаря football-data.org, получен ' + when(fetchedAt) +
    '. Модельная отдача учитывает шанс и указанный коэффициент.' : 'Коэффициенты поставщика недоступны.';
  view.append(ranked, show, el('p', quoteNote, 'muted small'));
  draw();
  return view;
}

function verdict(row) {
  const best = topOutcome(row.p);
  if (best === null) return 'равный максимум';
  return best === actual(row.result) ? 'прогноз совпал' : 'прогноз не совпал';
}

function parkContext() {
  const research = $('matchResearch'), corners = $('cornerForecast');
  if (research && research.nextElementSibling !== $('calculator')) {
    $('calculator').before(research);
    research.classList.add('v2-context-docked');
  }
  if (corners && corners.parentElement !== $('results')) $('results').append(corners);
}

export function clearExplorer() {
  dispose();
  dispose = () => {};
  parkContext();
  $('v2MatchDetail')?.remove();
}

export function renderExplorer({all, history, league, leagueCode, country, preview, openOwner, loadContext,
  resetSelection, freshness, providerFetchedAt, archive = false, targetId}) {
  clearExplorer();
  const target = $('content'), loadedAt = Date.now();
  const fields = {search:$('v2Search'), period:$('v2Period'), status:$('v2Status'), sort:$('v2Sort'),
    confidence:$('v2Confidence'), favorite:$('v2Favorites')};
  if (archive) fields.period.value = 'all';
  const calendar = new Map(), conflicts = new Set();
  for (const g of all) {
    const time = Date.parse(g.utcDate);
    if (![g.id, g.homeTeam?.id, g.awayTeam?.id].every(n => Number.isSafeInteger(n) && n > 0) ||
        g.homeTeam.id === g.awayTeam.id || !Number.isFinite(time) ||
        (!archive && (time < loadedAt - 7 * DAY || time > loadedAt + 14 * DAY) && g.id !== targetId)) continue;
    const other = calendar.get(g.id);
    if (other && (other.utcDate !== g.utcDate || other.homeTeam.id !== g.homeTeam.id ||
        other.awayTeam.id !== g.awayTeam.id || other.status !== g.status ||
        JSON.stringify(other.score?.fullTime) !== JSON.stringify(g.score?.fullTime))) conflicts.add(g.id);
    calendar.set(g.id, g);
  }
  const previews = new Map();
  for (const g of calendar.values()) {
    if (!conflicts.has(g.id) && status(g, loadedAt) === 'upcoming') {
      try { previews.set(g.id, preview(g)); } catch { /* Insufficient or conflicting historical inputs. */ }
    }
  }
  let rows = [], limit = 20, selected = null, category = 'outcomes', initialized = null;
  const deepLink = targetId && calendar.get(targetId);
  if (deepLink) {
    selected = targetId;
    fields.period.value = 'all';
    fields.status.value = status(deepLink, loadedAt) === 'finished' ? 'finished' : 'all';
  }
  function reconcile() {
    const packet = peekQuality(), now = Date.now();
    rows = [...calendar.values()].map(match => {
      const state = status(match, now), invalid = conflicts.has(match.id);
      const record = invalid ? null : getRecord(packet, match, leagueCode, now);
      const current = state === 'upcoming' && !invalid ? previews.get(match.id) : null;
      const p = record?.probabilities || current?.p;
      const stats = record ? record.stats : current?.stats;
      const scoreInput = record?.modelVersion && record.modelVersion !== MODEL ? null : stats;
      const markets = forecastMarkets(p, scoreInput, record ? null : current?.calculation);
      const asOf = record ? Date.parse(record.savedAt) : Math.min(loadedAt, Date.parse(match.utcDate));
      const source = record?.source || (leagueCode === 'PL' ? 'nightly' : 'archive');
      const assessment = assessConfidence(packet, {match, league: leagueCode, p, stats, asOf, source});
      const result = record?.state === 'settled' ? record.score : state === 'finished' ? regulation(match) : null;
      let message = 'Для расчёта недостаточно проверенной истории.';
      if (invalid) message = 'В расписании расходятся сведения об этом матче.';
      else if (state === 'changed') message = 'Проверь статус и время начала.';
      else if (state !== 'upcoming') message = 'В загруженном журнале нет подтверждённого предматчевого прогноза.';
      return {match, state, league, leagueCode, p, stats, markets, asOf, record, result, message, assessment, confidence:assessment.level};
    });
    render();
  }

  function closeSelection() {
    parkContext();
    if (initialized !== null) resetSelection();
    selected = null; initialized = null; category = 'outcomes';
  }

  function showDetails(row, card, button) {
    const g = row.match, details = el('section', undefined, 'v2-match-panel');
    details.id = 'v2MatchDetail'; details.setAttribute('aria-label', 'Прогноз матча ' + label(g));
    button.setAttribute('aria-controls', details.id);
    const controls = el('label', 'Раздел прогноза', 'v2-market-section');
    const choice = el('select'); choice.id = 'v2ForecastSection';
    const groups = [...marketGroups, ['compare', 'Сравнение рынков'], ['history', 'История и форма'], ['model','О расчёте']];
    const upcoming = scheduled(g) && Date.parse(g.utcDate) > Date.now();
    if (leagueCode === 'PL' && upcoming) groups.push(['context','Составы и угловые']);
    for (const [id, name] of groups) choice.add(new Option(name, id));
    if (!groups.some(([id]) => id === category)) category = 'outcomes';
    choice.value = category;
    controls.append(choice);
    const content = el('div');
    details.append(controls, content);
    card.append(details);
    const draw = () => {
      parkContext();
      content.replaceChildren();
      if (category === 'history') { content.append(teamHistory(row, history, render)); return; }
      if (category === 'model') { content.append(matchProvenance(row)); return; }
      if (category === 'context') {
        const research = $('matchResearch');
        research.classList.remove('v2-context-docked');
        content.append(research);
        if (!document.body.classList.contains('owner-mode')) content.append($('cornerForecast'));
        if (initialized !== g.id) { initialized = g.id; loadContext(g); }
        return;
      }
      const entries = row.markets?.groups[category];
      if (category === 'compare' && row.markets) {
        content.append(compareMarkets(row, upcoming && Number.isFinite(providerFetchedAt) ? g.odds : null, providerFetchedAt));
      } else if (entries?.length) {
        content.append(marketTable(entries));
      } else {
        content.append(el('p', row.p ? 'Для этого рынка нет проверенного распределения голов.' : row.message, 'muted'));
        return;
      }
      content.append(el('p','Риск непрохода по модели · низкий при шансе от 85%, средний от 70%, высокий ниже 70%.','muted small v2-market-note'));
    };
    choice.onchange = () => { category = choice.value; draw(); };
    draw();
  }

  function render() {
    const filter = Object.fromEntries(Object.entries(fields).map(([key, n]) => [key, key === 'favorite' ? n.checked : n.value]));
    const shown = selectMatches(rows, filter, Date.now(), readFavorites());
    const visible = shown.slice(0, limit);
    if (deepLink && selected === targetId && !visible.some(r => r.match.id === targetId)) {
      const matched = shown.find(r => r.match.id === targetId);
      if (matched) visible.splice(Math.max(0, limit - 1), 1, matched);
    }
    if (selected !== null && !visible.some(r => r.match.id === selected)) closeSelection();
    parkContext();
    target.replaceChildren();
    const future = rows.filter(r => r.state === 'upcoming');
    $('v2Available').textContent = rows.length;
    $('v2Today').textContent = rows.filter(r => r.p && dayFormat.format(Date.parse(r.match.utcDate)) === dayFormat.format(Date.now())).length;
    const latest = history.filter(g => g.status === 'FINISHED' && g.score?.duration === 'REGULAR' &&
      validScore(g.score.fullTime) && Date.parse(g.utcDate) <= loadedAt - 4 * 3600000)
      .map(g => g.utcDate).sort().at(-1);
    $('v2Freshness').textContent = freshness + (latest ? ' · Последний результат в истории ' + when(latest) : '');
    const count = $('v2MatchCount');
    count.textContent = Math.min(shown.length, limit) + ' из ' + shown.length + ' по фильтрам · ' +
      (archive ? 'Архив сезона' : 'С прогнозом ' + future.filter(r => r.p).length + ' из ' + future.length +
        ' предстоящих · сохранено ' + future.filter(r => r.record).length);
    count.title = archive ? 'Загруженный сезон' : 'Окно от 7 дней назад до 14 дней вперёд';
    for (const row of visible) {
      const g = row.match, card = el('article', undefined, 'v2-match item'); card.dataset.fixtureId = g.id;
      const location = country || g.area?.name || g.competition?.area?.name;
      const header = el('div', undefined, 'v2-card-header');
      const heading = el('div');
      heading.append(el('p', [leagueCode === 'PL' ? 'АПЛ' : league, location].filter(Boolean).join(' · '), 'v2-eyebrow'));
      const title = el('h3', undefined, 'v2-match-teams');
      for (const team of [g.homeTeam, g.awayTeam]) {
        const part = el('span'); const img = crest(team);
        if (img) part.append(img);
        part.append(el('span', team.name));
        title.append(part);
      }
      heading.append(title);
      const star = favoriteButton('match:' + g.id, label(g), () => {
        if (fields.favorite.checked) render();
      }, true);
      header.append(heading, star); card.append(header);
      card.append(el('p', cardTime.format(Date.parse(g.utcDate)) + ' МСК · ' +
        (statusNames[g.status] || 'Статус уточняется'), 'v2-card-time muted'));
      if (validScore(row.result)) card.append(el('p', 'Итог ' + row.result.home + ' : ' + row.result.away +
        (row.record?.state === 'settled' ? ' · ' + verdict(row) : ''), 'v2-match-score'));
      if (row.p) {
        const summary = el('div', undefined, 'v2-forecast-summary');
        summary.append(probabilityBar(row.p));
        const score = row.markets?.score;
        const line = el('p', undefined, 'v2-score-summary muted');
        line.append(el('span', score ? 'Прогноз счёта' : 'Прогноз счёта недоступен'));
        if (score) line.append(el('strong', score));
        if (row.record) {
          const saved = el('span', 'Сохранено', 'v2-saved'); saved.title = 'Сохранено до игры';
          line.append(saved);
        }
        summary.append(line); card.append(summary);
        const best = row.markets?.best;
        if (best && row.state === 'upcoming' && Date.parse(g.utcDate) > Date.now()) {
          const pick = el('div', undefined, 'v2-best-market');
          const title = el('div'); title.append(el('span','Наибольшая вероятность','muted'),el('strong',best.label));
          const value = el('div'); value.append(el('strong',marketPercent(best.p)),
            el('span','Риск · ' + risks[best.risk].toLocaleLowerCase('ru'),'v2-risk v2-risk-' + best.risk));
          pick.append(title,value); card.append(pick);
        }
      } else card.append(el('p', row.message, 'muted small'));
      const active = selected === g.id;
      const button = el('button', active ? 'Свернуть прогноз' : 'Показать прогноз', 'v2-toggle');
      button.type = 'button'; button.setAttribute('aria-expanded', String(active));
      button.onclick = () => {
        closeSelection();
        if (!active) {
          selected = g.id; category = row.p ? 'outcomes' : 'history';
          if (document.body.classList.contains('owner-mode') && scheduled(g) && Date.parse(g.utcDate) > Date.now()) {
            initialized = g.id; openOwner(g);
          }
        }
        render();
        target.querySelector('[data-fixture-id="' + g.id + '"] .v2-toggle')?.focus({preventScroll:true});
      };
      card.append(button); target.append(card);
      if (active) showDetails(row, card, button);
    }
    if (!shown.length) target.append(el('p', 'Матчей по выбранным условиям нет.', 'v2-empty'));
    if (shown.length > limit) { const more = el('button', 'Показать ещё'); more.onclick = () => { limit += 20; render(); }; target.append(more); }
  }

  const filterChanged = () => { limit = 20; render(); };
  Object.values(fields).forEach(f => f.addEventListener('input', filterChanged));
  window.addEventListener('footpredict:quality-loaded', reconcile);
  dispose = () => {
    Object.values(fields).forEach(f => f.removeEventListener('input', filterChanged));
    window.removeEventListener('footpredict:quality-loaded', reconcile);
    closeSelection();
  };
  reconcile();
  if (deepLink) $('v2MatchDetail')?.scrollIntoView({block:'nearest'});
}

if (typeof document !== 'undefined') {
  const theme = $('v2Theme');
  const panels = [$('v2Overview'), $('v2CalendarSettings')].filter(Boolean);
  if (panels.length) {
    const desktop = matchMedia('(min-width: 701px)');
    const adapt = () => { panels.forEach(panel => { panel.open = desktop.matches; }); };
    adapt();
    desktop.addEventListener('change', adapt);
  }
  function setTheme(value) {
    const light = value === 'light';
    document.documentElement.dataset.theme = light ? 'light' : 'dark';
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', light?'#f3f7f4':'#0b1110');
    theme?.setAttribute('aria-pressed', String(light));
    theme?.setAttribute('aria-label', 'Светлая тема');
  }
  let chosen = 'dark';
  try { chosen = localStorage.getItem('footpredict:theme'); } catch { /* Optional storage. */ }
  setTheme(chosen);
  theme?.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    setTheme(next); try { localStorage.setItem('footpredict:theme', next); } catch {}
  });
}
