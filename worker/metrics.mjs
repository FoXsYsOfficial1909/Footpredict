export function scoreQuality(probabilities,score){
  const actual=score.home>score.away?0:score.home===score.away?1:2;
  const max=Math.max(...probabilities),leaders=probabilities.map((p,i)=>Math.abs(p-max)<=1e-12?i:-1).filter(i=>i>=0);
  return {brier:probabilities.reduce((sum,p,i)=>sum+(p-Number(i===actual))**2,0),
    logLoss:-Math.log(Math.max(1e-12,probabilities[actual])),actual,actualProbability:probabilities[actual],
    top:leaders.length===1?leaders[0]:null,topProbability:max,correct:leaders.length===1?leaders[0]===actual:null};
}

export function summarizeQuality(records){
  const scored=records.filter(record=>record.metrics),eligible=scored.filter(record=>record.metrics.correct!==null);
  const average=key=>scored.length?scored.reduce((sum,row)=>sum+row.metrics[key],0)/scored.length:null;
  return {n:scored.length,brier:average('brier'),logLoss:average('logLoss'),
    correct:eligible.filter(row=>row.metrics.correct).length,topEligible:eligible.length,ties:scored.length-eligible.length,
    accuracy:eligible.length?eligible.filter(row=>row.metrics.correct).length/eligible.length:null};
}
