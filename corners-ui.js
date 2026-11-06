// The corner forecast is independent of the saved 1X2 forecast.
export const CORNER_REPORT_URL='https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/corner-reports/latest.json';
const MODEL_PREFIX='https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/corner-reports/models/';
const LINES=[8.5,9.5,10.5];
const MAX_AGE=36*3600000,MAX_BYTES=200000;

const finite=(value,min,max)=>typeof value==='number'&&Number.isFinite(value)&&value>=min&&value<=max;
const object=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
function calendarDay(value){
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value)||!Number.isFinite(Date.parse(value+'T00:00:00Z'))||new Date(value+'T00:00:00Z').toISOString().slice(0,10)!==value)
    throw new Error('Invalid corner model date');
  return value;
}

export function validateCornerModel(model){
  if(!object(model)||model.schemaVersion!==1||model.target!=='corners_total_regulation'||
     model.status!=='experimental'||typeof model.model_version!=='string'||!model.model_version||
     !['poisson','negative_binomial','empirical'].includes(model.family)||
     !object(model.teams)||!object(model.aliases)||
     JSON.stringify(model.lines)!=='[8.5,9.5,10.5]')throw new Error('Invalid corner model');
  calendarDay(model.as_of);
  if(model.family!=='empirical'){
    if(!finite(model.intercept,-2,4)||!finite(model.home_advantage,-1,1)||JSON.stringify(model.side_mean_bounds)!=='[0.1,20]')throw new Error('Invalid corner rates');
    if(model.family==='poisson'?model.alpha!==0:!finite(model.alpha,1e-4,1))throw new Error('Invalid corner dispersion');
  }
  const entries=Object.entries(model.teams);
  if(!entries.length||entries.length>100)throw new Error('Invalid corner team count');
  for(const [name,strength] of entries){
    if(!name.trim()||name.length>100||!object(strength)||!finite(strength.attack,-2.5,2.5)||!finite(strength.defence,-2.5,2.5))
      throw new Error('Invalid corner team strength');
  }
  for(const [name,alias] of Object.entries(model.aliases)){
    if(!name||typeof alias!=='string'||!alias.trim()||alias.length>100)throw new Error('Invalid corner alias');
  }
  if(!object(model.fit)||!Number.isSafeInteger(model.fit.training_games)||model.fit.training_games<100||
     calendarDay(model.fit.latest_training_date)>=model.as_of)throw new Error('Invalid corner training fence');
  if(model.family==='empirical'){
    const estimate=model.league_estimate,means=estimate?.expectedCorners,range=estimate?.range80;
    if(model.scope!=='league_baseline'||model.training_cutoff_exclusive!==model.as_of||!object(estimate)||
       estimate.as_of!==model.as_of||estimate.training_cutoff_exclusive!==model.as_of||!object(means)||
       !finite(means.home,0,40)||!finite(means.away,0,40)||!finite(means.total,0,80)||
       Math.abs(means.home+means.away-means.total)>1e-10||!object(estimate.totals)||
       !object(estimate.fit)||estimate.fit.training_games!==model.fit.training_games||estimate.fit.latest_training_date!==model.fit.latest_training_date||
       !object(range)||!Number.isSafeInteger(range.lower)||!Number.isSafeInteger(range.upper)||
       range.lower<0||range.upper>80||range.lower>range.upper||range.coverage!==.8)
      throw new Error('Invalid empirical corner estimate');
    let previous=1;
    for(const line of LINES){
      const event=estimate.totals[String(line)];
      if(!object(event)||event.line!==line||!finite(event.over,0,1)||!finite(event.under,0,1)||
         Math.abs(event.over+event.under-1)>1e-10||event.over>previous+1e-12)
        throw new Error('Invalid empirical corner probabilities');
      previous=event.over;
    }
  }
  return model;
}

function countCdf(mean,family,alpha){
  let mass=family==='poisson'?Math.exp(-mean):Math.exp(-Math.log1p(alpha*mean)/alpha);
  let cumulative=mass;
  const values=[Math.min(1,cumulative)];
  // With mean <=40 and alpha <=1 both requested quantiles are below 250.
  for(let k=1;k<=250;k++){
    mass*=family==='poisson'?mean/k:((k-1)*alpha+1)/k*(mean/(1+alpha*mean));
    cumulative+=mass;values.push(Math.max(0,Math.min(1,cumulative)));
    if(k>=10&&cumulative>=.999999999999)break;
  }
  return values;
}

export function predictCorner(model,homeName,awayName,kickoff){
  validateCornerModel(model);
  const time=Date.parse(kickoff);
  if(!Number.isFinite(time)||new Date(time).toISOString().slice(0,10)<=model.as_of)
    throw new Error('Corner model training cutoff must precede the target day');
  if(typeof homeName!=='string'||typeof awayName!=='string'||!homeName.trim()||!awayName.trim())throw new Error('Invalid corner fixture');
  const home=Object.hasOwn(model.aliases,homeName)?model.aliases[homeName]:homeName;
  const away=Object.hasOwn(model.aliases,awayName)?model.aliases[awayName]:awayName;
  if(home===away)throw new Error('Identical corner teams');
  const unseenTeams=[home,away].filter(name=>!Object.hasOwn(model.teams,name));
  const metadata={available:unseenTeams.length===0,unseenTeams,modelVersion:model.model_version,asOf:model.as_of,
    scope:model.family==='empirical'?'league_baseline':'team_model'};
  if(unseenTeams.length)return metadata;
  if(model.family==='empirical'){
    const estimate=model.league_estimate;
    return {...metadata,expectedCorners:{...estimate.expectedCorners},
      totals:LINES.map(line=>({...estimate.totals[String(line)]})),range80:[estimate.range80.lower,estimate.range80.upper]};
  }
  const hs=model.teams[home],as=model.teams[away];
  const h=Math.max(.1,Math.min(20,Math.exp(model.intercept+model.home_advantage+hs.attack-as.defence)));
  const a=Math.max(.1,Math.min(20,Math.exp(model.intercept+as.attack-hs.defence)));
  const mean=h+a,cdf=countCdf(mean,model.family,model.alpha);
  const totals=LINES.map(line=>({line,under:cdf[Math.floor(line)],over:Math.max(0,1-cdf[Math.floor(line)])}));
  const quantile=p=>{const index=cdf.findIndex(v=>v>=p);if(index<0)throw new Error('Corner quantile range exhausted');return index;};
  return {...metadata,expectedCorners:{home:h,away:a,total:mean},totals,range80:[quantile(.1),quantile(.9)]};
}

export function validateCornerReport(packet,now=Date.now()){
  const success=packet?.latestSuccessful;
  if(packet?.schemaVersion!==1||success?.status!=='success'||
     !/^[0-9a-f]{64}$/.test(success.modelSha256||'')||typeof success.modelVersion!=='string'||
     success.modelUrl!==MODEL_PREFIX+success.modelSha256+'.json')throw new Error('Invalid corner report');
  const generated=Date.parse(success.generatedAt);
  if(!Number.isFinite(generated)||generated>now||now-generated>MAX_AGE)throw new Error('Corner model is stale');
  const failed=packet.latestAttempt?.status==='failed'&&Date.parse(packet.latestAttempt.generatedAt)>=generated;
  return {success,failed};
}

export async function verifyCornerBytes(bytes,expected,subtle=globalThis.crypto?.subtle){
  if(!(bytes instanceof ArrayBuffer)||bytes.byteLength>MAX_BYTES||!subtle)throw new Error('Invalid corner model bytes');
  const digest=new Uint8Array(await subtle.digest('SHA-256',bytes));
  const actual=Array.from(digest,b=>b.toString(16).padStart(2,'0')).join('');
  if(actual!==expected)throw new Error('Corner model checksum mismatch');
  return validateCornerModel(JSON.parse(new TextDecoder().decode(bytes)));
}

const panel=globalThis.document?.getElementById('cornerForecast');
if(panel){
  const status=document.getElementById('cornerStatus'),body=document.getElementById('cornerBody');
  let generation=0,controller=null,cached=null;
  const node=(tag,text,className)=>{const item=document.createElement(tag);if(text!==undefined)item.textContent=text;if(className)item.className=className;return item;};
  function reset(){generation++;controller?.abort();controller=null;body.replaceChildren();panel.hidden=true;status.textContent='';}
  async function loadModel(signal){
    const now=Date.now();
    if(cached&&now-cached.loadedAt<300000){validateCornerReport(cached.packet,now);return cached;}
    const response=await fetch(CORNER_REPORT_URL,{signal,credentials:'omit',cache:'no-store',redirect:'error'});
    if(!response.ok)throw new Error('Corner report unavailable');
    const text=await response.text();if(text.length>MAX_BYTES)throw new Error('Corner report oversized');
    const packet=JSON.parse(text),verified=validateCornerReport(packet,now);
    const weights=await fetch(verified.success.modelUrl,{signal,credentials:'omit',cache:'no-store',redirect:'error'});
    if(!weights.ok)throw new Error('Corner weights unavailable');
    const model=await verifyCornerBytes(await weights.arrayBuffer(),verified.success.modelSha256);
    if(model.model_version!==verified.success.modelVersion)throw new Error('Corner version mismatch');
    return {packet,model,loadedAt:now};
  }
  function render(prediction,metadata){
    const one=value=>value.toLocaleString('ru-RU',{minimumFractionDigits:1,maximumFractionDigits:1});
    const pct=value=>(100*value).toLocaleString('ru-RU',{maximumFractionDigits:0})+'%';
    const title=document.getElementById('cornerTitle');
    if(title)title.textContent=prediction.scope==='league_baseline'?'Угловые · базовая оценка АПЛ':'Угловые';
    if(prediction.scope==='league_baseline'){
      body.append(node('p','Средний тотал по АПЛ: '+one(prediction.expectedCorners.total)+' угловых.','research-match'),
        node('p','По средней статистике лиги, без подтверждённой поправки на эти команды.','research-note'));
    }else{
      body.append(node('p','Ожидается всего '+one(prediction.expectedCorners.total)+' угловых.','research-match'),
        node('p','Хозяева: '+one(prediction.expectedCorners.home)+' · гости: '+one(prediction.expectedCorners.away)+'.','research-note'));
    }
    const list=node('dl',undefined,'quality-facts');
    for(const line of prediction.totals){
      const row=node('div');row.append(node('dt',String(Math.floor(line.line)+1)+' и более'),node('dd',pct(line.over)));list.append(row);
    }
    body.append(list);
    const date=new Date(metadata.success.generatedAt).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'});
    body.append(node('p','Экспериментальный прогноз · расчёт от '+date+' МСК.','research-note'));
    if(prediction.scope==='league_baseline')body.append(node('p','Командная модель проверяется отдельно.','research-note'));
    if(metadata.failed)body.append(node('p','Последняя проверка модели не завершилась; показана предыдущая проверенная версия.','research-note'));
    status.textContent='';
  }
  window.addEventListener('footpredict:selection-reset',reset);
  window.addEventListener('footpredict:match-selected',async event=>{
    reset();const detail=event.detail;
    if(detail?.league!=='PL'||!detail.homeName||!detail.awayName||!Number.isFinite(Date.parse(detail.kickoff)))return;
    panel.hidden=false;status.textContent='Загружаю прогноз угловых…';
    const current=generation;controller=new AbortController();const active=controller;
    const timeout=setTimeout(()=>active.abort(),15000);
    try{
      const loaded=await loadModel(active.signal);
      if(current!==generation)return;
      cached=loaded;
      const metadata=validateCornerReport(loaded.packet),prediction=predictCorner(loaded.model,detail.homeName,detail.awayName,detail.kickoff);
      if(!prediction.available){status.textContent='Для одной из команд пока мало истории угловых.';return;}
      render(prediction,metadata);
    }catch{
      if(current===generation)status.textContent='Прогноз угловых пока недоступен. Основной прогноз матча доступен отдельно.';
    }finally{clearTimeout(timeout);if(current===generation)controller=null;}
  });
}
