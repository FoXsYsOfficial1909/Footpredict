import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash,webcrypto} from 'node:crypto';
import {predictCorner,validateCornerModel,validateCornerReport,verifyCornerBytes} from '../corners-ui.js';

const model=(family='poisson',alpha=0)=>({schemaVersion:1,target:'corners_total_regulation',
  model_version:'corners-test-1',status:'experimental',family,alpha,
  intercept:Math.log(5),home_advantage:Math.log(1.4),teams:{A:{attack:0,defence:0},B:{attack:0,defence:0}},
  aliases:{'A FC':'A','B FC':'B'},as_of:'2026-11-05',side_mean_bounds:[.1,20],
  lines:[8.5,9.5,10.5],parameters:{half_life:180,ridge:.03,window_days:1095},
  fit:{training_games:1121,latest_training_date:'2026-11-02'}});
const kickoff='2026-11-08T14:00:00Z';
const close=(actual,expected,tolerance=1e-12)=>assert.ok(Math.abs(actual-expected)<=tolerance,`${actual} vs ${expected}`);
function empiricalModel(){
  const base=model();
  for(const key of ['intercept','home_advantage','alpha','side_mean_bounds'])delete base[key];
  return {...base,model_version:'corners-league-test-1',family:'empirical',scope:'league_baseline',
    training_cutoff_exclusive:base.as_of,reason:'team_model_not_validated',
    league_estimate:{as_of:base.as_of,training_cutoff_exclusive:base.as_of,
      expectedCorners:{home:7,away:5,total:12},fit:{...base.fit},range80:{lower:6,upper:18,coverage:.8},
      totals:Object.fromEntries([.72,.61,.51].map((over,i)=>{const line=8.5+i;return [String(line),{line,over,under:1-over}];}))}};
}

test('league baseline is explicit and identical across clubs without invented team effects',()=>{
  const m=empiricalModel(),p=predictCorner(m,'A','B',kickoff),reverse=predictCorner(m,'B','A',kickoff);
  assert.equal(p.available,true);assert.equal(p.scope,'league_baseline');
  assert.deepEqual(p.expectedCorners,{home:7,away:5,total:12});
  assert.deepEqual(p.expectedCorners,reverse.expectedCorners);assert.deepEqual(p.totals,reverse.totals);
  assert.deepEqual(p.totals.map(row=>row.over),[.72,.61,.51]);
  assert.equal(predictCorner(m,'Unknown','B',kickoff).available,false);
  p.totals[0].over=0;
  assert.equal(m.league_estimate.totals['8.5'].over,.72);
});

test('empirical estimates must have coherent probabilities, means and training dates',()=>{
  for(const change of [m=>m.league_estimate.totals['9.5'].over=.99,
    m=>m.league_estimate.totals['8.5'].under=.99,
    m=>m.league_estimate.totals['8.5'].over=NaN,
    m=>m.league_estimate.expectedCorners.total=20,
    m=>m.league_estimate.fit.training_games=101,
    m=>m.league_estimate.as_of='2026-11-06',
    m=>m.league_estimate.range80.upper=5,
    m=>m.scope='personal_team_model']){
    const m=empiricalModel();change(m);assert.throws(()=>validateCornerModel(m));
  }
});

test('corner means exceed the goal cap and Poisson tails match independent SciPy values',()=>{
  const p=predictCorner(model(),'A FC','B FC',kickoff);
  assert.equal(p.available,true);close(p.expectedCorners.home,7);close(p.expectedCorners.away,5);close(p.expectedCorners.total,12);
  const expected=[0.8449722182325373,0.7576078383294875,0.6527705824458285];
  p.totals.forEach((line,i)=>{close(line.over,expected[i]);close(line.over+line.under,1);});
  assert.deepEqual(p.range80,[8,17]);
});

test('negative-binomial recurrence matches independent tails and approaches Poisson',()=>{
  const p=predictCorner(model('negative_binomial',.08),'A','B',kickoff);
  const expected=[0.7511894542023678,0.6707260864203922,0.585993233817332];
  p.totals.forEach((line,i)=>{close(line.over,expected[i]);close(line.over+line.under,1);});
  assert.deepEqual(p.range80,[6,18]);
  assert.ok(p.totals[0].over>=p.totals[1].over&&p.totals[1].over>=p.totals[2].over);
  const tiny=predictCorner(model('negative_binomial',1e-4),'A','B',kickoff);
  const poisson=predictCorner(model(),'A','B',kickoff);
  tiny.totals.forEach((line,i)=>close(line.over,poisson.totals[i].over,.001));
});

test('an unseen club has no public corner probabilities',()=>{
  const p=predictCorner(model(),'New Club','B',kickoff);
  assert.equal(p.available,false);assert.deepEqual(p.unseenTeams,['New Club']);assert.equal(p.totals,undefined);
  assert.throws(()=>predictCorner(model(),'A','A',kickoff));
});

test('future-fitted and malformed exports are rejected rather than producing plausible values',()=>{
  assert.throws(()=>predictCorner(model(),'A','B','2026-11-05T20:00:00Z'));
  for(const change of [{alpha:-1},{side_mean_bounds:[.05,5]},{intercept:NaN},
    {teams:{A:{attack:Infinity,defence:0}}},{target:'goals'},{as_of:'2026-02-30'},
    {fit:{training_games:1121,latest_training_date:'2026-11-05'}}]){
    assert.throws(()=>validateCornerModel({...model(),...change}));
  }
});

test('source freshness is independent of forecast date and only registered immutable URLs are accepted',()=>{
  const sha='a'.repeat(64),now=Date.parse('2026-11-06T17:00:00Z');
  const report={schemaVersion:1,latestSuccessful:{status:'success',generatedAt:'2026-11-06T16:00:00Z',
    modelVersion:'corners-test-1',modelSha256:sha,
    modelUrl:`https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/corner-reports/models/${sha}.json`}};
  assert.equal(validateCornerReport(report,now).failed,false);
  assert.equal(validateCornerReport({...report,latestAttempt:{status:'failed',generatedAt:'2026-11-06T16:30:00Z'}},now).failed,true);
  assert.throws(()=>validateCornerReport(report,now+37*3600000));
  assert.throws(()=>validateCornerReport({...report,latestSuccessful:{...report.latestSuccessful,modelUrl:'https://example.invalid/weights.json'}},now));
  assert.throws(()=>validateCornerReport(report,Date.parse('2026-11-06T15:00:00Z')));
});

test('the actual downloaded bytes must match the published SHA-256',async()=>{
  const bytes=new TextEncoder().encode(JSON.stringify(model())).buffer;
  const sha=createHash('sha256').update(Buffer.from(bytes)).digest('hex');
  const accepted=await verifyCornerBytes(bytes,sha,webcrypto.subtle);
  assert.equal(accepted.model_version,'corners-test-1');
  await assert.rejects(verifyCornerBytes(bytes,'0'.repeat(64),webcrypto.subtle),/checksum/);
});

test('selection reset aborts an old request and only the current match receives a forecast',async()=>{
  class Element{
    constructor(){this.children=[];this.textContent='';this.hidden=true;}
    append(...values){this.children.push(...values);}
    replaceChildren(...values){this.children=values;this.textContent='';}
  }
  const elements=Object.fromEntries(['cornerForecast','cornerStatus','cornerBody'].map(key=>[key,new Element()]));
  const old={document:globalThis.document,window:globalThis.window,fetch:globalThis.fetch};
  const window=new EventTarget();const requests=[];
  const bytes=new TextEncoder().encode(JSON.stringify(model())).buffer;
  const sha=createHash('sha256').update(Buffer.from(bytes)).digest('hex');
  const packet={schemaVersion:1,latestSuccessful:{status:'success',generatedAt:new Date().toISOString(),
    modelVersion:'corners-test-1',modelSha256:sha,
    modelUrl:`https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/corner-reports/models/${sha}.json`}};
  let firstAborted=false;
  globalThis.document={getElementById:id=>elements[id],createElement:()=>new Element()};
  globalThis.window=window;
  globalThis.fetch=async(url,options)=>{
    requests.push({url,method:options.method||'GET'});
    if(requests.length===1)return new Promise((resolve,reject)=>{
      options.signal.addEventListener('abort',()=>{firstAborted=true;reject(new DOMException('Aborted','AbortError'));},{once:true});
    });
    if(url.endsWith('/latest.json'))return {ok:true,text:async()=>JSON.stringify(packet)};
    return {ok:true,arrayBuffer:async()=>bytes};
  };
  const selected=()=>window.dispatchEvent(new CustomEvent('footpredict:match-selected',{
    detail:{league:'PL',homeName:'A',awayName:'B',kickoff}}));
  try{
    await import('../corners-ui.js?race-check');
    assert.equal(requests.length,0);
    selected();assert.equal(requests.length,1);
    window.dispatchEvent(new Event('footpredict:selection-reset'));
    assert.equal(elements.cornerForecast.hidden,true);assert.equal(elements.cornerBody.children.length,0);
    selected();
    const deadline=Date.now()+2000;
    while(!elements.cornerBody.children.length&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,5));
    assert.equal(firstAborted,true);assert.equal(elements.cornerForecast.hidden,false);
    assert.equal(elements.cornerStatus.textContent,'');assert.ok(elements.cornerBody.children.length>0);
    assert.ok(elements.cornerBody.children[0].textContent.includes('12,0'));
    assert.equal(requests.length,3);assert.ok(requests.every(r=>r.method==='GET'));
  }finally{
    window.dispatchEvent(new Event('footpredict:selection-reset'));
    for(const [key,value] of Object.entries(old)){
      if(value===undefined)delete globalThis[key];else globalThis[key]=value;
    }
  }
});
