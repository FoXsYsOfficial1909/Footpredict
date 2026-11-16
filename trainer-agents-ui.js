const REPORT='https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/trainer-agent-reports/latest.json';
const date=value=>Number.isFinite(Date.parse(value))?new Date(value).toLocaleString('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Moscow'})+' МСК':'—';

export function describeTrainerAgents(packet,now=Date.now()){
  const attempt=packet?.latestAttempt,success=packet?.latestSuccessful;
  if(packet?.schemaVersion!==1||packet.protocol!=='trainer-agents-1'||!attempt||
    !['success','failed'].includes(attempt.status)||attempt.promotedToDefault!==false||
    attempt.policy?.automaticPromotion!==false||attempt.policy?.paidAIRequests!==0||
    !Number.isFinite(Date.parse(attempt.checkedAt)))throw new Error('Invalid trainer agent report');
  const lines=['Автотренеры · проверка каждые 6 часов',
    'Последняя проверка: '+date(attempt.checkedAt)+'.'];
  if(now-Date.parse(attempt.checkedAt)>12*3600000)lines.push('Проверка старше 12 часов. Состояние запуска доступно в журнале GitHub.');
  if(attempt.status==='failed')lines.push('Последний цикл завершился ошибкой. Предыдущие веса и прогнозы сохранены.');
  if(!success){lines.push('Первый успешный цикл ещё не опубликован.');return lines.join('\n');}
  const trainer=success.roles?.trainer,observer=success.prospective;
  if(success.status!=='success'||success.protocol!=='trainer-agents-1'||success.promotedToDefault!==false||
    success.roles?.examiner?.publicPromotionAllowed!==false||trainer?.parameterCount!==963||
    ![0,2].includes(trainer.fits)||!Number.isFinite(Date.parse(success.trainedAt))||
    !['registered','settled','pending','review','unavailable'].every(k=>Number.isSafeInteger(observer?.[k])&&observer[k]>=0)||
    observer.registered!==observer.settled+observer.pending+observer.review+observer.unavailable)throw new Error('Incomplete trainer state');
  lines.push('Нейросеть: '+success.modelVersion+'.',
    'Последнее обучение: '+date(success.trainedAt)+'; примеров: '+success.trainingMatches+'.');
  if(trainer.status==='unchanged')lines.push('Данные и рецепт прежние; повторная подгонка не требовалась.');
  if(trainer.converged===false&&trainer.stopReason==='iteration_limit')lines.push('Обучение остановлено по заданному лимиту итераций; сходимость не заявляется.');
  lines.push('Будущая проверка АПЛ: сохранено '+observer.registered+', ожидают '+observer.pending+', завершено '+observer.settled+'.',
    'Контролёр оставляет сеть исследовательским кандидатом. Основная модель автоматически не заменяется.');
  if(observer.review+observer.unavailable)lines.push('Требуют проверки или недоступны: '+(observer.review+observer.unavailable)+'.');
  return lines.join('\n');
}

if(typeof document!=='undefined'){
  let loaded=false,requestId=0;
  async function refresh(){
    const token=++requestId,target=document.getElementById('trainerAgentsBody');
    try{
      const response=await fetch(REPORT+'?t='+Date.now(),{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(20000)});
      if(response.status===404){if(token===requestId)target.textContent='Первый отчёт автотренеров ещё не опубликован.';return;}
      if(!response.ok)throw new Error('Report unavailable');
      const raw=await response.text();if(raw.length>500000)throw new Error('Report too large');
      const message=describeTrainerAgents(JSON.parse(raw));
      if(token===requestId){target.textContent=message;loaded=true;}
    }catch{if(token===requestId)target.textContent='Не удалось обновить состояние автотренеров. Последние веса сохраняются; проверь журнал запуска.';}
  }
  const parent=document.getElementById('trainingStatus');
  parent.addEventListener('toggle',()=>{if(parent.open&&!loaded)refresh();});
  document.getElementById('refreshTraining').addEventListener('click',refresh);
}
