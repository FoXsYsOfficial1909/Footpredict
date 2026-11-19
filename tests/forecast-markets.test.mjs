import test from 'node:test';
import assert from 'node:assert/strict';
import {frozenV1Forecast} from '../worker/index.mjs';
import {summarizeGoalMarkets} from '../worker/goal-market-quality.mjs';
import {predictScore,forecastMarkets,marketQuote,riskLevel} from '../forecast-markets.js';

const stats = {homeScored:1.8,homeConceded:1.07,awayScored:1.3,awayConceded:1.5,leagueHome:1.5,leagueAway:1.2};
const close = (x,y) => assert.ok(Math.abs(x-y)<1e-12, x + ' != ' + y);
const byId = (group,id) => group.find(m => m.id===id).p;

test('all goal markets are consistent with the same joint score distribution',()=>{
  const prediction = predictScore(stats);
  const markets = forecastMarkets(prediction.prob,stats,prediction);
  const {outcomes,totals,teamGoals,handicap,scores} = markets.groups;
  close(prediction.prob.reduce((s,p)=>s+p,0),1);
  close(byId(outcomes,'home-draw'),prediction.prob[0]+prediction.prob[1]);
  close(byId(outcomes,'away-draw'),prediction.prob[1]+prediction.prob[2]);
  close(byId(handicap,'home-handicap-0.5'),byId(outcomes,'home-draw'));
  close(byId(handicap,'away-handicap--0.5'),prediction.prob[2]);
  for (const t of [.5,1.5,2.5,3.5])
    close(byId(totals,'total-over-'+t)+byId(totals,'total-under-'+t),1);
  close(byId(totals,'total-under-0.5'),Math.exp(-prediction.homeGoals-prediction.awayGoals)/prediction.total);
  close(byId(teamGoals,'both-yes')+byId(teamGoals,'both-no'),1);
  for (const side of ['home','away']) for (const t of [.5,1.5,2.5])
    close(byId(teamGoals,side+'-total-over-'+t)+byId(teamGoals,side+'-total-under-'+t),1);
  assert.equal(scores[0].label,prediction.score);
  assert.equal(new Set(markets.all.map(m=>m.id)).size,markets.all.length);
  assert.deepEqual(markets.all.map(m=>m.p),markets.all.map(m=>m.p).sort((a,b)=>b-a));
  assert.equal(markets.best,markets.all[0]);
});

test('historical and alternate models cannot acquire unrelated score estimates',()=>{
  const frozen = Object.freeze([.55,.2,.25]);
  for (const input of [undefined,stats,{...stats,awayConceded:NaN}]) {
    const markets = forecastMarkets(frozen,input);
    assert.equal(markets.score,null);
    assert.equal(markets.groups.outcomes.length,6);
    assert.equal(markets.groups.totals.length,0);
    assert.equal(markets.groups.handicap.length,0);
  }
  assert.deepEqual(frozen,[.55,.2,.25]);
  assert.equal(forecastMarkets([.5,.5,.5],stats),null);
});

test('risk bands and price estimates use current numerical data',()=>{
  assert.deepEqual([.85,.849999,.70,.699999].map(riskLevel),['low','medium','medium','high']);
  const home = {id:'home',p:.60};
  assert.equal(marketQuote(home,{msg:'odds unavailable'}),null);
  assert.equal(marketQuote(home,{homeWin:'1.5'}),null);
  assert.equal(marketQuote(home,{homeWin:1}),null);
  assert.equal(marketQuote({id:'total-over-1.5',p:.75},{homeWin:2.1}),null);
  const quote=marketQuote(home,{homeWin:1.5});
  assert.equal(quote.price,1.5);
  close(quote.expectedReturn,-.10);
});

test('the Worker records the probabilities used by the card and only counts saved market predictions',()=>{
  const kickoff='2026-12-05T16:00:00Z', now=Date.parse(kickoff)-2*86400000;
  const target={id:999,kickoff,utcDate:kickoff,homeTeam:{id:1},awayTeam:{id:2}};
  const history=Array.from({length:18},(_,i)=>({...target,id:100+i,
    utcDate:new Date(now-(i+1)*86400000).toISOString(),status:'FINISHED',
    score:{duration:'REGULAR',fullTime:{home:i%4,away:1}}}));
  const model=frozenV1Forecast(history,[],target,2026,now);
  const panel=forecastMarkets(model.probabilities,model.stats);
  close(byId(panel.groups.totals,'total-over-2.5'),model.goalMarkets.model.over25);
  close(byId(panel.groups.teamGoals,'both-yes'),model.goalMarkets.model.bothScore);
  const summary=summarizeGoalMarkets([
    {state:'settled',result:{home:2,away:0}},
    {state:'review',forecast:model.goalMarkets},
    {state:'settled',forecast:model.goalMarkets,result:{home:1,away:1}},
    {state:'settled',forecast:{...model.goalMarkets,model:{over25:4,bothScore:1}},result:{home:0,away:0}},
    {state:'settled',forecast:model.goalMarkets}
  ]);
  assert.deepEqual(summary.counts,{registered:4,pending:0,settled:1,review:1,excluded:2,withoutSnapshot:1});
  assert.equal(summary.markets.over25.frequency,0);
  assert.equal(summary.markets.bothScore.frequency,1);
  close(summary.markets.over25.base.brier,2*model.goalMarkets.base.over25**2);
});
