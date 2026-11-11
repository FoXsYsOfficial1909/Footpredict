// Owner-only, read-only visibility into the availability experiment.
export const AVAILABILITY_REPORT='https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/availability-reports/latest.json';
export function describeAvailabilityExperiment(packet){
  if(packet?.schemaVersion!==1||packet.protocol!=='availability-residual-1')throw new Error('Invalid report');
  const attempt=packet.latestAttempt;
  if(!attempt||!['collecting','candidate','failed'].includes(attempt.status)||attempt.promotedToDefault!==false)
    throw new Error('Invalid experiment state');
  const report=attempt.status==='failed'?packet.latestSuccessful:attempt;
  if(!report)return 'Последняя проверка не завершилась. Успешного отчёта пока нет.';
  const c=report.counts;
  for(const key of ['snapshots','fixtures','fplAvailableFixtures','minutesReadyFixtures','eligibleDayAheadFixtures','settledTrainingFixtures'])
    if(!Number.isSafeInteger(c?.[key])||c[key]<0)throw new Error('Invalid sample count');
  if(!Number.isSafeInteger(report.minimumTrainingMatches)||report.minimumTrainingMatches<60)throw new Error('Invalid training threshold');
  if(!Number.isFinite(Date.parse(report.generatedAt)))throw new Error('Invalid report date');
  const date=new Date(report.generatedAt).toLocaleString('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Moscow'})+' МСК';
  const lines=[attempt.status==='failed'?'Свежая проверка не удалась. Показан предыдущий отчёт.':
    report.status==='candidate'?'Поправка обучена и проверяется отдельно.':'Идёт накопление пригодных данных; поправка ещё не обучена.',
    `Архив: ${c.snapshots} срезов для ${c.fixtures} матчей. Со статусами FPL: ${c.fplAvailableFixtures}; с игровыми минутами: ${c.minutesReadyFixtures}.`,
    `Пригодных завершённых матчей за сутки до игры: ${c.settledTrainingFixtures} / ${report.minimumTrainingMatches} для первой попытки обучения.`,
    'Карточки без сохранённых игровых минут исключены. Новые сведения не подставляются в прошлые снимки.',
    'Поправка использует сообщения FPL и долю минут игроков. Стартовые составы сохраняются для отдельного позднего прогноза.',
    'Сравнение идёт с экспериментальной v2. Основные вероятности v1 пока прежние. Проверено: '+date+'.'];
  if(report.prospective&&Number.isSafeInteger(report.prospective.registered)&&Number.isSafeInteger(report.prospective.n))
    lines.splice(3,0,`Заранее сохранено прогнозов поправки: ${report.prospective.registered}; подтверждённых результатов: ${report.prospective.n}.`);
  return lines.join('\n\n');
}
const details=globalThis.document?.getElementById('availabilityTraining');
if(details){
  let loaded=false,loading=false;
  const target=document.getElementById('availabilityTrainingBody');
  details.addEventListener('toggle',async()=>{
    if(!details.open||loaded||loading)return;
    loading=true;target.textContent='Загружаю состояние эксперимента…';
    try{
      const response=await fetch(AVAILABILITY_REPORT,{credentials:'omit',cache:'no-store',signal:AbortSignal.timeout(15000)});
      if(response.status===404){target.textContent='Первый отчёт эксперимента ещё не опубликован.';return;}
      if(!response.ok)throw new Error('Unavailable report');
      const text=await response.text();if(text.length>100000)throw new Error('Report too large');
      target.textContent=describeAvailabilityExperiment(JSON.parse(text));loaded=true;
    }catch{target.textContent='Не удалось прочитать состояние эксперимента. Это не означает готовность модели.';}
    finally{loading=false;}
  });
}
