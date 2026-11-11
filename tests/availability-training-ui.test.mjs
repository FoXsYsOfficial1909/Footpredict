import test from 'node:test';
import assert from 'node:assert/strict';
import {describeAvailabilityExperiment} from '../availability-training-ui.js';
const good={schemaVersion:1,protocol:'availability-residual-1',latestAttempt:{status:'collecting',promotedToDefault:false,
  generatedAt:'2026-11-11T12:00:00Z',minimumTrainingMatches:60,counts:{snapshots:146,fixtures:20,
    fplAvailableFixtures:9,minutesReadyFixtures:0,eligibleDayAheadFixtures:0,settledTrainingFixtures:0},
  prospective:{registered:0,n:0}}};
test('readiness distinguishes repeated cards, usable fixtures and an actually trained correction',()=>{
 const text=describeAvailabilityExperiment(good);
 assert.match(text,/146 срезов для 20 матчей/);assert.match(text,/0 \/ 60/);
 assert.match(text,/ещё не обучена/);assert.match(text,/Основные вероятности v1 пока прежние/);
 assert.match(text,/МСК/);
});
test('a failed check cannot claim readiness and retains the old successful data explicitly',()=>{
 const packet={...good,latestAttempt:{status:'failed',promotedToDefault:false},latestSuccessful:good.latestAttempt};
 assert.match(describeAvailabilityExperiment(packet),/Свежая проверка не удалась/);
 assert.match(describeAvailabilityExperiment({...packet,latestSuccessful:null}),/Успешного отчёта пока нет/);
 assert.throws(()=>describeAvailabilityExperiment({...good,latestAttempt:{...good.latestAttempt,promotedToDefault:true}}));
 assert.throws(()=>describeAvailabilityExperiment({...good,latestAttempt:{...good.latestAttempt,counts:{...good.latestAttempt.counts,minutesReadyFixtures:-1}}}));
});
