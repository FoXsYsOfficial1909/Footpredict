import MODEL from './model-v2-data.mjs';

function outcomeProbabilities(homeGoals, awayGoals) {
  const home = [Math.exp(-homeGoals)], away = [Math.exp(-awayGoals)];
  for (let i=1;i<=20;i++) {home.push(home[i-1]*homeGoals/i);away.push(away[i-1]*awayGoals/i);}
  const result=[0,0,0];let best=-1,score='';
  for(let i=0;i<=20;i++)for(let j=0;j<=20;j++){
    const value=home[i]*away[j];result[i>j?0:i===j?1:2]+=value;
    if(value>best){best=value;score=i+' : '+j;}
  }
  const total=result.reduce((a,b)=>a+b,0);
  return {probabilities:result.map(value=>value/total),score};
}

export function modelV2Info(){
  return {modelVersion:MODEL.model_version,status:MODEL.status,asOf:MODEL.as_of,
    featuresUsed:MODEL.features_used,newsUsedInProbabilities:false,promotedToDefault:false,benchmark:MODEL.benchmark};
}

export function predictV2(homeName,awayName,kickoff){
  if(!Number.isFinite(Date.parse(kickoff))||Date.parse(kickoff)<=Date.parse(MODEL.as_of+'T00:00:00Z'))
    throw new Error('Experimental model cannot predict at or before its training cutoff.');
  const home=MODEL.aliases[homeName]||homeName,away=MODEL.aliases[awayName]||awayName;
  const homeStrength=MODEL.glm.teams[home]||{attack:0,defence:0};
  const awayStrength=MODEL.glm.teams[away]||{attack:0,defence:0};
  const h=Math.max(.05,Math.min(5,Math.exp(MODEL.glm.intercept+MODEL.glm.home_advantage+homeStrength.attack-awayStrength.defence)));
  const a=Math.max(.05,Math.min(5,Math.exp(MODEL.glm.intercept+awayStrength.attack-homeStrength.defence)));
  const poisson=outcomeProbabilities(h,a);
  const elo=MODEL.elo,delta=(elo.ratings[home]??1500)-(elo.ratings[away]??1500)+elo.parameters.home_advantage;
  const strength=10**(delta/800),values=[strength,elo.parameters.draw_weight,1/strength],total=values.reduce((x,y)=>x+y,0);
  const probabilities=poisson.probabilities.map((value,index)=>MODEL.glm_weight*value+(1-MODEL.glm_weight)*values[index]/total);
  return {...modelV2Info(),probabilities,goalExpectations:[h,a],poissonComponentScore:poisson.score,
    unseenTeams:[home,away].filter(team=>!MODEL.glm.teams[team]),
    note:'Экспериментальная версия. Новости и травмы пока не используются в расчёте вероятностей.'};
}
