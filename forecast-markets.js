import {validProbabilities} from './forecast-display.js';

const MAX_GOALS = 20;
const OUTCOME_KEYS = ['home', 'draw', 'away'];
const ODDS_KEYS = ['homeWin', 'draw', 'awayWin'];
export const marketGroups = [
  ['outcomes', 'Исходы и двойной шанс'],
  ['totals', 'Тоталы голов'],
  ['teamGoals', 'Обе забьют и индивидуальные тоталы'],
  ['handicap', 'Форы'],
  ['scores', 'Точный счёт']
];

function poisson(rate) {
  const probabilities = [Math.exp(-rate)];
  for (let i = 1; i <= MAX_GOALS; i++) probabilities.push(probabilities[i - 1] * (rate / i));
  return probabilities;
}

export function predictScore(stats) {
  const {homeScored, homeConceded, awayScored, awayConceded, leagueHome, leagueAway} = stats || {};
  if (![homeScored, homeConceded, awayScored, awayConceded].every(v => Number.isFinite(v) && v >= 0) ||
      ![leagueHome, leagueAway].every(v => Number.isFinite(v) && v > 0))
    throw new Error('Для прогноза счёта нужны проверенные средние голы команд и лиги.');
  const clamp = v => Math.max(.05, Math.min(5, v));
  const homeGoals = clamp(homeScored * awayConceded / leagueHome);
  const awayGoals = clamp(awayScored * homeConceded / leagueAway);
  const home = poisson(homeGoals), away = poisson(awayGoals);
  const scores = [], sums = [0, 0, 0];
  let best;
  for (let h = 0; h <= MAX_GOALS; h++) {
    for (let a = 0; a <= MAX_GOALS; a++) {
      const mass = home[h] * away[a], cell = {home:h, away:a, mass};
      scores.push(cell);
      sums[h > a ? 0 : h === a ? 1 : 2] += mass;
      if (!best || mass > best.mass) best = cell;
    }
  }
  const total = sums.reduce((sum, value) => sum + value, 0);
  return {prob:sums.map(v => v / total), homeGoals, awayGoals, score:best.home + ' : ' + best.away, scores, total};
}

const line = n => String(n).replace('.', ',');
const signed = n => (n > 0 ? '+' : n < 0 ? '−' : '') + line(Math.abs(n));
export const riskLevel = p => p >= .85 ? 'low' : p >= .70 ? 'medium' : 'high';

export function forecastMarkets(prob, stats, calculation = null) {
  if (!validProbabilities(prob)) return null;
  const market = (id, label, probability) => {
    const p = Math.max(0, Math.min(1, probability));
    return {id, label, p, risk:riskLevel(p)};
  };
  const groups = {
    outcomes:[
      ...['П1 · победа хозяев', 'Х · ничья', 'П2 · победа гостей'].map((label, i) => market(OUTCOME_KEYS[i], label, prob[i])),
      market('home-draw', '1Х · хозяева не проиграют', prob[0] + prob[1]),
      market('away-draw', 'Х2 · гости не проиграют', prob[1] + prob[2]),
      market('not-draw', '12 · любая победа', prob[0] + prob[2])
    ],
    totals:[], teamGoals:[], handicap:[], scores:[]
  };
  let score = null;
  try { calculation ??= predictScore(stats); } catch { /* Scores require proven model inputs. */ }
  // Archived coefficients may be absent. Only project a score distribution
  // when its outcome probabilities reproduce those shown on the card.
  if (calculation && calculation.prob.every((v, i) => Math.abs(v - prob[i]) <= 1e-8)) {
    score = calculation.score;
    const chance = test => calculation.scores.reduce((sum, s) =>
      sum + (test(s.home, s.away) ? s.mass : 0), 0) / calculation.total;
    const overUnder = (list, prefix, name, lines, value) => {
      for (const t of lines) {
        const over = chance((h, a) => value(h, a) > t);
        list.push(market(prefix + '-over-' + t, name + 'Б ' + line(t), over),
          market(prefix + '-under-' + t, name + 'М ' + line(t), 1 - over));
      }
    };
    overUnder(groups.totals, 'total', 'Т', [.5, 1.5, 2.5, 3.5], (h, a) => h + a);
    const both = chance((h, a) => h > 0 && a > 0);
    groups.teamGoals.push(market('both-yes', 'Обе забьют · да', both), market('both-no', 'Обе забьют · нет', 1 - both));
    overUnder(groups.teamGoals, 'home-total', 'Хозяева · ИТ', [.5, 1.5, 2.5], h => h);
    overUnder(groups.teamGoals, 'away-total', 'Гости · ИТ', [.5, 1.5, 2.5], (_, a) => a);
    for (const f of [-1.5, -.5, .5, 1.5]) {
      groups.handicap.push(market('home-handicap-' + f, 'Ф1 (' + signed(f) + ')', chance((h, a) => h + f > a)),
        market('away-handicap-' + f, 'Ф2 (' + signed(f) + ')', chance((h, a) => a + f > h)));
    }
    const scores = calculation.scores.slice().sort((a, b) => b.mass - a.mass).slice(0, 7);
    groups.scores = scores.map(s => market('score-' + s.home + '-' + s.away, s.home + ' : ' + s.away,
      s.mass / calculation.total));
  }
  Object.values(groups).forEach(list => list.sort((a, b) => b.p - a.p));
  const all = Object.values(groups).flat().sort((a, b) => b.p - a.p);
  return {groups, all, best:all[0], score};
}

export function marketQuote(market, odds) {
  const index = OUTCOME_KEYS.indexOf(market.id);
  if (index < 0) return null;
  const price = odds?.[ODDS_KEYS[index]];
  if (!Number.isFinite(price) || price <= 1) return null;
  return {price, expectedReturn:market.p * price - 1};
}
