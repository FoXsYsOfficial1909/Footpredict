import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {buildHundred,writeHundred,HUNDRED_LEAGUES} from '../scripts/freeze_hundred.mjs';
import {frozenV1Forecast} from '../worker/index.mjs';
const NOW=Date.parse('2026-11-11T18:00:00Z');
function sources(){
 const all={};
 HUNDRED_LEAGUES.forEach((code,index)=>{
  const home={id:1+index*2,name:'Home '+code},away={id:2+index*2,name:'Away '+code};
  const pair={};
  for(const [kind,year] of [['current',2026],['previous',2025]]){
   const start=kind==='current'?NOW-40*86400000:NOW-400*86400000;
   const matches=Array.from({length:20},(_,i)=>({id:index*10000+(kind==='current'?100:1000)+i,utcDate:new Date(start+i*86400000).toISOString(),
    status:'FINISHED',homeTeam:home,awayTeam:away,score:{duration:'REGULAR',fullTime:{home:2,away:1}}}));
   if(kind==='current')for(let i=0;i<40;i++)matches.push({id:5000+index*10000+i,utcDate:new Date(NOW+3*86400000+i*3600000).toISOString(),
    status:'TIMED',homeTeam:home,awayTeam:away,score:{duration:'REGULAR',fullTime:{home:null,away:null}}});
   pair[kind]={data:{filters:{season:year},competition:{code},matches},observedAt:new Date(NOW-1000).toISOString(),url:'https://example.org/'+code+'/'+year,sha256:'a'.repeat(64)};
  }
  all[code]=pair;
 });return all;
}
test('exactly one hundred future predictions use separate league histories and a fixed source clock',()=>{
 const input=sources(),before=JSON.stringify(input),{packet,inputs}=buildHundred(input,NOW);
 assert.equal(packet.fixtures.length,100);assert.equal(new Set(packet.fixtures.map(f=>f.fixtureId)).size,100);
 assert.equal(packet.fixtures[0].league,'BSA');
 assert.ok(packet.fixtures.every(f=>Date.parse(f.kickoff)>=NOW+24*3600000&&Math.abs(f.probabilities.reduce((a,b)=>a+b)-1)<1e-12));
 assert.ok(Object.values(inputs).every(rows=>rows.every(g=>g.status==='FINISHED'&&Date.parse(g.utcDate)<=NOW-4*3600000)));
 assert.equal(JSON.stringify(input),before);
 const pair=input.PL,game=pair.current.data.matches.at(-1);
 const first=frozenV1Forecast(pair.current.data.matches,pair.previous.data.matches,game,2026,NOW);
 const future={...pair.current.data.matches[0],id:999999,utcDate:new Date(NOW+3600000).toISOString(),score:{duration:'REGULAR',fullTime:{home:20,away:0}}};
 assert.deepEqual(frozenV1Forecast([...pair.current.data.matches,future],pair.previous.data.matches,game,2026,NOW),first);
 assert.throws(()=>buildHundred({...input,PL:{...input.PL,current:{...input.PL.current,observedAt:new Date(NOW+1).toISOString()}}},NOW));
});
test('a published packet and its input bytes cannot be refrozen or overwritten',()=>{
 const dir=mkdtempSync(join(tmpdir(),'footpredict-hundred-'));
 try{
  const output=join(dir,'cohort'),ui=join(dir,'cohort.js');
  const first=writeHundred(sources(),output,ui,NOW),raw=readFileSync(join(output,'packet.json'));
  assert.equal(first.sha,createHash('sha256').update(raw).digest('hex'));
  assert.equal(readFileSync(join(output,'forecasts.csv'),'utf8').trim().split('\n').length,101);
  assert.throws(()=>writeHundred(sources(),output,ui,NOW+1),/already exists/);
  assert.deepEqual(readFileSync(join(output,'packet.json')),raw);
 }finally{rmSync(dir,{recursive:true,force:true});}
});
