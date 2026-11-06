// Lightweight public context. Does not call AI, collect news, or write forecasts.
import {teamStatistics} from './research.mjs';
import {getPlayerAvailability} from './player-availability.mjs';
import {getConfirmedLineups} from './confirmed-lineups.mjs';

export const PLAYER_CONTEXT_VERSION = 'player-context-1.0.0';
const DAY = 86400000;
const error = (status, message) => Object.assign(new Error(message), {status});

export async function getPlayerContext(fixtureId, env, services) {
  if (!Number.isSafeInteger(fixtureId) || fixtureId < 1 || fixtureId > 999999999999)
    throw error(400, 'Некорректный ID матча.');
  const now = services.now(), date = new Date(now);
  const year = date.getUTCFullYear() - (date.getUTCMonth() < 6 ? 1 : 0);
  // This shares the already loaded match-list cache; no per-team provider calls.
  const season = await services.upstream('fd', `/competitions/PL/matches?season=${year}`, {ttl:300});
  const games = season.data?.matches;
  if (!Array.isArray(games)) throw error(502, 'Нет календаря АПЛ.');
  const fixture = games.find(game => game?.id === fixtureId);
  if (!fixture) throw error(404, 'Матч не найден в текущем сезоне АПЛ.');
  const kickoff = Date.parse(fixture.utcDate);
  if (!Number.isFinite(kickoff) || kickoff < now - 7*DAY || kickoff > now + 14*DAY ||
      ![fixture.homeTeam?.id, fixture.awayTeam?.id].every(id => Number.isSafeInteger(id) && id > 0) ||
      fixture.homeTeam.id === fixture.awayTeam.id ||
      ![fixture.homeTeam.name, fixture.awayTeam.name].every(name => typeof name === 'string' && name.length > 0 && name.length <= 120) ||
      (fixture.competition?.code && fixture.competition.code !== 'PL'))
    throw error(422, 'Сведения доступны для ближайших матчей АПЛ.');
  const matchedFixture = {...fixture, competition:{code:'PL'}, season:{...fixture.season, startDate:`${year}-07-01`}};
  const [availability, lineups] = await Promise.all([
    getPlayerAvailability(env, services, matchedFixture),
    getConfirmedLineups(env, services, matchedFixture)
  ]);
  return {version:PLAYER_CONTEXT_VERSION, collectedAt:new Date(services.now()).toISOString(),
    fixture:{id:fixture.id, competition:'PL', season:year, home:fixture.homeTeam.name,
      away:fixture.awayTeam.name, kickoff:fixture.utcDate},
    formFetchedAt:season.fetchedAt, formCutoffAt:new Date(Math.min(now,kickoff)).toISOString(),
    teams:[fixture.homeTeam,fixture.awayTeam].map(team => teamStatistics(team,games,{cutoff:Math.min(now,kickoff),kickoff})),
    availability, lineups, usedInProbabilities:false};
}
