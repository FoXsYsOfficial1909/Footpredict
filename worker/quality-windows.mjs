import {scoreQuality, summarizeQuality} from './metrics.mjs';

const DAY = 86400000;
const validVector = p => Array.isArray(p) && p.length === 3 &&
  p.every(v => Number.isFinite(v) && v >= 0 && v <= 1) &&
  Math.abs(p.reduce((a, b) => a + b, 0) - 1) < 1e-8;

function calibration(rows, key) {
  const bins = Array.from({length: 3}, () => Array.from({length: 5}, () => ({n: 0, sum: 0, hits: 0})));
  for (const row of rows) {
    row.models[key].forEach((p, k) => {
      const cell = bins[k][Math.min(4, Math.floor(p * 5))];
      cell.n++;
      cell.sum += p;
      cell.hits += Number(k === row.scores[key].actual);
    });
  }
  return bins.map(cells => cells.map(({n, sum, hits}) => ({
    n, predicted: n ? sum / n : null, observed: n ? hits / n : null
  })));
}

function dailyStatistics(rows, keys) {
  const days = new Map();
  for (const {kickoff, models, scores} of rows) {
    const date = kickoff.slice(0, 10);
    if (!days.has(date)) days.set(date, {
      date, n: 0, losses: Object.fromEntries(keys.map(key => [key, 0])),
      sums: [0, 0, 0, 0], counts: [0, 0, 0, 0]
    });
    const day = days.get(date);
    day.n++;
    for (const key of keys) day.losses[key] += scores[key].logLoss;
    const {actual, top} = scores.v1;
    for (const [i, k] of [0, 1, 2, top].entries()) {
      if (k === null) continue;
      day.sums[i] += Number(actual === k) - models.v1[k];
      day.counts[i]++;
    }
  }
  const ordered = [...days.values()].sort((a, b) => a.date.localeCompare(b.date));
  const diagnostics = [0, 1, 2, 3].map(i => {
    const eligible = ordered.filter(d => d.counts[i] > 0);
    const n = eligible.length;
    // Each UTC day has equal weight. Four planned two-sided comparisons.
    // Daily mean residual is in [-1,1]. Hoeffding assumes independent days.
    const mean = n ? eligible.reduce((s, d) => s + d.sums[i] / d.counts[i], 0) / n : null;
    const radius = n ? Math.sqrt(2 * Math.log(8 / .05) / n) : null;
    const lo = n ? Math.max(-1, mean - radius) : null;
    const hi = n ? Math.min(1, mean + radius) : null;
    return {days: n, n: eligible.reduce((s, d) => s + d.counts[i], 0), mean, lo, hi,
      signal: n >= 30 && (lo > 0 || hi < 0)};
  });
  const timeline = [];
  const step = Math.max(1, Math.ceil(ordered.length / 28));
  const sums = Object.fromEntries(keys.map(key => [key, 0]));
  let n = 0;
  for (const [i, day] of ordered.entries()) {
    n += day.n;
    for (const key of keys) sums[key] += day.losses[key];
    if ((i + 1) % step === 0 || i === ordered.length - 1) timeline.push({
      date: day.date, n, loss: Object.fromEntries(keys.map(k => [k, sums[k] / n]))
    });
  }
  return {timeline, diagnostics};
}

export function qualityWindows(records, keys, now) {
  const leagues = [...new Set(records.map(r => r.league).filter(Boolean))].sort();
  const groups = {};
  for (const league of ['all', ...leagues]) {
    const leagueRows = records.filter(r => league === 'all' || league === r.league);
    const periods = {};
    for (const days of [7, 30, 0]) {
      const start = days ? now - days * DAY : -Infinity;
      const rows = leagueRows.filter(r => Date.parse(r.kickoff) >= start && Date.parse(r.kickoff) <= now);
      const counts = {settled: 0, pending: 0, review: 0, excluded: 0};
      const paired = [];
      for (const row of rows) {
        counts[row.state]++;
        if (row.state !== 'settled' || !keys.every(key => validVector(row.models[key]))) continue;
        const scores = Object.fromEntries(keys.map(key => [key, scoreQuality(row.models[key], row.score)]));
        paired.push({...row, scores});
      }
      periods[days] = {archived: rows.length, ...counts, paired: paired.length,
        models: Object.fromEntries(keys.map(key => [key, summarizeQuality(paired.map(r => ({metrics: r.scores[key]})))])),
        calibration: calibration(paired, 'v1'), ...dailyStatistics(paired, keys)};
    }
    groups[league] = periods;
  }
  return {schemaVersion: 1, groups};
}
