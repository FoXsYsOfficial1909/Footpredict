export const GOAL_MARKET_PROTOCOL = 'poisson-goal-markets-1';
export const GOAL_MARKETS = ['over25', 'bothScore'];
const V1 = 'poisson-1.0.0';
const BTTS_TRIAL = Object.freeze({
  protocol: 'pl-btts-calibration-live-1',
  modelVersion: 'pl-goal-calibration-0.1.0',
  // Frozen from docs/research/goal-markets-2026-11-19/calibration.json.
  artifactSha256: '866fc04068d7198ec5482f7738b201b26c2329e4eb17c4464d5ec99d2a2ee3c8',
  intercept: -0.012515245755720812,
  slope: 0.22953207891201705,
});
const BTTS_START = '2026-11-19T19:25:00.000Z';
const BTTS_START_MS = Date.parse(BTTS_START);
const BTTS_FIRST_REVIEW = 100;

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

function bttsProbability(forecast) {
  const logit = p => {
    const clipped = Math.max(1e-12, Math.min(1 - 1e-12, p));
    return Math.log(clipped / (1 - clipped));
  };
  const base = logit(forecast.base.bothScore);
  return 1 / (1 + Math.exp(-(base + BTTS_TRIAL.intercept +
    BTTS_TRIAL.slope * (logit(forecast.model.bothScore) - base))));
}

export function freezeBothScoreCandidate(forecast, savedAt) {
  if (!validSnapshot(forecast) || !(Date.parse(savedAt) >= BTTS_START_MS)) return forecast;
  return {...forecast, bothScoreCandidate: {...BTTS_TRIAL, probability: bttsProbability(forecast)}};
}

function score(rows, kind, market, binCount = 5) {
  let sum = 0, brier = 0, logLoss = 0;
  const bins = Array.from({length: binCount}, () => ({n: 0, expected: 0, hits: 0}));
  for (const {forecast, events} of rows) {
    const y = Number(events[market]), p = forecast[kind][market];
    sum += p;
    brier += 2 * (p - y)**2;
    logLoss -= Math.log(Math.max(1e-12, y ? p : 1 - p));
    const bin = bins[Math.min(bins.length - 1, Math.floor(p * bins.length))];
    bin.n++; bin.expected += p; bin.hits += y;
  }
  const n = rows.length;
  return {meanProbability: n ? sum / n : null, brier: n ? brier / n : null, logLoss: n ? logLoss / n : null,
    calibration: bins.map(b => ({n: b.n, predicted: b.n ? b.expected / b.n : null,
      observed: b.n ? b.hits / b.n : null}))};
}

function confirmedScore(result) {
  return [result?.home, result?.away].every(v => Number.isInteger(v) && v >= 0 && v <= 30);
}

function summarizeBothScoreTrial(records) {
  const counts = {eligible: 0, registered: 0, pending: 0, settled: 0, review: 0, excluded: 0, withoutCandidate: 0};
  const rows = [];
  for (const {forecast, state, result, savedAt} of records) {
    if (!(Date.parse(savedAt) >= BTTS_START_MS)) continue;
    counts.eligible++;
    const candidate = forecast?.bothScoreCandidate;
    if (!candidate) { counts.withoutCandidate++; continue; }
    counts.registered++;
    const validCandidate = validSnapshot(forecast) &&
      Object.entries(BTTS_TRIAL).every(([key, value]) => candidate[key] === value) &&
      Number.isFinite(candidate.probability) && candidate.probability >= 0 && candidate.probability <= 1 &&
      Math.abs(candidate.probability - bttsProbability(forecast)) < 1e-12;
    if (!validCandidate || !['pending','settled','review'].includes(state) ||
        (state === 'settled' && !confirmedScore(result))) {
      counts.excluded++; continue;
    }
    counts[state]++;
    if (state === 'settled') rows.push({forecast: {...forecast, candidate: {bothScore: candidate.probability}},
      events: goalEvents(result.home, result.away)});
  }
  const models = Object.fromEntries(['model','base','candidate'].map(key => [key, score(rows, key, 'bothScore', 10)]));
  const frequency = rows.length ? rows.filter(row => row.events.bothScore).length / rows.length : null;
  return {protocol: BTTS_TRIAL.protocol, modelVersion: BTTS_TRIAL.modelVersion, savedAtFrom: BTTS_START,
    primaryMetric: 'logLoss', firstReviewMatches: BTTS_FIRST_REVIEW,
    reviewReady: counts.settled >= BTTS_FIRST_REVIEW, counts,
    coverage: counts.eligible ? counts.registered / counts.eligible : null, frequency, models};
}

export function summarizeGoalMarkets(records, {includeBothScoreTrial = false} = {}) {
  const counts = {registered: 0, pending: 0, settled: 0, review: 0, excluded: 0, withoutSnapshot: 0};
  const rows = [];
  for (const {forecast, state, result} of records) {
    if (forecast == null) { counts.withoutSnapshot++; continue; }
    counts.registered++;
    if (!validSnapshot(forecast) || (state === 'settled' && !confirmedScore(result)) || !Object.hasOwn(counts, state) ||
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
  return {protocol: GOAL_MARKET_PROTOCOL, league: 'PL', brierScale: [0, 2], counts, markets,
    ...(includeBothScoreTrial ? {bothScoreTrial: summarizeBothScoreTrial(records)} : {})};
}
