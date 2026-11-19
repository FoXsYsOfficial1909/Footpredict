// Past-only replay of the exact Worker goal forecast and weighted league baseline.
import {readFileSync, writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {gunzipSync} from 'node:zlib';
import {frozenV1Forecast} from '../worker/index.mjs';
import {goalEvents, summarizeGoalMarkets} from '../worker/goal-market-quality.mjs';

const sha = b => createHash('sha256').update(b).digest('hex');
const sourcePlan = JSON.parse(readFileSync('docs/research/neural-plan-2026-11-16.json'));
const compressed = readFileSync(sourcePlan.sourceSnapshot);
if (sha(compressed) !== sourcePlan.sourceCompressedSha256) throw new Error('Frozen corpus changed');
const raw = gunzipSync(compressed);
if (sha(raw) !== sourcePlan.sourceDataSha256) throw new Error('Frozen match data changed');
const games = JSON.parse(raw).filter(row => row[0] === 'E0');
if (games.length !== 4280) throw new Error('Unexpected Premier League history');

const teams = new Map();
function teamId(name) {
  if (!teams.has(name)) teams.set(name, teams.size + 1);
  return {id: teams.get(name)};
}
const seasons = new Map();
const seen = new Set();
for (const [i, [,day,year,home,away,h,a]] of games.entries()) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day) || !Number.isFinite(Date.parse(day)) ||
      !Number.isInteger(year) || typeof home !== 'string' || typeof away !== 'string' ||
      !home || !away || home === away || ![h,a].every(g => Number.isInteger(g) && g >= 0 && g <= 30)) throw new Error('Invalid corpus row');
  const id = JSON.stringify([day,home,away]);
  if (seen.has(id)) throw new Error('Duplicate corpus fixture');
  seen.add(id);
  // CSV dates have no reliable kickoff hour. A common time excludes all
  // results on the target day and preceding day from the 24-hour replay.
  const match = {id:i+1, utcDate:day+'T12:00:00Z', status:'FINISHED', homeTeam:teamId(home), awayTeam:teamId(away),
    score:{duration:'REGULAR',fullTime:{home:h,away:a}}};
  if (!seasons.has(year)) seasons.set(year, []);
  seasons.get(year).push(match);
}

function evaluate(year, includeRows) {
  const current = seasons.get(year), previous = seasons.get(year-1);
  const paired = [], skipped = [], rows = [];
  for (const game of current) {
    const now = Date.parse(game.utcDate) - 86400000;
    const day = game.utcDate.slice(0,10);
    let forecast;
    try { forecast = frozenV1Forecast(current, previous, game, year, now); }
    catch (error) {
      if (!/^(Мало прошлых матчей|Недостаточная суммарная статистическая масса|Слишком низкие средние голы)/.test(error.message))
        throw error;
      skipped.push({date:day,fixture:game.id,reason:error.message});
      continue;
    }
    if (forecast.inputs.some(g => Date.parse(g.utcDate) > now - 4 * 3600000)) throw new Error('Future input in replay');
    paired.push({state:'settled',result:game.score.fullTime,forecast:forecast.goalMarkets});
    if (includeRows) {
      const {home, away} = game.score.fullTime;
      const {model, base} = forecast.goalMarkets;
      rows.push({day, fixture:game.id, poisson:model, base, events:goalEvents(home, away)});
    }
  }
  const dates = current.map(g=>g.utcDate.slice(0,10)).sort();
  return {season:year,from:dates[0],to:dates.at(-1),completed:current.length,
    coverage:paired.length/current.length,skipped,quality:summarizeGoalMarkets(paired),
    ...(includeRows ? {rows} : {})};
}

const includeRows = process.argv[2] === '--training-rows';
const years = includeRows ? [...seasons.keys()].filter(year => seasons.has(year - 1)).sort() : [2025,2026];
const report = {schemaVersion:1, kind:'chronological-historical-replay', league:'PL',
  prospective:false, source:{file:sourcePlan.sourceSnapshot,sha256:sha(compressed),available:games.length},
  leadHours:24, resultSafetyHours:4, settings:{halfLifeDays:120,priorSeasonWeight:.35,shrinkageGames:5},
  primaryMetric:'logLoss', periods:years.map(year => evaluate(year, includeRows))};
const output = JSON.stringify(report,null,2)+'\n';
const location = process.argv[includeRows ? 3 : 2];
if (location) writeFileSync(location, output);
else process.stdout.write(output);
