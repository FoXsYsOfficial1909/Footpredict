export const GOAL_MARKET_PROTOCOL = 'poisson-goal-markets-1';
export const GOAL_MARKETS = ['over25', 'bothScore'];
const V1 = 'poisson-1.0.0';

export function goalEvents(home, away) {
  return {over25: home + away >= 3, bothScore: home > 0 && away > 0};
}

// Use the same pre-match inputs and weights as the 1X2 league-frequency
// comparator, with one pseudo-observation for each binary outcome.
export function goalMarketSnapshot(prediction, inputs, getWeight) {
  const counts = {over25: 1, bothScore: 1};
  let mass = 2;
  for (const game of inputs) {
    const {home, away} = game.score.fullTime;
    const events = goalEvents(home, away);
    const weight = getWeight(game);
    mass += weight;
    for (const market of GOAL_MARKETS) counts[market] += weight * Number(events[market]);
  }
  return {protocol: GOAL_MARKET_PROTOCOL, modelVersion: V1,
    model: prediction.goalProbabilities, base: {over25: counts.over25 / mass, bothScore: counts.bothScore / mass}};
}

function validSnapshot(record) {
  return record?.protocol === GOAL_MARKET_PROTOCOL && record.modelVersion === V1 &&
    ['model', 'base'].every(kind => GOAL_MARKETS.every(market => {
      const p = record[kind]?.[market];
      return Number.isFinite(p) && p >= 0 && p <= 1;
    }));
}

function score(rows, kind, market) {
  let sum = 0, brier = 0, logLoss = 0;
  const bins = Array.from({length: 5}, () => ({n: 0, expected: 0, hits: 0}));
  for (const {forecast, events} of rows) {
    const y = Number(events[market]), p = forecast[kind][market];
    sum += p;
    brier += 2 * (p - y)**2;
    logLoss -= Math.log(Math.max(1e-12, y ? p : 1 - p));
    const bin = bins[Math.min(4, Math.floor(p * bins.length))];
    bin.n++; bin.expected += p; bin.hits += y;
  }
  const n = rows.length;
  return {meanProbability: n ? sum / n : null, brier: n ? brier / n : null, logLoss: n ? logLoss / n : null,
    calibration: bins.map(b => ({n: b.n, predicted: b.n ? b.expected / b.n : null,
      observed: b.n ? b.hits / b.n : null}))};
}

export function summarizeGoalMarkets(records) {
  const counts = {registered: 0, pending: 0, settled: 0, review: 0, excluded: 0, withoutSnapshot: 0};
  const rows = [];
  for (const {forecast, state, result} of records) {
    if (forecast == null) { counts.withoutSnapshot++; continue; }
    counts.registered++;
    const confirmedScore = [result?.home, result?.away].every(v => Number.isInteger(v) && v >= 0 && v <= 30);
    if (!validSnapshot(forecast) || (state === 'settled' && !confirmedScore) || !Object.hasOwn(counts, state) ||
        state === 'registered' || state === 'withoutSnapshot') {
      counts.excluded++; continue;
    }
    counts[state]++;
    if (state === 'settled') rows.push({forecast, events: goalEvents(result.home, result.away)});
  }
  const markets = Object.fromEntries(GOAL_MARKETS.map(market => [market, {
    frequency: rows.length ? rows.reduce((sum, row) => sum + Number(row.events[market]), 0) / rows.length : null,
    model: score(rows, 'model', market),
    base: score(rows, 'base', market)
  }]));
  return {protocol: GOAL_MARKET_PROTOCOL, league: 'PL', brierScale: [0, 2], counts, markets};
}
