import test from 'node:test';
import assert from 'node:assert/strict';
import {describeTrainerAgents} from '../trainer-agents-ui.js';

const success={schemaVersion:1,protocol:'trainer-agents-1',status:'success',checkedAt:'2026-11-16T12:00:00Z',
  trainedAt:'2026-11-16T06:00:00Z',trainingMatches:13070,modelVersion:'neural-agent-example',promotedToDefault:false,
  policy:{automaticPromotion:false,paidAIRequests:0},roles:{trainer:{parameterCount:963,fits:0,status:'unchanged',
    converged:false,stopReason:'iteration_limit'},examiner:{publicPromotionAllowed:false}},
  prospective:{registered:5,pending:5,settled:0,review:0,unavailable:0}};
const packet=()=>({schemaVersion:1,protocol:'trainer-agents-1',latestAttempt:structuredClone(success),latestSuccessful:structuredClone(success)});

test('trainer checks do not pretend to be new fits or future wins',()=>{
  const result=describeTrainerAgents(packet(),Date.parse('2026-11-16T12:30:00Z'));
  assert.match(result,/повторная подгонка не требовалась/);assert.match(result,/завершено 0/);
  assert.match(result,/сходимость не заявляется/);assert.match(result,/автоматически не заменяется/);
});
test('failed cycle preserves the old model and first failure has no invented weights',()=>{
  const p=packet();p.latestAttempt.status='failed';
  assert.match(describeTrainerAgents(p),/Предыдущие веса и прогнозы сохранены/);
  assert.match(describeTrainerAgents(p),/neural-agent-example/);
  p.latestSuccessful=null;assert.match(describeTrainerAgents(p),/Первый успешный цикл ещё не опубликован/);
});
test('wrong scope, automatic deployment and mismatched paired counts are rejected',()=>{
  for(const change of [p=>p.protocol='other',p=>p.latestAttempt.promotedToDefault=true,
    p=>p.latestAttempt.policy.paidAIRequests=10,p=>p.latestSuccessful.prospective.settled=1,
    p=>p.latestSuccessful.roles.trainer.fits=3]){
    const p=packet();change(p);assert.throws(()=>describeTrainerAgents(p));
  }
});
