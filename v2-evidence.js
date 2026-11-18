import {validProbabilities, topOutcome} from './forecast-display.js';

export const MODEL = 'poisson-1.0.0';
const HOUR = 3600000;
const id = n => Number.isSafeInteger(n) && n > 0;
const scoreOk = s => s && [s.home, s.away].every(v => Number.isInteger(v) && v >= 0 && v <= 30);
const actual = s => s.home > s.away ? 0 : s.home === s.away ? 1 : 2;

function records(packet, source) {
  if (source === 'nightly') return (packet?.nightly?.fixtures || []).map(f => {
    const key = f.primaryModel === 'candidate' ? 'candidate' : 'v1';
    return {
    fixtureId: f.fixtureId, homeId: f.homeId, awayId: f.awayId, league: f.league, kickoff: f.kickoff,
    savedAt: f.savedAt, state: f.state, probabilities: f.models?.[key]?.probabilities,
    modelVersion: f.models?.[key]?.modelVersion, score: f.latest?.score, checkedAt: f.latest?.checkedAt,
    stats: key === 'v1' ? f.stats : null, source, lead: 24 * HOUR
    };
  });
  return (packet?.fixtures || []).flatMap(f => f.v1 ? [{...f.v1, source, lead: 5 * 60000}] : []);
}

function verified(r, now) {
  const kickoff = Date.parse(r.kickoff), saved = Date.parse(r.savedAt);
  const knownModel = r.modelVersion === MODEL || (r.source === 'nightly' &&
    /^ensemble-candidate-\d{4}-\d{2}-\d{2}-[0-9a-f]{12}$/.test(r.modelVersion));
  return knownModel && validProbabilities(r.probabilities) &&
    [r.fixtureId, r.homeId, r.awayId].every(id) && r.homeId !== r.awayId &&
    typeof r.league === 'string' && Number.isFinite(kickoff) && saved <= now &&
    kickoff - saved >= r.lead && (r.state === 'pending' || (r.state === 'settled' &&
    scoreOk(r.score) && Date.parse(r.checkedAt) >= kickoff + 4 * HOUR && Date.parse(r.checkedAt) <= now));
}

export function getRecord(packet, match, league, now = Date.now()) {
  for (const source of ['nightly', 'archive']) {
    const found = records(packet, source).filter(r => r.fixtureId === match.id);
    if (!found.length) continue;
    const r = found[0];
    if (found.length !== 1 || !verified(r, now) || r.league !== league ||
        ['POSTPONED','CANCELLED','SUSPENDED','AWARDED'].includes(match.status) ||
        r.homeId !== match.homeTeam.id || r.awayId !== match.awayTeam.id ||
        Date.parse(r.kickoff) !== Date.parse(match.utcDate)) continue;
    return r;
  }
  return null;
}

const labels = {high: 'Высокая', medium: 'Средняя', low: 'Низкая', insufficient: 'Недостаточно данных'};
export function assessConfidence(packet, {match, league, stats, p, asOf, source = 'archive'}) {
  const result = (level, explanation) => ({level, label: labels[level], explanation});
  const cutoff = Math.min(asOf, Date.parse(match.utcDate));
  if (!Number.isFinite(cutoff) || !validProbabilities(p) || !stats ||
      ![stats.homeCount, stats.awayCount].every(n => Number.isSafeInteger(n) && n >= 5))
    return result('insufficient', 'Для оценки надёжности нужны подтверждённые выборки от пяти домашних и гостевых игр.');
  const top = topOutcome(p);
  if (top === null) return result('insufficient', 'Два исхода имеют одинаковый максимум.');
  const bin = v => Math.min(9, Math.floor(v * 10));
  const sample = new Map();
  for (const r of records(packet, source)) {
    if (r.fixtureId === match.id || r.modelVersion !== MODEL || r.league !== league || r.state !== 'settled' ||
        !verified(r, cutoff) || Date.parse(r.checkedAt) >= cutoff ||
        topOutcome(r.probabilities) !== top || bin(r.probabilities[top]) !== bin(p[top])) continue;
    sample.set(r.fixtureId, r);
  }
  // Use one deterministic outcome per UTC matchday to avoid counting
  // simultaneous fixtures as independent evidence. Wilson interval is descriptive.
  const calendar = new Map();
  for (const r of [...sample.values()].sort((a, b) => a.fixtureId - b.fixtureId)) {
    const day = r.kickoff.slice(0, 10);
    if (!calendar.has(day)) calendar.set(day, r);
  }
  const n = calendar.size;
  if (n < 25) return result('insufficient', 'Сопоставимых проверенных игровых дней ' + n + ' из необходимых 25.');
  const rows = [...calendar.values()], rate = rows.filter(r => actual(r.score) === top).length / n;
  const expected = rows.reduce((s, r) => s + r.probabilities[top], 0) / n;
  const z = 1.96, den = 1 + z * z / n;
  const center = (rate + z * z / (2 * n)) / den;
  const half = z * Math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / den;
  const info = n + ' игровых дней в том же диапазоне. Попадания ' + Math.round(rate * 100) +
    '%, средняя оценка ' + Math.round(expected * 100) + '%. Интервал Wilson ' +
    Math.round((center - half) * 100) + '–' + Math.round((center + half) * 100) + '%.';
  if (expected < center - half || expected > center + half) return result('low', info);
  if (Math.min(stats.homeCount, stats.awayCount) >= 12 && half <= .10 &&
      Math.abs(rate - expected) <= .05 && center - half >= .50) return result('high', info);
  return result('medium', info);
}
