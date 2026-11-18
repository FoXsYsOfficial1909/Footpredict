import {rounded, topOutcome} from './forecast-display.js';
import {MODEL, getRecord, assessConfidence} from './v2-evidence.js';
import {peekQuality} from './quality-data-v2.js';
export {rounded} from './forecast-display.js';

const DAY = 86400000;
const $ = id => document.getElementById(id);
const outcomes = ['П1', 'Х', 'П2'];
const statusNames = {FINISHED:'Завершён', IN_PLAY:'Идёт матч', PAUSED:'Перерыв',
  EXTRA_TIME:'Дополнительное время', PENALTY_SHOOTOUT:'Серия пенальти', TIMED:'По расписанию',
  SCHEDULED:'Запланирован', POSTPONED:'Перенесён', CANCELLED:'Отменён', SUSPENDED:'Приостановлен',
  AWARDED:'Технический результат'};
const live = new Set(['IN_PLAY', 'PAUSED', 'EXTRA_TIME', 'PENALTY_SHOOTOUT']);
const scheduled = g => ['TIMED', 'SCHEDULED'].includes(g.status);
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
  return result.sort((a, b) => (filters.sort === 'probability' ? Math.max(...(b.p || [-1])) - Math.max(...(a.p || [-1])) : 0)
    || Date.parse(a.match.utcDate) - Date.parse(b.match.utcDate) || a.match.id - b.match.id);
}

function readFavorites() {
  try {
    const list = JSON.parse(localStorage.getItem(favoriteKey) || '[]');
    return Array.isArray(list) ? list.filter(v => /^(team|match):\d+$/.test(v)).slice(-500) : [];
  } catch { return []; }
}

function favoriteButton(key, title, onChange) {
  const button = el('button', undefined, 'favorite secondary');
  button.type = 'button';
  const sync = () => {
    const saved = readFavorites().includes(key);
    button.setAttribute('aria-pressed', String(saved));
    button.setAttribute('aria-label', (saved ? 'Убрать из избранного · ' : 'В избранное · ') + title);
    button.textContent = saved ? '★ В избранном' : '☆ В избранное';
  };
  sync();
  button.onclick = () => {
    let list = readFavorites();
    list = list.includes(key) ? list.filter(v => v !== key) : [...list, key].slice(-500);
    try { localStorage.setItem(favoriteKey, JSON.stringify(list)); sync(); onChange(); }
    catch { button.textContent = 'Хранилище недоступно'; }
  };
  return button;
}

function probabilityBar(p) {
  const wrap = el('div', undefined, 'v2-distribution'), bar = el('div', undefined, 'v2-segments');
  const labels = el('div', undefined, 'v2-prob-labels');
  bar.setAttribute('aria-hidden', 'true');
  rounded(p).forEach((value, i) => {
    const segment = el('span'); segment.style.width = p[i] * 100 + '%'; bar.append(segment);
    labels.append(el('span', outcomes[i] + ' ' + value.replace('.', ',')));
  });
  wrap.append(bar, labels);
  return wrap;
}

function crest(team) {
  try {
    const url = new URL(team.crest);
    if (url.origin !== 'https://crests.football-data.org') return null;
    const img = el('img');
    img.src = url.href; img.width = 28; img.height = 28; img.alt = ''; img.loading = 'lazy';
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

function renderDetail(row, history, onFavorite) {
  const g = row.match, root = $('v2MatchDetail');
  root.hidden = false;
  root.replaceChildren(el('h2', label(g)), el('p', row.league + ' · ' + when(g.utcDate) + ' · ' +
    (statusNames[g.status] || 'Статус уточняется'), 'muted'));
  if (row.p) {
    root.append(probabilityBar(row.p), el('p', sourceText(row) + ' · ' + (row.record?.modelVersion || MODEL), 'muted'));
    const best = topOutcome(row.p);
    root.append(el('p', best === null ? 'У исходов равный максимум.' : 'Самый вероятный исход · ' + outcomes[best]));
    root.append(el('p', 'Уверенность · ' + row.assessment.label + '. ' + row.assessment.explanation, 'v2-confidence'));
  } else root.append(el('p', row.message, 'muted'));
  if (validScore(row.result)) root.append(el('p', 'Основное время · ' + row.result.home + ' : ' + row.result.away +
    (row.record?.state === 'settled' ? ' · ' + verdict(row) : ''), 'v2-match-score'));
  if (row.record?.source === 'nightly') {
    const link = el('a', 'Первоначальный расчёт и использованные матчи');
    link.href = 'https://footpredict-api.wzjp5747md.workers.dev/v1/candidate-comparison/' + g.id + '/evidence';
    link.target = '_blank'; link.rel = 'noopener noreferrer'; root.append(link);
  }
  if (row.record && row.record.modelVersion !== MODEL) {
    root.append(el('h3','Почему такой прогноз'),el('p',
      'Ансамбль объединяет обученный Пуассон с оценками Elo. На расчёт влияют результаты прошлых матчей, силы соперников и домашнее преимущество.'));
  }
  if (row.stats && row.p) {
    root.append(el('h3', 'Почему такой прогноз'), el('p',
      'Пуассон сопоставляет результативность хозяев дома и гостей на выезде со средними голами лиги. ' +
      'У недавних игр больше вес. Сглаживание учитывает размер выборки.'));
    const facts = el('dl', undefined, 'v2-facts');
    for (const [title, value] of [['Хозяева забивают дома', row.stats.homeScored], ['Хозяева пропускают дома', row.stats.homeConceded],
      ['Гости забивают на выезде', row.stats.awayScored], ['Гости пропускают на выезде', row.stats.awayConceded]]) {
      if (!Number.isFinite(value)) continue;
      const item = el('div'); item.append(el('dt', title), el('dd', value.toFixed(2).replace('.', ','))); facts.append(item);
    }
    root.append(facts, el('p', 'Игр в расчёте · хозяева дома ' + row.stats.homeCount + ', гости на выезде ' + row.stats.awayCount + '.', 'muted'));
  }
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
}

function verdict(row) {
  const best = topOutcome(row.p);
  if (best === null) return 'равный максимум';
  return best === actual(row.result) ? 'прогноз совпал' : 'прогноз не совпал';
}

export function clearExplorer() {
  dispose();
  dispose = () => {};
  $('v2MatchDetail').hidden = true; $('v2MatchDetail').replaceChildren();
}

export function renderExplorer({all, history, league, leagueCode, country, preview, open, freshness, archive = false, targetId}) {
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
  let rows = [], limit = 20, selected = null;
  function reconcile() {
    const packet = peekQuality(), now = Date.now();
    rows = [...calendar.values()].map(match => {
      const state = status(match, now), invalid = conflicts.has(match.id);
      const record = invalid ? null : getRecord(packet, match, leagueCode, now);
      const current = state === 'upcoming' && !invalid ? previews.get(match.id) : null;
      const p = record?.probabilities || current?.p;
      const stats = record ? record.stats : current?.stats;
      const asOf = record ? Date.parse(record.savedAt) : Math.min(loadedAt, Date.parse(match.utcDate));
      const source = record?.source || (leagueCode === 'PL' ? 'nightly' : 'archive');
      const assessment = assessConfidence(packet, {match, league: leagueCode, p, stats, asOf, source});
      const result = record?.state === 'settled' ? record.score : state === 'finished' ? regulation(match) : null;
      let message = 'Для расчёта недостаточно проверенной истории.';
      if (invalid) message = 'В расписании расходятся сведения об этом матче.';
      else if (state === 'changed') message = 'Проверь статус и время начала.';
      else if (state !== 'upcoming') message = 'В загруженном журнале нет подтверждённого предматчевого прогноза.';
      return {match, state, league, leagueCode, p, stats, asOf, record, result, message, assessment, confidence:assessment.level};
    });
    if (selected) {
      const row = rows.find(r => r.match.id === selected);
      if (row) {
        renderDetail(row, history, render);
        if (row.record) window.dispatchEvent(new CustomEvent('footpredict:visible-record',{detail:row.record}));
      }
    }
    render();
  }

  function detail(row, focus = true) {
    selected = row.match.id;
    renderDetail(row, history, render);
    if (!focus) return;
    $('v2MatchDetail').focus({preventScroll:true}); $('v2MatchDetail').scrollIntoView({block:'start'});
  }
  function render() {
    const filter = Object.fromEntries(Object.entries(fields).map(([key, n]) => [key, key === 'favorite' ? n.checked : n.value]));
    const shown = selectMatches(rows, filter, Date.now(), readFavorites());
    target.replaceChildren();
    const future = rows.filter(r => r.state === 'upcoming');
    $('v2Available').textContent = rows.length;
    $('v2Today').textContent = rows.filter(r => r.p && dayFormat.format(Date.parse(r.match.utcDate)) === dayFormat.format(Date.now())).length;
    const latest = history.filter(g => g.status === 'FINISHED' && g.score?.duration === 'REGULAR' &&
      validScore(g.score.fullTime) && Date.parse(g.utcDate) <= loadedAt - 4 * 3600000)
      .map(g => g.utcDate).sort().at(-1);
    $('v2Freshness').textContent = freshness + (latest ? ' · Последний результат в истории ' + when(latest) : '');
    $('v2MatchCount').textContent = 'Показано ' + Math.min(shown.length, limit) + ' из ' + shown.length + ' по фильтрам. ' +
      (archive ? 'Архив сезона.' : 'Окно от 7 дней назад до 14 дней вперёд.') +
      ' С прогнозом ' + future.filter(r => r.p).length + ' из ' + future.length + ' предстоящих, сохранено ' +
      future.filter(r => r.record).length + '.';
    for (const row of shown.slice(0, limit)) {
      const g = row.match, card = el('article', undefined, 'v2-match item'); card.dataset.fixtureId = g.id;
      const location = country || g.area?.name || g.competition?.area?.name;
      card.append(el('p', [league, location].filter(Boolean).join(' · '), 'v2-eyebrow'));
      const title = el('h3', label(g)), logos = el('span', undefined, 'v2-crests');
      for (const team of [g.homeTeam, g.awayTeam]) { const img = crest(team); if (img) logos.append(img); }
      if (logos.childNodes.length) title.prepend(logos);
      card.append(title, el('p', when(g.utcDate) + ' · ' + (statusNames[g.status] || 'Статус уточняется'), 'muted'));
      if (row.p) {
        const best = topOutcome(row.p);
        card.append(probabilityBar(row.p), el('p', best === null ? 'Равный максимум' : 'Самый вероятный исход · ' + outcomes[best]),
          el('p', sourceText(row), 'muted small'), el('p', 'Уверенность · ' + row.assessment.label, 'v2-confidence'));
      } else card.append(el('p', row.message, 'muted'));
      if (validScore(row.result)) card.append(el('p', row.result.home + ' : ' + row.result.away +
        (row.record?.state === 'settled' ? ' · ' + verdict(row) : ''), 'v2-match-score'));
      const actions = el('div', undefined, 'v2-actions'), button = el('button', 'Подробный анализ'); button.type = 'button';
      button.onclick = async () => {
        if (row.state === 'upcoming' && scheduled(g) && Date.parse(g.utcDate) > Date.now()) await open(g, row);
        else window.dispatchEvent(new CustomEvent('footpredict:readonly-detail'));
        detail(row);
      };
      actions.append(button, favoriteButton('match:' + g.id, label(g), render));
      card.append(actions); target.append(card);
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
  };
  reconcile();
  const deepLink = targetId && rows.find(r => r.match.id === targetId);
  if (deepLink) {
    const match = deepLink.match;
    fields.period.value = 'all';
    if (status(match, Date.now()) === 'finished') fields.status.value = 'finished';
    render(); detail(deepLink);
  }
}

if (typeof document !== 'undefined') {
  const theme = $('v2Theme');
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
