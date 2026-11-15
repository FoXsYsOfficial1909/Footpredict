"""Check the published training summary without starting training or research."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright

OUTPUT=Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT',tempfile.mkdtemp(prefix='footpredict-training-')))
OUTPUT.mkdir(parents=True,exist_ok=True)
BASE=os.environ.get('FOOTPREDICT_TEST_URL','http://127.0.0.1:8000/').split('#')[0]
FIXTURE=os.environ.get('FOOTPREDICT_TRAINING_FIXTURE')
PATTERN='**/training-reports/latest.json*'

with sync_playwright() as playwright:
    options={'executable_path':'/usr/bin/chromium','headless':True,'args':['--no-sandbox']}
    if os.environ.get('HTTPS_PROXY'):options['proxy']={'server':os.environ['HTTPS_PROXY'],'bypass':'127.0.0.1,localhost'}
    browser=playwright.chromium.launch(**options)
    try:
        page=browser.new_page(viewport={'width':1280,'height':960});errors=[];reports=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('request',lambda request:reports.append(request.url) if 'training-reports/latest.json' in request.url else None)
        if FIXTURE:page.route(PATTERN,lambda route:route.fulfill(status=200,content_type='application/json',body=Path(FIXTURE).read_text()))
        page.goto(BASE+'#qualityNightly',wait_until='networkidle',timeout=45000)
        assert reports==[]
        page.locator('#trainingStatus > summary').click()
        page.wait_for_function("!document.getElementById('downloadTraining').disabled",timeout=35000)
        initial_message=page.locator('#trainingMessage').inner_text()
        assert 'Последний цикл обучения завершён' in initial_message or 'Данные проверены: изменений нет' in initial_message
        with page.expect_download() as event:page.click('#downloadTraining')
        packet=json.loads(Path(event.value.path()).read_text())
        assert packet['latestSuccessful']['promotedToDefault'] is False
        assert packet['latestSuccessful']['evaluation']['commonMatches']>=60
        assert page.locator('#trainingBody table').count()==1
        page.locator('#trainingStatus').screenshot(path=str(OUTPUT/'training-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#trainingStatus').screenshot(path=str(OUTPUT/'training-mobile.png'))
        unchanged=copy.deepcopy(packet)
        unchanged['latestCheck']={'schemaVersion':1,'protocol':'continuous-training-1','status':'unchanged',
            'checkedAt':datetime.now(timezone.utc).isoformat(),'asOf':packet['latestSuccessful']['asOf'],
            'league':'PL','division':'E0','method':'goals','promotedToDefault':False,
            'runUrl':packet['latestAttempt']['runUrl']}
        page.route(PATTERN,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(unchanged)))
        page.click('#refreshTraining');page.wait_for_function("!document.getElementById('refreshTraining').disabled")
        assert 'Повторное обучение не требовалось' in page.locator('#trainingMessage').inner_text()
        unchanged_body=page.locator('#trainingBody').inner_text()
        assert 'Последняя проверка данных:' in unchanged_body
        assert 'Последнее успешное обучение:' in unchanged_body
        assert packet['latestSuccessful']['modelVersion'] in unchanged_body
        assert '03:43, 09:43, 15:43 и 21:43 МСК' in unchanged_body
        expected_fit_date=page.evaluate("value=>new Date(value).toLocaleString('ru-RU',{dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Moscow'})+' МСК'",packet['latestSuccessful']['generatedAt'])
        assert expected_fit_date in unchanged_body
        with page.expect_download() as event:page.click('#downloadTraining')
        after_check=json.loads(Path(event.value.path()).read_text())
        assert after_check['latestSuccessful']==packet['latestSuccessful']
        assert after_check['latestAttempt']==packet['latestAttempt']
        failed_check=copy.deepcopy(unchanged);failed_check['latestCheck']['status']='failed'
        page.route(PATTERN,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(failed_check)))
        page.click('#refreshTraining');page.wait_for_function("!document.getElementById('refreshTraining').disabled")
        assert 'Последняя проверка данных завершилась ошибкой' in page.locator('#trainingMessage').inner_text()
        assert 'Предыдущий успешный результат сохранён' in page.locator('#trainingMessage').inner_text()
        assert packet['latestSuccessful']['modelVersion'] in page.locator('#trainingBody').inner_text()
        first_failure={'schemaVersion':1,'latestCheck':failed_check['latestCheck'],'latestSuccessful':None}
        page.route(PATTERN,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(first_failure)))
        page.click('#refreshTraining');page.wait_for_function("!document.getElementById('refreshTraining').disabled")
        assert 'Последняя проверка данных завершилась ошибкой' in page.locator('#trainingMessage').inner_text()
        assert 'Последний цикл обучения завершён' not in page.locator('#trainingMessage').inner_text()
        assert 'Успешный кандидат пока не опубликован' in page.locator('#trainingBody').inner_text()
        assert page.locator('#trainingBody table').count()==0
        failure=copy.deepcopy(packet);failure['latestAttempt']={**failure['latestAttempt'],'status':'failed'}
        failure.pop('latestCheck',None)
        page.route(PATTERN,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(failure)))
        page.click('#refreshTraining');page.wait_for_function("!document.getElementById('refreshTraining').disabled")
        assert 'Предыдущий успешный результат сохранён' in page.locator('#trainingMessage').inner_text()
        assert packet['latestSuccessful']['modelVersion'] in page.locator('#trainingBody').inner_text()
        malicious=copy.deepcopy(packet)
        malicious['latestSuccessful']['modelVersion']='<img src=x onerror="window.__trainingXss=1">'
        malicious['latestSuccessful']['artifactsUrl']='javascript:window.__trainingXss=1'
        page.route(PATTERN,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(malicious)))
        page.click('#refreshTraining');page.wait_for_function("!document.getElementById('refreshTraining').disabled")
        assert page.locator('#trainingBody img').count()==0
        assert page.locator('#trainingBody a').last.get_attribute('href') is None
        assert page.evaluate('window.__trainingXss || 0')==0
        assert not errors,errors
        result={'modelVersion':packet['latestSuccessful']['modelVersion'],'trainingMatches':packet['latestSuccessful']['trainingMatches'],
            'commonMatches':packet['latestSuccessful']['evaluation']['commonMatches'],'lazyLoading':True,'failureRetainsCandidate':True,
            'unchangedRetainsOriginalFit':True,'failedCheckRetainsCandidate':True,'firstFailedCheckIsNotTraining':True,
            'mobileOverflow':False,'jsErrors':errors}
        (OUTPUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print('TRAINING_PASS',json.dumps(result,ensure_ascii=False))
    finally:browser.close()
