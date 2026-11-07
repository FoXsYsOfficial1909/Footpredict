import test from 'node:test';
import assert from 'node:assert/strict';
import {translateSourceText} from '../source-translations.js';

test('Russian reports preserve uncertainty and original English verbatim',()=>{
  const cases=[
    ['Thigh injury - Unknown return date','Травма бедра. Срок возвращения неизвестен.'],
    ['Hamstring injury - Expected back 8 Nov','Травма задней поверхности бедра. Ожидаемое возвращение — 8 ноября.'],
    ['Suspended until 29 Nov','Дисквалификация до 29 ноября'],
    ['not included in squad.','Не включён в состав команды.'],
    ['Knock - Expected back 2 Jan','Повреждение. Ожидаемое возвращение — 2 января.'],
    ['Unspecified injury - Unknown return date','Травма, характер не уточнён. Срок возвращения неизвестен.']
  ];
  for(const [original,text] of cases){
    const result=translateSourceText(original);
    assert.equal(result.text,text);assert.equal(result.original,original);
    assert.equal(result.status,'translated');assert.equal(result.method,'glossary');
  }
});

test('supplier participation percentages stay attributed and numerically unchanged',()=>{
  for(const chance of [25,50,75]){
    const result=translateSourceText(`Ankle injury - ${chance}% chance of playing`);
    assert.match(result.text,new RegExp('по оценке FPL — '+chance+'%'));
    assert.ok(!result.text.includes('побед'));assert.ok(!result.text.includes('точно'));
  }
  for(const chance of [0,10,90,100])assert.equal(translateSourceText(`Knock - ${chance}% chance of playing`).status,'unavailable');
  assert.equal(translateSourceText('Knock - 75% chance of playing',{kind:'news'}).status,'unavailable',
    'an unrelated news source must not be attributed to FPL');
});

test('whole messages prevent lost negation, hedges, additions or fabricated return dates',()=>{
  for(const original of [
    'No knee injury - Unknown return date','Not suspended until 29 Nov',
    'Hamstring injury - not expected back 8 Nov','Thigh injury - Expected back 8 Nov, possibly later',
    'Thigh injury - Expected back 8 Nov 2026','Thigh injury - Unknown return date; will play tomorrow',
    'He has a thigh injury - Unknown return date','Hamstring injury - Expected back tomorrow',
    'Suspended until 31 Nov','Suspended until 0 Nov','Suspended until 30 Feb',
    'Cole Palmer is not injured and may play on Saturday.',
    '<img src=x onerror=alert(1)>','Unknown condition - Unknown return date'
  ]){
    const result=translateSourceText(original);
    assert.equal(result.status,'unavailable',original);assert.equal(result.text,null);assert.equal(result.original,original);
  }
});

test('month names are translated without inventing a year or medical clearance',()=>{
  for(const month of ['Jan','February','Mar','Apr','May','Jun','Jul','August','Sep','Oct','Nov','December']){
    const result=translateSourceText('Knock - Expected back 1 '+month);
    assert.equal(result.status,'translated');assert.ok(!/20\d{2}/.test(result.text));
  }
  const past=translateSourceText('Thigh injury - Expected back 31 Oct');
  assert.match(past.text,/Ожидаемое возвращение/);assert.ok(!past.text.includes('здоров'));
});

test('a translation is a display-only projection and never mutates source evidence',()=>{
  const report=Object.freeze({news:'  Knee injury  —  Unknown return date  ',publishedAt:'2026-10-21T12:00:00Z',stale:true});
  const before=JSON.stringify(report),result=translateSourceText(report.news);
  assert.equal(result.status,'translated');assert.equal(result.original,report.news);
  assert.equal(JSON.stringify(report),before);
  const russian='Срок возвращения пока неизвестен.';
  assert.equal(translateSourceText(russian).status,'already_russian');
  assert.equal(translateSourceText(russian).text,russian);
  assert.equal(translateSourceText(null).status,'empty');
  assert.equal(translateSourceText('a'.repeat(601)).status,'unavailable');
  assert.equal(translateSourceText('Suspended until 29 Nov',{kind:'fixture'}).status,'unavailable');
});
