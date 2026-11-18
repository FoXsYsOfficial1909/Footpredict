import {scoreQuality, summarizeQuality} from './metrics.mjs';
import {NIGHTLY_SETTINGS_SHA} from './nightly-model.mjs';

export const MODEL_RELEASE = Object.freeze({
  protocol: 'pl-nightly-promotion-1',
  season: 2026,
  firstRound: 14,
  firstFixture: 560672,
  fixtureCount: 190,
  opensAt: '2026-11-19T00:00:00Z',
  decisionAt: '2027-05-03T04:50:00Z',
  calendarSha256: '228df2b95a24eddce8562a0b7acd28859e556d807e50cfed7ad4a3b1188af175',
  trainingCodeSha256: '934eeebbabd2b60effb47d65859b1b2adbb16fcfa2c036730ec51cc21390ed37',
  settingsSha256: NIGHTLY_SETTINGS_SHA,
  minimumCoverage: .90,
  minimumRounds: 18,
  minimumImprovement: .005,
  resamples: 2000,
  seed: 261118,
  primaryMetric: 'logLoss'
});
const keys = ['v1', 'candidate', 'base'];
export const V1_RELEASE_MODEL = 'poisson-1.0.0';
const BASE = 'league-frequencies-v1';
const HOUR = 3600000;
const opens = Date.parse(MODEL_RELEASE.opensAt);
const closes = Date.parse(MODEL_RELEASE.decisionAt);

function validVector(p) {
  return Array.isArray(p) && p.length === 3 &&
    p.every(v => Number.isFinite(v) && v >= 0 && v <= 1) &&
    Math.abs(p.reduce((a,b) => a + b, 0) - 1) < 1e-8;
}

export function eligibleRecipe(candidate) {
  return candidate?.codeSha256 === MODEL_RELEASE.trainingCodeSha256 &&
    candidate.settingsSha256 === MODEL_RELEASE.settingsSha256 &&
    /^ensemble-candidate-\d{4}-\d{2}-\d{2}-[0-9a-f]{12}$/.test(candidate.modelVersion || '') &&
    /^[0-9a-f]{64}$/.test(candidate.modelSha256 || '') &&
    Array.isArray(candidate.unseenTeams) && candidate.unseenTeams.length === 0;
}

function eligible(row, now) {
  const saved = Date.parse(row.savedAt), kickoff = Date.parse(row.kickoff);
  const checked = Date.parse(row.checkedAt), generated = Date.parse(row.models?.candidate?.trainingGeneratedAt);
  return row.league === 'PL' && row.season === MODEL_RELEASE.season &&
    row.state === 'settled' && row.observedKickoff === row.kickoff &&
    saved >= opens && kickoff - saved >= 24 * HOUR && checked >= kickoff + 4 * HOUR && checked <= Math.min(now, closes) &&
    generated <= saved && saved - generated <= 36 * HOUR &&
    row.score && [row.score.home, row.score.away].every(n => Number.isInteger(n) && n >= 0 && n <= 30) &&
    row.models?.v1?.modelVersion === V1_RELEASE_MODEL && row.models?.base?.modelVersion === BASE &&
    eligibleRecipe(row.models.candidate) && keys.every(k => validVector(row.models[k]?.probabilities));
}

function upperBounds(rounds) {
  let seed = MODEL_RELEASE.seed;
  const clusters = [...rounds.values()];
  const againstV1 = [], againstBase = [];
  for (let i = 0; i < MODEL_RELEASE.resamples; i++) {
    let sumV1 = 0, sumBase = 0, count = 0;
    for (let j = 0; j < clusters.length; j++) {
      seed = (Math.imul(1664525, seed) + 1013904223) >>> 0;
      const row = clusters[Math.floor(seed / 2**32 * clusters.length)];
      sumV1 += row.deltaV1; sumBase += row.deltaBase; count += row.n;
    }
    againstV1.push(sumV1 / count); againstBase.push(sumBase / count);
  }
  const at = Math.ceil(.975 * MODEL_RELEASE.resamples) - 1;
  return {v1: againstV1.sort((a,b) => a-b)[at], base: againstBase.sort((a,b) => a-b)[at]};
}

export function evaluateRelease(rows, now) {
  const start = MODEL_RELEASE.firstFixture;
  const members = rows.filter(r => Number.isSafeInteger(r.fixtureId) && r.fixtureId >= start &&
    r.fixtureId < start + MODEL_RELEASE.fixtureCount && Date.parse(r.savedAt) >= opens);
  const frequencies = new Map();
  for (const r of members) frequencies.set(r.fixtureId, (frequencies.get(r.fixtureId) || 0) + 1);
  const paired = members.filter(r => frequencies.get(r.fixtureId) === 1 && eligible(r, now))
    .sort((a,b) => a.fixtureId - b.fixtureId);
  const coverage = paired.length / MODEL_RELEASE.fixtureCount;
  const metrics = Object.fromEntries(keys.map(k => [k, summarizeQuality(paired.map(row =>
    ({metrics:scoreQuality(row.models[k].probabilities, row.score)})))]));
  const rounds = new Map();
  for (const row of paired) {
    const round = Math.floor((row.fixtureId - start) / 10) + MODEL_RELEASE.firstRound;
    if (!rounds.has(round)) rounds.set(round, {n:0, deltaV1:0, deltaBase:0});
    const cell = rounds.get(round);
    const loss = k => scoreQuality(row.models[k].probabilities, row.score).logLoss;
    const candidate = loss('candidate');
    cell.n++; cell.deltaV1 += candidate - loss('v1'); cell.deltaBase += candidate - loss('base');
  }
  const decision = {protocol:MODEL_RELEASE.protocol, status:'collecting',
    calendar:MODEL_RELEASE.fixtureCount, registered:frequencies.size, settled:paired.length, coverage, metrics,
    rounds:rounds.size, upperLogLossDifference:null, evaluatedAt:null, reason:'waiting_for_fixed_review'};
  if (now < closes) return decision;
  decision.evaluatedAt = new Date(now).toISOString();
  decision.status = 'rejected';
  if (coverage < MODEL_RELEASE.minimumCoverage || rounds.size < MODEL_RELEASE.minimumRounds) {
    decision.reason = 'insufficient_coverage';
    return decision;
  }
  const upper = upperBounds(rounds);
  decision.upperLogLossDifference = upper;
  const improved = ['v1', 'base'].every(k =>
    metrics[k].logLoss - metrics.candidate.logLoss >= MODEL_RELEASE.minimumImprovement &&
    upper[k] < 0 && metrics.candidate.brier <= metrics[k].brier);
  decision.status = improved ? 'approved' : 'rejected';
  decision.reason = improved ? 'lower_probability_error' : 'advantage_not_established';
  return decision;
}

const releaseKey = 'release:'+MODEL_RELEASE.protocol;
async function savedDecision(db) {
  const row = await db.prepare('SELECT value FROM automation_meta WHERE key=?1').bind(releaseKey).first();
  return row ? JSON.parse(row.value) : null;
}

export function releaseApproved(decision) {
  return decision?.protocol === MODEL_RELEASE.protocol && decision.status === 'approved' &&
    decision.reason === 'lower_probability_error' && Date.parse(decision.evaluatedAt) >= closes &&
    decision.coverage >= MODEL_RELEASE.minimumCoverage;
}

export async function releaseStatus(db, records, now, paused = false) {
  const decision = now >= closes ? await savedDecision(db) : null;
  const progress = decision || evaluateRelease(records, Math.min(now, closes - 1));
  return {
    schedule: MODEL_RELEASE, ...progress,
    status: !decision && now >= closes ? 'awaiting_review' : progress.status,
    useCandidateForNewForecasts: releaseApproved(decision) && !paused,
    paused
  };
}

// Only the scheduled review can persist a decision. The cohort is evaluated
// once using observations timestamped by the fixed deadline.
export async function checkRelease(db, now) {
  if (now < closes) return null;
  const existing = await savedDecision(db);
  if (existing) return existing;
  const start = MODEL_RELEASE.firstFixture, end = start + MODEL_RELEASE.fixtureCount;
  const r = await db.prepare(`SELECT fixture_id AS fixtureId,kickoff_at AS kickoff,saved_at AS savedAt,
    json_extract(payload,'$.models') AS models, json_extract(payload,'$.year') AS season,
    json_extract(payload,'$.league') AS league,
    (SELECT payload FROM nightly_results WHERE fixture_id=p.fixture_id AND checked_at<=?4 ORDER BY seq DESC LIMIT 1) AS latest
    FROM nightly_predictions p WHERE fixture_id>=?1 AND fixture_id<?2 AND saved_at>=?3
    AND protocol='nightly-live-1' AND saved_at<=?4 ORDER BY fixture_id`)
    .bind(start,end,MODEL_RELEASE.opensAt,new Date(Math.min(now,closes)).toISOString()).all();
  const records = r.results.map(row => {
    const latest = row.latest ? JSON.parse(row.latest) : {};
    return {...row, models:JSON.parse(row.models), state:latest.status, checkedAt:latest.checkedAt,
      observedKickoff:latest.kickoff, score:latest.score};
  });
  const decision = evaluateRelease(records, now);
  await db.prepare('INSERT INTO automation_meta (key,value) VALUES (?1,?2) ON CONFLICT(key) DO NOTHING')
    .bind(releaseKey, JSON.stringify(decision)).run();
  return await savedDecision(db);
}

export async function publishedModel(db, candidate, paused, now) {
  if (now < closes || paused || !eligibleRecipe(candidate)) return 'v1';
  try { return releaseApproved(await savedDecision(db)) ? 'candidate' : 'v1'; }
  catch { return 'v1'; }
}
