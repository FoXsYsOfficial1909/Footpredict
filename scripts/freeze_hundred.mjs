// Register exactly 100 genuinely future league matches. Never refreeze an existing packet.
import {readFileSync,writeFileSync,existsSync,mkdirSync,statSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {frozenV1Forecast} from '../worker/index.mjs';

export const HUNDRED_PROTOCOL='hundred-future-1';
export const HUNDRED_LEAGUES=['BSA','ELC','PD','PL'];
const checksum=text=>createHash('sha256').update(text).digest('hex');

export function buildHundred(sources,now){
  if(!Number.isFinite(now))throw new Error('Explicit clock required');
  const pool=[],manifest=[],byLeague={};
  for(const code of HUNDRED_LEAGUES){
    const pair=sources[code];
    if(!pair?.current||!pair?.previous)throw new Error('Both source seasons required: '+code);
    for(const [kind,year] of [['current',2026],['previous',2025]]){
      const source=pair[kind],data=source.data;
      if(data.competition?.code!==code||!Array.isArray(data.matches)||data.matches.length>650||
        Number(data.filters?.season)!==year||!Number.isFinite(Date.parse(source.observedAt))||Date.parse(source.observedAt)>now)
        throw new Error('Invalid league/season/source observation: '+code);
      if(new Set(data.matches.map(g=>g.id)).size!==data.matches.length)throw new Error('Duplicate fixture in source');
      manifest.push({league:code,season:year,url:source.url,sha256:source.sha256,observedAt:source.observedAt,rows:data.matches.length});
    }
    for(const game of pair.current.data.matches){
      if(['TIMED','SCHEDULED'].includes(game.status)&&Date.parse(game.utcDate)>=now+24*3600000&&Date.parse(game.utcDate)<=now+45*86400000)
        pool.push({code,game});
    }
  }
  pool.sort((a,b)=>Date.parse(a.game.utcDate)-Date.parse(b.game.utcDate)||a.code.localeCompare(b.code)||a.game.id-b.game.id);
  const fixtures=[],skipped=[];
  for(const {code,game} of pool){
    if(!Number.isSafeInteger(game.id)||game.id<=0||!Number.isSafeInteger(game.homeTeam?.id)||!Number.isSafeInteger(game.awayTeam?.id)||
      !game.homeTeam.name||!game.awayTeam.name||game.homeTeam.id===game.awayTeam.id)throw new Error('Invalid fixture identity');
    const pair=sources[code];let prediction;
    try{prediction=frozenV1Forecast(pair.current.data.matches,pair.previous.data.matches,game,2026,now);}
    catch(error){skipped.push({fixtureId:game.id,league:code,reason:String(error.message)});continue;}
    if(!byLeague[code])byLeague[code]=prediction.inputs;
    else if(checksum(JSON.stringify(byLeague[code]))!==checksum(JSON.stringify(prediction.inputs)))throw new Error('League input set changed during registration');
    fixtures.push({fixtureId:game.id,league:code,season:2026,homeId:game.homeTeam.id,awayId:game.awayTeam.id,
      home:game.homeTeam.name,away:game.awayTeam.name,kickoff:game.utcDate,statusAtFreeze:game.status,
      leadHours:(Date.parse(game.utcDate)-now)/3600000,
      probabilities:prediction.probabilities,baseline:prediction.baseline,expectedGoals:prediction.expectedGoals,
      mostLikelyScore:prediction.mostLikelyScore,stats:prediction.stats});
    if(fixtures.length===100)break;
  }
  if(fixtures.length!==100||new Set(fixtures.map(f=>f.fixtureId)).size!==100)throw new Error('Exactly 100 unique predictable future matches required');
  const packet={schemaVersion:1,protocol:HUNDRED_PROTOCOL,frozenAt:new Date(now).toISOString(),
    market:'1X2_REGULATION',modelVersion:'poisson-1.0.0',forecastCount:100,
    leagues:HUNDRED_LEAGUES,minimumLeadHours:24,maximumLeadDays:45,
    selection:'Earliest eligible matches by UTC kickoff, league code and fixture ID. Skip only insufficient historical data; no confidence-based selection.',
    parameters:{priorSeasonWeight:.35,halfLifeDays:120,regularizationGames:5,minLeagueGames:15,minTeamSideGames:2,finishSafetyHours:4,goalCap:5,poissonMax:20},
    sourceManifest:manifest,inputSha256:Object.fromEntries(Object.entries(byLeague).map(([code,games])=>[code,checksum(JSON.stringify(games))])),
    skipped,fixtures,availabilityUsed:false,newsUsed:false,
    scope:'Independent frozen mixed-league cohort. Does not replace old histories or count as 100 FPL injury training examples.',
    updatePolicy:'Forecasts and inputs never change. Results are observed separately; transfers, postponements and identity changes require review.'};
  return {packet,inputs:byLeague};
}

export function writeHundred(sources,directory,uiFile,now=Date.now()){
  if(existsSync(join(directory,'packet.json')))throw new Error('Frozen packet already exists; refusing to refreeze');
  const {packet,inputs}=buildHundred(sources,now),raw=JSON.stringify(packet),sha=checksum(raw);
  mkdirSync(join(directory,'inputs'),{recursive:true});
  for(const [code,games] of Object.entries(inputs))writeFileSync(join(directory,'inputs',code+'.json'),JSON.stringify(games),{flag:'wx'});
  writeFileSync(join(directory,'packet.json'),raw,{flag:'wx'});
  writeFileSync(join(directory,'packet.sha256'),sha+'\n',{flag:'wx'});
  const csv=['Матч ID;Лига;Начало UTC;Хозяева;Гости;П1 %;Ничья %;П2 %;Версия модели;Зафиксировано UTC',
    ...packet.fixtures.map(f=>[f.fixtureId,f.league,f.kickoff,f.home,f.away,...f.probabilities.map(p=>(p*100).toFixed(4)),packet.modelVersion,packet.frozenAt].join(';'))].join('\n');
  writeFileSync(join(directory,'forecasts.csv'),'\ufeff'+csv+'\n',{flag:'wx'});
  writeFileSync(uiFile,"// Immutable cohort; generated once by scripts/freeze_hundred.mjs.\nexport const PACKET_SHA256='"+sha+"';\nexport const PACKET="+raw+';\n',{flag:'wx'});
  return {sha,packet};
}

if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url)){
  const args=Object.fromEntries(process.argv.slice(2).reduce((rows,value,index,array)=>index%2?rows:[...rows,[value,array[index+1]]],[]));
  if(!args['--source-dir']||!args['--output']||!args['--ui-file'])throw new Error('--source-dir, --output and --ui-file required');
  const directory=resolve(args['--source-dir']),sources={};
  for(const code of HUNDRED_LEAGUES){
    sources[code]={};
    for(const [kind,year] of [['current',2026],['previous',2025]]){
      const path=join(directory,code+'-'+year+'.json'),body=readFileSync(path);
      sources[code][kind]={data:JSON.parse(body),sha256:checksum(body),observedAt:statSync(path).mtime.toISOString(),
        url:'https://footpredict-api.wzjp5747md.workers.dev/fd/competitions/'+code+'/matches?season='+year};
    }
  }
  const {sha,packet}=writeHundred(sources,resolve(args['--output']),resolve(args['--ui-file']));
  console.log(JSON.stringify({sha,frozenAt:packet.frozenAt,count:packet.fixtures.length,first:packet.fixtures[0],last:packet.fixtures.at(-1),counts:Object.fromEntries(HUNDRED_LEAGUES.map(code=>[code,packet.fixtures.filter(f=>f.league===code).length]))}));
}
