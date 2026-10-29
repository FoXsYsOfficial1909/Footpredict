"""Check the quality screen using only ledger reads; never collect or save forecasts.

Optional FOOTPREDICT_QUALITY_FIXTURE supplies a saved report before API deployment.
"""
import copy
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright

OUTPUT=Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT',tempfile.mkdtemp(prefix='footpredict-quality-')))
OUTPUT.mkdir(parents=True,exist_ok=True)
BASE=os.environ.get('FOOTPREDICT_TEST_URL','http://127.0.0.1:8000/')
fixture=os.environ.get('FOOTPREDICT_QUALITY_FIXTURE')

with sync_playwright() as playwright:
    options={'executable_path':'/usr/bin/chromium','headless':True,'args':['--no-sandbox']}
    if os.environ.get('HTTPS_PROXY'):
        options['proxy']={'server':os.environ['HTTPS_PROXY'],'bypass':'127.0.0.1,localhost'}
    browser=playwright.chromium.launch(**options)
    try:
        page=browser.new_page(viewport={'width':1280,'height':960})
        errors=[];requests=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('request',lambda request:requests.append((request.method,request.url)) if 'workers.dev' in request.url else None)
        if fixture:
            body=Path(fixture).read_text()
            page.route('**/v1/quality',lambda route:route.fulfill(status=200,content_type='application/json',body=body))
        page.goto(BASE,wait_until='networkidle',timeout=60000)
        page.wait_for_function("!document.getElementById('qualityResult').hidden",timeout=35000)
        assert page.locator('.quality-model').count()==2
        assert page.locator('#qualityPaired table').count()==1
        with page.expect_download() as event:page.click('#downloadQuality')
        report=json.loads(Path(event.value.path()).read_text())
        assert report['schemaVersion']==1 and report['generatedAt']
        assert len(report['fixtures'])==report['fixtureScope']['returned']
        m=report['cohorts']['v1']['metrics']
        if m['topEligible']:
            assert f"{m['accuracy']*100:.1f}".replace('.',',')+'%' in page.locator('[data-model=v1]').inner_text()
        if not report['cohorts']['v2']['metrics']['n']:
            assert 'Ждём результаты' in page.locator('[data-model=v2]').inner_text()
        if not report['paired']['models']['v1']['n']:
            assert 'нет общих подтверждённых результатов' in page.locator('#qualityPaired').inner_text()
        page.locator('#qualityDashboard').screenshot(path=str(OUTPUT/'quality-desktop.png'))
        expected_errors=sum(any(row.get(key,{}).get('metrics') and row[key]['metrics']['correct'] is False for key in ['v1','v2']) for row in report['fixtures'])
        assert page.locator('.quality-fixture').count()==min(20,expected_errors)
        if expected_errors:
            page.locator('.quality-fixture summary').first.click()
            assert 'Факт:' in page.locator('.quality-fixture').first.inner_text()
            page.locator('.quality-fixture summary').first.click()
        page.select_option('#qualityFilter','pending');page.select_option('#qualityModelFilter','v2')
        pending=sum(row.get('v2',{}).get('state')=='pending' for row in report['fixtures'])
        assert page.locator('.quality-fixture').count()==min(20,pending)
        assert page.locator('.quality-miss').count()==0
        page.select_option('#qualityView','historical')
        assert page.locator('#qualityLive').is_hidden()
        assert str(report['historical']['common_fixtures']) in page.locator('#qualityHistorical').inner_text()
        assert page.locator('#qualityHistorical table').count()==1
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),'Mobile overflow in historical view'
        page.locator('#qualityDashboard').screenshot(path=str(OUTPUT/'quality-historical-mobile.png'))
        page.select_option('#qualityView','live');page.select_option('#qualityFilter','errors');page.select_option('#qualityModelFilter','all')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),'Mobile overflow in saved view'
        page.locator('#qualityDashboard').screenshot(path=str(OUTPUT/'quality-mobile.png'))

        # A failed refresh preserves the previous dated report, never a fake zero.
        page.route('**/v1/quality',lambda route:route.fulfill(status=503,content_type='application/json',body='{}'))
        page.click('#refreshQuality')
        page.wait_for_function("!document.getElementById('refreshQuality').disabled")
        assert 'Показаны предыдущие данные' in page.locator('#qualityStatus').inner_text()
        assert page.locator('.quality-model').count()==2

        # Source names are text, and filter changes never become executable markup.
        malicious=copy.deepcopy(report)
        if malicious['fixtures']:
            malicious['fixtures'][0]['home']='<img src=x onerror="window.__qualityXss=1">'
            page.route('**/v1/quality',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(malicious)))
            page.click('#refreshQuality');page.wait_for_function("!document.getElementById('refreshQuality').disabled")
            page.select_option('#qualityFilter','all')
            assert page.locator('#qualityFixtures img').count()==0
            assert page.evaluate('window.__qualityXss || 0')==0
            assert '<img src=x' in page.locator('#qualityFixtures').inner_text()
        assert not errors,errors
        fresh=browser.new_page()
        fresh.route('**/*.workers.dev/**',lambda route:route.fulfill(status=503,content_type='application/json',body='{}'))
        fresh.goto(BASE,wait_until='networkidle',timeout=60000)
        fresh.wait_for_function("!document.getElementById('refreshQuality').disabled")
        assert fresh.locator('#qualityResult').is_hidden()
        assert fresh.locator('#downloadQuality').is_disabled()
        assert 'отсутствие ответа не означает нулевую точность' in fresh.locator('#qualityStatus').inner_text()
        fresh.close()
        assert requests and all(method=='GET' for method,_ in requests),requests
        assert all('/v1/research/' not in url and '/fd/' not in url and '/af/' not in url for _,url in requests),requests
        result={'cohorts':report['cohorts'],'paired':report['paired'],'fixtureScope':report['fixtureScope'],
                'api_reads':len(requests),'js_errors':errors,'provider_requests':0,'writes':0}
        (OUTPUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('QUALITY_PASS',json.dumps(result,ensure_ascii=False),flush=True)
    finally:
        browser.close()
