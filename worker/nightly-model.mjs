// Read public, content-addressed weights. This never deploys or promotes a model.
export const TRAINING_BASE='https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/training-reports/';
export const NIGHTLY_MAX_AGE_HOURS=36;
export const NIGHTLY_SETTINGS_SHA='bdc6f29f3ab8db55cbdcd861fee4cd280103b18c5dcf231a96575decff8b96cd';
const DAY=86400000,HOUR=3600000,PROTOCOL='nightly-fixed-settings-1';
const HEX=/^[0-9a-f]{64}$/,COMMIT=/^[0-9a-f]{40}$/;
const fail=message=>{throw Object.assign(new Error(message),{status:503});};
const date=value=>{
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value))return NaN;
  const t=Date.parse(value+'T00:00:00Z');return Number.isFinite(t)&&new Date(t).toISOString().slice(0,10)===value?t:NaN;
};
const number=(value,min,max)=>typeof value==='number'&&Number.isFinite(value)&&value>=min&&value<=max;
const integer=(value,min,max)=>Number.isSafeInteger(value)&&value>=min&&value<=max;
const record=value=>value&&typeof value==='object'&&!Array.isArray(value);

export async function modelDigest(body){
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(body)))].map(n=>n.toString(16).padStart(2,'0')).join('');
}

export function validateTrainingReport(report,now){
  const generated=Date.parse(report?.generatedAt),asOf=date(report?.asOf),cutoff=date(report?.trainingCutoffExclusive),latest=date(report?.latestTrainingMatch);
  if(report?.schemaVersion!==1||report.protocol!==PROTOCOL||report.status!=='success'||report.promotedToDefault!==false||
    report.prospectiveValidationRequired!==true||report.repository!=='FoXsYsOfficial1909/Footpredict'||
    !HEX.test(report.modelSha256)||!HEX.test(report.dataSha256)||!HEX.test(report.codeSha256)||report.settingsSha256!==NIGHTLY_SETTINGS_SHA||
    !COMMIT.test(report.sourceCommit)||!/^\d+$/.test(report.runId)||!/^\d+$/.test(report.runAttempt)||
    !/^ensemble-candidate-\d{4}-\d{2}-\d{2}-[0-9a-f]{12}$/.test(report.modelVersion)||!report.modelVersion.startsWith('ensemble-candidate-'+report.asOf+'-')||
    !integer(report.trainingMatches,200,100000)||!integer(report.poissonTrainingMatches,200,report.trainingMatches)||
    !integer(report.evaluation?.commonMatches,60,1000)||!Number.isFinite(latest)||!Number.isFinite(asOf)||
    cutoff!==asOf-DAY||latest>=cutoff||!Number.isFinite(generated)||generated<asOf||generated>=asOf+DAY||generated>now)
    fail('Отчёт ночного кандидата не прошёл проверку происхождения и среза данных.');
  if(now-generated>NIGHTLY_MAX_AGE_HOURS*HOUR)fail('Последний проверенный ночной кандидат старше 36 часов. Прогноз отложен.');
  return report;
}

export async function verifyNightlyArtifact(report,modelJson,now){
  validateTrainingReport(report,now);
  if(typeof modelJson!=='string'||new TextEncoder().encode(modelJson).length>100000||await modelDigest(modelJson)!==report.modelSha256)
    fail('Контрольная сумма ночного кандидата не совпадает с отчётом.');
  let model;try{model=JSON.parse(modelJson);}catch{fail('Файл ночного кандидата содержит некорректный JSON.');}
  const glm=model?.glm,elo=model?.elo,fit=glm?.fit;
  if(model?.protocol!==PROTOCOL||model.status!=='candidate'||model.promoted_to_default!==false||model.news_used_in_probabilities!==false||
    model.model_version!==report.modelVersion||model.as_of!==report.asOf||model.training_cutoff_exclusive!==report.trainingCutoffExclusive||
    model.glm_weight!==.75||glm?.as_of!==report.trainingCutoffExclusive||elo?.as_of!==report.trainingCutoffExclusive||
    glm?.parameters?.half_life!==365||glm?.parameters?.ridge!==.03||glm?.parameters?.window_days!==1095||
    glm?.goal_cap!==5||glm?.poisson_max!==20||elo?.parameters?.k!==30||elo?.parameters?.home_advantage!==80||elo?.parameters?.draw_weight!==.9||
    !number(glm?.intercept,-10,10)||!number(glm?.home_advantage,-10,10)||
    fit?.training_games!==report.poissonTrainingMatches||!Number.isFinite(date(fit?.latest_training_date))||fit.latest_training_date>=report.trainingCutoffExclusive||
    elo?.training_games!==report.trainingMatches||elo?.latest_training_date!==report.latestTrainingMatch||
    JSON.stringify(model.features_used)!==JSON.stringify(['past_match_results','opponent_strength','home_advantage'])||
    !record(glm?.teams)||!record(elo?.ratings)||!record(model.aliases))fail('Параметры ночного кандидата не соответствуют протоколу.');
  const names=Object.keys(glm.teams),ratings=Object.entries(elo.ratings),aliases=Object.entries(model.aliases);
  if(names.length<2||names.length>100||ratings.length<2||ratings.length>100||aliases.length>100||
    names.some(name=>!name||name.length>100||!number(glm.teams[name]?.attack,-10,10)||!number(glm.teams[name]?.defence,-10,10))||
    ratings.some(([name,value])=>!name||name.length>100||!number(value,0,5000))||
    aliases.some(([name,value])=>!name||name.length>100||typeof value!=='string'||!value||value.length>100))
    fail('Некорректные силы команд ночного кандидата.');
  return model;
}

async function publicText(services,url,limit){
  let response;
  try{response=await services.fetch(url,{headers:{Accept:'application/json'},credentials:'omit',redirect:'error',cache:'no-store',signal:AbortSignal.timeout(15000)});}
  catch{fail('Публичный архив ночного обучения временно недоступен.');}
  if(!response.ok)fail('Архив ночного обучения вернул HTTP '+response.status+'.');
  try{return await services.readLimited(response.body,limit);}
  catch{fail('Не удалось прочитать ограниченный снимок ночного обучения.');}
}

export async function loadNightlyModel(services){
  const reportUrl=TRAINING_BASE+'latest.json';
  const raw=await publicText(services,reportUrl,100000),reportFetchedAt=new Date(services.now()).toISOString();
  let packet;try{packet=JSON.parse(raw);}catch{fail('Публичный отчёт обучения содержит некорректный JSON.');}
  if(packet?.schemaVersion!==1)fail('Неизвестная версия отчёта ночного обучения.');
  const report=validateTrainingReport(packet.latestSuccessful,services.now());
  // The URL comes only from the fixed repository and validated hash, never a
  // redirect or a URL supplied in untrusted JSON. No credentials are sent.
  const modelUrl=TRAINING_BASE+'models/'+report.modelSha256+'.json';
  const modelJson=await publicText(services,modelUrl,100000),modelFetchedAt=new Date(services.now()).toISOString();
  const model=await verifyNightlyArtifact(report,modelJson,services.now());
  return {model,modelJson,report,modelUrl,modelFetchedAt,reportUrl,reportFetchedAt};
}
