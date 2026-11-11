"""Owner-only readiness, lazy reads, failure preservation and mobile layout."""
import copy
import json
import os
from pathlib import Path
import tempfile
from playwright.sync_api import sync_playwright

BASE=os.environ.get('FOOTPREDICT_TEST_URL','http://127.0.0.1:8000/').split('#')[0]
OUT=Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT',tempfile.mkdtemp(prefix='footpredict-availability-learning-')))
OUT.mkdir(parents=True,exist_ok=True)
REPORT='https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/availability-reports/latest.json'
packet={'schemaVersion':1,'protocol':'availability-residual-1','latestAttempt':{
    'status':'collecting','promotedToDefault':False,'generatedAt':'2026-11-11T12:00:00Z',
    'minimumTrainingMatches':60,'counts':{'snapshots':147,'fixtures':21,'fplAvailableFixtures':10,
        'minutesReadyFixtures':1,'eligibleDayAheadFixtures':0,'settledTrainingFixtures':0},
    'prospective':{'registered':0,'n':0}}}
with sync_playwright() as p:
    options={'executable_path':'/usr/bin/chromium','headless':True,'args':['--no-sandbox']}
    if os.environ.get('HTTPS_PROXY'):options['proxy']={'server':os.environ['HTTPS_PROXY'],'bypass':'127.0.0.1,localhost'}
    browser=p.chromium.launch(**options)
    try:
        page=browser.new_page(viewport={'width':390,'height':844});errors=[];requests=[];reads=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:requests.append((r.method,r.url)))
        def report(route):
            reads.append(route.request.url)
            route.fulfill(status=200,content_type='application/json',body=json.dumps(packet))
        page.route(REPORT,report)
        page.goto(BASE+'#quality',wait_until='networkidle',timeout=60000)
        assert page.get_by_role('tab').count()==3
        assert page.locator('#availabilityTraining').is_hidden() and not reads
        page.goto(BASE+'#qualityNightly',wait_until='networkidle',timeout=60000)
        assert not reads
        page.locator('#availabilityTraining > summary').click()
        page.wait_for_function("document.getElementById('availabilityTrainingBody').textContent.includes('0 / 60')")
        assert len(reads)==1
        text=page.locator('#availabilityTrainingBody').inner_text()
        assert '147 срезов для 21 матчей' in text and 'ещё не обучена' in text and 'v1 пока прежние' in text
        assert 'МСК' in text and 'с игровыми минутами: 1' in text
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#availabilityTraining').screenshot(path=str(OUT/'readiness-mobile.png'))
        previous=copy.deepcopy(packet['latestAttempt'])
        packet={'schemaVersion':1,'protocol':'availability-residual-1',
            'latestAttempt':{'status':'failed','promotedToDefault':False},'latestSuccessful':previous}
        page.reload(wait_until='networkidle');page.locator('#availabilityTraining > summary').click()
        page.wait_for_function("document.getElementById('availabilityTrainingBody').textContent.includes('не удалась')")
        assert '147 срезов' in page.locator('#availabilityTrainingBody').inner_text()
        assert all(method=='GET' for method,_ in requests)
        assert all('/v1/research/' not in url for _,url in requests)
        assert not errors,errors
        result={'publicTabs':3,'ownerOnly':True,'lazyRead':True,'sampleDenominatorClear':True,
            'failedCheckPreservesPrevious':True,'mobileOverflow':False,'jsErrors':errors,'writes':0}
        (OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('AVAILABILITY_LEARNING_PASS',json.dumps(result,ensure_ascii=False),flush=True)
    finally:browser.close()
