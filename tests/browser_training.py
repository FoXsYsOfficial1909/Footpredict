"""Check the published training summary without starting training or research."""
import copy
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
        page.goto(BASE+'#qualityDashboard',wait_until='networkidle',timeout=45000)
        assert reports==[]
        page.locator('#trainingStatus > summary').click()
        page.wait_for_function("!document.getElementById('downloadTraining').disabled",timeout=35000)
        assert 'Последний цикл обучения завершён' in page.locator('#trainingMessage').inner_text()
        with page.expect_download() as event:page.click('#downloadTraining')
        packet=json.loads(Path(event.value.path()).read_text())
        assert packet['latestSuccessful']['promotedToDefault'] is False
        assert packet['latestSuccessful']['evaluation']['commonMatches']>=60
        assert page.locator('#trainingBody table').count()==1
        page.locator('#trainingStatus').screenshot(path=str(OUTPUT/'training-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#trainingStatus').screenshot(path=str(OUTPUT/'training-mobile.png'))
        failure=copy.deepcopy(packet);failure['latestAttempt']={**failure['latestAttempt'],'status':'failed'}
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
            'commonMatches':packet['latestSuccessful']['evaluation']['commonMatches'],'lazyLoading':True,'failureRetainsCandidate':True,'mobileOverflow':False,'jsErrors':errors}
        (OUTPUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print('TRAINING_PASS',json.dumps(result,ensure_ascii=False))
    finally:browser.close()
