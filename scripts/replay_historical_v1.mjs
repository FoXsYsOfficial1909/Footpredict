// Retrospective, past-only replay using exactly the production calculation.
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {createHash} from 'node:crypto';
import {frozenV1Forecast} from '../worker/index.mjs';

const cohort=resolve('docs/research/hundred-2026-11-11');
const packetBytes=readFileSync(join(cohort,'packet.json'));
const sourceChecksum=readFileSync(join(cohort,'packet.sha256'),'utf8').trim();
if(createHash('sha256').update(packetBytes).digest('hex')!==sourceChecksum)throw new Error('Frozen cohort bytes changed');
const packet=JSON.parse(packetBytes);
const output=resolve(process.argv[2]||'/tmp/footpredict-historical-v1');mkdirSync(output,{recursive:true});
const rows=[],skipped=[],manifest=[];
for(const code of packet.leagues){
  const raw=readFileSync(join(cohort,'inputs',code+'.json')),hash=createHash('sha256').update(raw).digest('hex');
  if(hash!==packet.inputSha256[code])throw new Error('Frozen history changed: '+code);
  const games=JSON.parse(raw);manifest.push({league:code,inputSha256:hash,rows:games.length});
  for(const game of [...games].sort((a,b)=>Date.parse(a.utcDate)-Date.parse(b.utcDate)||a.id-b.id)){
    const now=Date.parse(game.utcDate)-24*3600000,year=game.__year;
    let forecast;
    try{forecast=frozenV1Forecast(games.filter(g=>g.__year===year),games.filter(g=>g.__year===year-1),game,year,now);}
    catch(error){skipped.push({fixtureId:game.id,league:code,reason:error.message});continue;}
    const score=game.score.fullTime;
    rows.push({fixtureId:game.id,league:code,date:game.utcDate.slice(0,10),kickoff:game.utcDate,
      simulatedForecastAt:new Date(now).toISOString(),season:year,homeId:game.homeTeam.id,awayId:game.awayTeam.id,
      actual:score.home>score.away?0:score.home===score.away?1:2,v1:forecast.probabilities,base:forecast.baseline,
      pastMatches:forecast.inputs.length});
  }
}
rows.sort((a,b)=>a.kickoff.localeCompare(b.kickoff)||a.league.localeCompare(b.league)||a.fixtureId-b.fixtureId);
const result={schemaVersion:1,protocol:'mixed-v1-replay-1',leadHours:24,resultSafetyHours:4,
  modelVersion:'poisson-1.0.0',sourceCohortSha256:sourceChecksum,
  sourceManifest:manifest,rows,skipped,retrospective:true};
writeFileSync(join(output,'replay.json'),JSON.stringify(result));
console.log(JSON.stringify({sourceMatches:manifest.reduce((n,s)=>n+s.rows,0),replayed:rows.length,skipped:skipped.length,
  byLeague:Object.fromEntries(packet.leagues.map(code=>[code,rows.filter(r=>r.league===code).length]))}));
