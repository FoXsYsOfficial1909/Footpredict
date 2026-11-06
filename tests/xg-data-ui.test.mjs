import test from 'node:test';
import assert from 'node:assert/strict';
import {describeXgData} from '../xg-data-ui.js';

const report=status=>({schemaVersion:1,latestAttempt:{status,generatedAt:'2026-11-06T00:00:00Z',
  coverage:{completedMatches:90,matchedXg:80,missingXg:10,latestXgDate:'2026-10-25'}}});

test('a successful source check cannot hide old or incomplete underlying xG',()=>{
  const text=describeXgData(report('stale'));
  assert.match(text,/80 из 90/);assert.match(text,/25 октября 2026/);
  assert.match(text,/неполная или устарела/);assert.match(text,/Проверено:/);
  assert.doesNotMatch(text,/6 ноября 2026.*Последний матч/);
});

test('a failed refresh reports retained data rather than zero coverage',()=>{
  const text=describeXgData(report('failed'));
  assert.match(text,/80 из 90/);assert.match(text,/сохранена предыдущая история/);
  assert.match(describeXgData({...report('failed'),latestAttempt:{...report('failed').latestAttempt,coverage:null}}),/Полнота истории xG не подтверждена/);
  assert.throws(()=>describeXgData({...report('stale'),latestAttempt:{...report('stale').latestAttempt,
    coverage:{completedMatches:90,matchedXg:80,missingXg:0}}}));
  assert.throws(()=>describeXgData({schemaVersion:1}));
});
