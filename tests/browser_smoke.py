"""Run against the local HTTP server and real read-only APIs; never save predictions."""
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT',tempfile.mkdtemp(prefix='footpredict-browser-')))
OUTPUT.mkdir(parents=True,exist_ok=True)


def log(name,value):print(name,json.dumps(value,ensure_ascii=False),flush=True)


with sync_playwright() as playwright:
    options={'executable_path':'/usr/bin/chromium','headless':True,'args':['--no-sandbox']}
    if os.environ.get('HTTPS_PROXY'):options['proxy']={'server':os.environ['HTTPS_PROXY'],'bypass':'127.0.0.1,localhost'}
    browser=playwright.chromium.launch(**options)
    try:
        page=browser.new_page(viewport={'width':1280,'height':900})
        errors=[];failures=[];api=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('requestfailed',lambda request:failures.append({'url':request.url,'error':request.failure}))
        page.on('response',lambda response:api.append({'url':response.url,'http':response.status}) if 'workers.dev' in response.url else None)
        response=page.goto('http://127.0.0.1:8000/',wait_until='networkidle',timeout=45000)
        assert response.status==200 and response.body()==(ROOT/'index.html').read_bytes()
        assert page.locator('#loadResearch').is_disabled()
        page.wait_for_function("!document.getElementById('historyStatus').textContent.startsWith('Загружаю')",timeout=35000)
        assert 'Загружено с сервера записей:' in page.locator('#historyStatus').inner_text()
        log('HISTORY',page.locator('#historyStatus').inner_text())
        page.wait_for_function("!document.getElementById('refreshAutomation').disabled",timeout=35000)
        assert 'Не удалось' not in page.locator('#automationStatus').inner_text(),page.locator('#automationStatus').inner_text()
        assert page.locator('.automation-stats').count()>=1
        page.locator('#automationReport summary').click()
        assert page.locator('#automationReportBody').inner_text()
        if page.locator('#downloadDailyReport').is_enabled():
            with page.expect_download() as report_event:page.click('#downloadDailyReport')
            daily=json.loads(Path(report_event.value.path()).read_text())
            assert daily['generatedAt'] and daily['scheduledFor'] and daily['day']
            log('DAILY_REPORT',{'day':daily['day'],'generatedAt':daily['generatedAt'],'lateBySeconds':daily['lateBySeconds']})
        log('AUTOMATION',page.locator('#automationStatus').inner_text())
        page.locator('#serviceStatus').screenshot(path=str(OUTPUT/'automation-desktop.png'))

        def idle():page.wait_for_function("!document.getElementById('loadCompetitions').disabled",timeout=65000)

        page.click('#loadCompetitions');idle();page.select_option('#competition','PL')
        page.click('#loadMatches');idle()
        page.locator('#content button').first.click();idle()
        assert page.locator('#results').is_visible() and page.locator('#loadResearch').is_enabled()
        original_score=page.locator('#score').inner_text()
        log('V1',{'match':page.locator('#matchName').inner_text(),'score':original_score})
        page.click('#loadResearch')
        page.wait_for_function("!document.getElementById('loadResearch').disabled",timeout=90000)
        assert page.locator('#researchResult').is_visible(),page.locator('#researchStatus').inner_text()
        assert page.locator('.research-team').count()==2
        assert page.locator('.research-article').count()>0
        assert page.locator('#score').inner_text()==original_score
        page.locator('#researchCandidate summary').click()
        assert page.locator('.candidate-probabilities strong').count()==3
        probs=[float(text.replace('%','').replace(',','.')) for text in page.locator('.candidate-probabilities strong').all_text_contents()]
        assert abs(sum(probs)-100)<.2
        log('RESEARCH',{'status':page.locator('#researchStatus').inner_text(),'articles':page.locator('.research-article').count(),
                        'coverage':page.locator('#researchCoverage').inner_text(),'candidate_probabilities':probs})
        with page.expect_download() as download_event:page.click('#exportResearch')
        download=download_event.value
        packet=json.loads(Path(download.path()).read_text())
        assert packet['fixture']['id'] and len(packet['teams'])==2
        assert packet['candidate']['promotedToDefault'] is False
        page.locator('#matchResearch').screenshot(path=str(OUTPUT/'research-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'Mobile horizontal overflow'
        page.locator('#matchResearch').screenshot(path=str(OUTPUT/'research-mobile.png'))
        page.locator('#serviceStatus').screenshot(path=str(OUTPUT/'automation-mobile.png'))
        assert not errors,errors
        assert not failures,failures
        assert api and all(item['http']==200 for item in api),api
        log('LIVE_PASS',{'api_requests':api,'js_errors':errors,'screenshots':str(OUTPUT)})

        # Untrusted source text is rendered as text; executable URLs are discarded.
        malicious=json.loads(json.dumps(packet))
        malicious['articles']=[{'title':'<img src=x onerror="window.__researchXss=1">','summary':'<script>window.__researchXss=1</script>',
                                'url':'javascript:window.__researchXss=1','source':'Test source','team':'Chelsea FC','publishedAt':'2026-10-27T12:00:00Z'}]
        page.route('**/v1/research/**',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(malicious)))
        page.click('#loadResearch');page.wait_for_function("!document.getElementById('loadResearch').disabled")
        assert page.locator('#researchNews img').count()==0
        assert page.locator('#researchNews a').first.get_attribute('href') is None
        assert page.evaluate('window.__researchXss || 0')==0
        page.select_option('#competition','CL')
        assert page.locator('#loadResearch').is_disabled() and not page.locator('#researchResult').is_visible()
        log('SAFETY_PASS','Untrusted text/URL rendering, selection reset, source export and mobile layout verified. No prediction writes.')
    finally:browser.close()
