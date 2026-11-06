// Lazy, read-only source freshness; the historical forecast packet is separate.
export const XG_REPORT_URL = 'https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/xg-data-reports/latest.json';

export function describeXgData(packet) {
  const r=packet?.latestAttempt,c=r?.coverage;
  if(packet?.schemaVersion!==1||!['success','stale','failed'].includes(r?.status)||!Number.isFinite(Date.parse(r.generatedAt)))
    throw new Error('Invalid xG freshness report');
  const date=value=>{
    if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value)||!Number.isFinite(Date.parse(value)))throw new Error('Invalid xG data date');
    return new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'long',year:'numeric',timeZone:'UTC'}).format(new Date(value+'T00:00:00Z'));
  };
  const checked=new Intl.DateTimeFormat('ru-RU',{day:'numeric',month:'long',hour:'2-digit',minute:'2-digit',timeZone:'Europe/Moscow'}).format(new Date(r.generatedAt));
  if(r.status==='failed'&&!c)return `Проверка источников не завершилась. Полнота истории xG не подтверждена. Проверено: ${checked} МСК.`;
  if(!c||![c.completedMatches,c.matchedXg,c.missingXg].every(v=>Number.isSafeInteger(v)&&v>=0)||
     c.matchedXg+c.missingXg!==c.completedMatches)throw new Error('Invalid xG coverage');
  const coverage=`xG есть у ${c.matchedXg} из ${c.completedMatches} сыгранных матчей сезона.`;
  const last=c.latestXgDate?` Последний матч с xG: ${date(c.latestXgDate)}.`:' Дата последних xG неизвестна.';
  const status=r.status==='failed'?' Проверка источников не завершилась; сохранена предыдущая история.':
    r.status==='stale'?' История xG пока неполная или устарела.':'';
  return coverage+last+status+` Проверено: ${checked} МСК.`;
}

const details=globalThis.document?.getElementById('xgResearch');
if(details){
  let loading=false,loaded=false,lastText='';
  const target=document.getElementById('xgDataStatus');
  const load=async()=>{
    if(loading)return;
    loading=true;target.textContent='Проверяю свежесть истории xG…';
    try{
      const response=await fetch(XG_REPORT_URL,{credentials:'omit',cache:'no-store',redirect:'error',signal:AbortSignal.timeout(15000)});
      if(!response.ok)throw new Error('Unavailable');
      lastText=describeXgData(await response.json());target.textContent=lastText;loaded=true;
    }catch{target.textContent=lastText?lastText+' Повторная проверка сейчас недоступна.':'Статус обновления xG сейчас недоступен. Дата проверки источника не подтверждена.';}
    finally{loading=false;}
  };
  details.addEventListener('toggle',()=>{if(details.open&&!loaded)load();});
  document.getElementById('refreshXgData')?.addEventListener('click',load);
}
