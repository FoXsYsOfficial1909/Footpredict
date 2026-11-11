// Lazy, public view of the fixed hundred; viewing never recalculates predictions.
export const HUNDRED_RESULTS='https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/hundred-results/latest.json';
const PROTOCOL='hundred-future-1';

export function verifiedHundredResults(envelope,packet,checksum){
  if(envelope?.schemaVersion!==1||envelope.protocol!==PROTOCOL||envelope.packetSha256!==checksum)throw new Error('Different cohort');
  const failed=envelope.latestAttempt?.status==='failed';
  const report=failed?envelope.latestSuccessful:envelope.latestAttempt;
  if(!report)return {report:null,failed};
  if(report.protocol!==PROTOCOL||report.packetSha256!==checksum||report.forecastsChanged!==false||report.total!==100||
    !Number.isFinite(Date.parse(report.checkedAt))||!Array.isArray(report.fixtures)||report.fixtures.length!==100)throw new Error('Invalid result observation');
  const ids=new Set(packet.fixtures.map(f=>f.fixtureId)),seen=new Set();
  for(const row of report.fixtures){
    if(!ids.has(row.fixtureId)||seen.has(row.fixtureId)||!['pending','settled','review','unavailable'].includes(row.status))throw new Error('Invalid result fixture');
    seen.add(row.fixtureId);
    if(row.status==='settled'){
      if(![row.score?.home,row.score?.away].every(n=>Number.isInteger(n)&&n>=0&&n<=30)||
        row.outcome!==(row.score.home>row.score.away?0:row.score.home===row.score.away?1:2))throw new Error('Unverified score');
    }
  }
  return {report,failed};
}

export function hundredSummary(packet,results=[]){
  const map=new Map(results.map(r=>[r.fixtureId,r]));let settled=0,correct=0,eligible=0,review=0;
  for(const f of packet.fixtures){
    const row=map.get(f.fixtureId);if(row?.status==='review')review++;
    if(row?.status!=='settled')continue;
    settled++;const maximum=Math.max(...f.probabilities),winners=f.probabilities.map((p,i)=>Math.abs(p-maximum)<=1e-12?i:null).filter(i=>i!==null);
    if(winners.length===1){eligible++;if(winners[0]===row.outcome)correct++;}
  }
  return {total:packet.fixtures.length,settled,correct,eligible,review,accuracy:eligible?correct/eligible:null};
}

const section=globalThis.document?.getElementById('hundredSeries');
if(section){
  const $=id=>document.getElementById(id),el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
  const names={BSA:'Бразилия · Серия А',ELC:'Англия · Чемпионшип',PD:'Испания · Ла Лига',PL:'АПЛ'};
  const date=value=>new Date(value).toLocaleString('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Moscow'})+' МСК';
  let packet=null,rows=[],limit=10,loading=false,loaded=false;
  function render(){
    const map=new Map(rows.map(r=>[r.fixtureId,r])),summary=hundredSummary(packet,rows);
    $('hundredSummary').textContent='Зафиксировано 100 · проверено '+summary.settled+' / 100'+
      (summary.eligible?' · самый вероятный исход совпал: '+summary.correct+' / '+summary.eligible:'')+
      (summary.review?' · требуют проверки: '+summary.review:'');
    $('hundredFrozen').textContent='Вероятности сохранены '+date(packet.frozenAt)+'. Основное время · П1 / ничья / П2. Модель poisson-1.0.0.';
    const target=$('hundredRows');target.replaceChildren();
    for(const f of packet.fixtures.slice(0,limit)){
      const row=map.get(f.fixtureId),card=el('article',undefined,'hundred-row');
      card.append(el('h3',f.home+' — '+f.away),el('p',names[f.league]+' · '+date(f.kickoff),'research-note'));
      const probs=el('div',undefined,'hundred-probabilities'),maximum=Math.max(...f.probabilities);
      f.probabilities.forEach((p,i)=>{const box=el('div',undefined,Math.abs(p-maximum)<=1e-12?'is-leading':'');
        box.append(el('span',['П1','Ничья','П2'][i]),el('strong',(p*100).toFixed(1).replace('.',',')+'%'));probs.append(box);});
      card.append(probs);
      card.append(el('p',row?.status==='settled'?'Результат: '+row.score.home+' : '+row.score.away:
        row?.status==='review'?'Перенос или изменение данных · требуется проверка':row?.status==='unavailable'?'Источник пока не подтвердил результат':
        Date.parse(f.kickoff)>Date.now()?'Ожидаем матч':'Ожидаем подтверждённый результат','research-note'));
      target.append(card);
    }
    $('hundredMore').hidden=limit>=100;
    $('hundredMore').textContent='Ещё '+Math.min(10,100-limit)+' матчей';
    $('hundredCsv').disabled=false;
  }
  async function load(){
    if(loaded||loading)return;loading=true;
    try{
      const data=await import('./hundred-cohort-data.js');packet=data.PACKET;
      const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(packet))))].map(n=>n.toString(16).padStart(2,'0')).join('');
      if(hash!==data.PACKET_SHA256||packet.fixtures.length!==100)throw new Error('Frozen packet integrity mismatch');
      render();
      try{
        const response=await fetch(HUNDRED_RESULTS,{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(15000)});
        if(response.status===404){$('hundredChecked').textContent='Первый отчёт сверки готовится. Все исходные вероятности уже зафиксированы.';return;}
        if(!response.ok)throw new Error('Result service unavailable');
        const raw=await response.text();if(raw.length>500000)throw new Error('Oversized result report');
        const {report,failed}=verifiedHundredResults(JSON.parse(raw),packet,data.PACKET_SHA256);
        rows=report?.fixtures||[];render();
        $('hundredChecked').textContent=(failed?'Последняя проверка не удалась. Показаны предыдущие сведения. ':'')+
          (report?'Последняя сверка: '+date(report.checkedAt)+'. ':'')+
          (report?.finalized?'Серия закрыта; исходные прогнозы сохранены.':'Автоматическая сверка — каждые 6 часов.');
        loaded=!failed;
      }catch{$('hundredChecked').textContent='Сверка временно недоступна. Показаны сохранённые прогнозы.';}
    }catch{$('hundredSummary').textContent='Не удалось прочитать зафиксированную серию. Обнови страницу позже.';}
    finally{loading=false;}
  }
  $('hundredMore').addEventListener('click',()=>{limit=Math.min(100,limit+10);render();});
  $('hundredCsv').addEventListener('click',()=>{
    if(!packet)return;
    const map=new Map(rows.map(r=>[r.fixtureId,r]));
    const field=v=>'"'+String(v??'').replace(/"/g,'""')+'"';
    const csv=[['Матч ID','Лига','Начало UTC','Хозяева','Гости','П1 %','Ничья %','П2 %','Статус','Счёт','Зафиксировано UTC'],
      ...packet.fixtures.map(f=>{const r=map.get(f.fixtureId);return [f.fixtureId,f.league,f.kickoff,f.home,f.away,...f.probabilities.map(p=>(p*100).toFixed(8)),r?.status||'pending',r?.score?r.score.home+':'+r.score.away:'',packet.frozenAt];})]
      .map(row=>row.map(field).join(';')).join('\n');
    const url=URL.createObjectURL(new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8'}));const a=el('a');a.href=url;a.download='Footpredict-100-2026-11-11.csv';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  });
  window.addEventListener('footpredict:tab-selected',event=>{if(event.detail?.tab==='history')void load();});
  if(!document.getElementById('history').hidden)void load();
}
