"""Check candidate comparisons using public ledger reads, never new forecasts.

Optional preview files are local test fixtures, not published observations.
"""
import copy
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright

OUTPUT=Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT',tempfile.mkdtemp(prefix='footpredict-candidate-')))
OUTPUT.mkdir(parents=True,exist_ok=True)
BASE=os.environ.get('FOOTPREDICT_TEST_URL','http://127.0.0.1:8000/').split('#')[0]
FIXTURE=os.environ.get('FOOTPREDICT_QUALITY_FIXTURE')
EVIDENCE=os.environ.get('FOOTPREDICT_CANDIDATE_FIXTURE')
instant=lambda value:datetime.fromisoformat(value.replace('Z','+00:00'))

with sync_playwright() as playwright:
    options={'executable_path':'/usr/bin/chromium','headless':True,'args':['--no-sandbox']}
    if os.environ.get('HTTPS_PROXY'):options['proxy']={'server':os.environ['HTTPS_PROXY'],'bypass':'127.0.0.1,localhost'}
    browser=playwright.chromium.launch(**options)
    try:
        page=browser.new_page(viewport={'width':1280,'height':960});errors=[];requests=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('request',lambda request:requests.append((request.method,request.url)) if 'workers.dev' in request.url else None)
        if FIXTURE:page.route('**/v1/quality',lambda route:route.fulfill(status=200,content_type='application/json',body=Path(FIXTURE).read_text()))
        if EVIDENCE:page.route('**/v1/candidate-comparison/*/evidence',lambda route:route.fulfill(status=200,content_type='application/json',body=Path(EVIDENCE).read_text()))
        page.goto(BASE+'#qualityNightly',wait_until='networkidle',timeout=60000)
        page.wait_for_function("!document.getElementById('qualityResult').hidden",timeout=35000)
        assert page.locator('#tab-quality').get_attribute('aria-selected')=='true'
        assert page.locator('#qualityNightly').is_visible()
        assert 'Ночной кандидат' in page.locator('#qualityNightly h3').inner_text()
        with page.expect_download() as event:page.click('#downloadQuality')
        packet=json.loads(Path(event.value.path()).read_text());data=packet['nightly']
        assert data['protocol']=='nightly-live-1'
        assert data['totals']['registered']>=1
        assert data['policy']['automaticPromotion'] is False
        assert data['policy']['metricsScope']=='nightly_refresh_procedure'
        assert set(data['models'])=={'v1','v2','base','candidate'}
        assert len({m['n'] for m in data['models'].values()})==1
        assert sum(version['totals']['registered'] for version in data['versions'])==data['totals']['registered']
        assert len(page.locator('#qualityNightly table').first.locator('thead th').all_text_contents())==5
        if not data['totals']['settled']:
            assert 'Ждём завершения' in page.locator('#qualityNightly').inner_text()
            assert all(m['brier'] is None for m in data['models'].values())
        page.locator('#qualityNightly > details').first.locator('summary').click()
        with page.expect_download() as event:
            page.locator('#qualityNightly').get_by_role('button',name='Скачать исходный расчёт',exact=True).first.click()
        evidence=json.loads(Path(event.value.path()).read_text())
        assert evidence['protocol']=='nightly-live-1'
        assert (instant(evidence['kickoff'])-instant(evidence['savedAt'])).total_seconds()>=86400
        assert set(evidence['models'])=={'v1','v2','base','candidate'}
        candidate=evidence['models']['candidate'];artifact=evidence['candidateArtifact']
        assert hashlib.sha256(artifact['modelJson'].encode()).hexdigest()==candidate['modelSha256']==artifact['trainingReport']['modelSha256']
        assert json.loads(artifact['modelJson'])['model_version']==candidate['modelVersion']
        assert instant(candidate['trainingGeneratedAt'])<=instant(evidence['savedAt'])
        assert artifact['trainingReport']['promotedToDefault'] is False
        page.locator('#qualityNightly > details').first.locator('summary').click()
        page.locator('#qualityNightly').screenshot(path=str(OUTPUT/'candidate-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#qualityNightly').screenshot(path=str(OUTPUT/'candidate-mobile.png'))
        page.locator('#qualityNightly > details').last.locator('summary').click()
        assert page.locator('#qualityNightly > details').last.locator('table').count()==min(20,len(data['versions']))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        malicious=copy.deepcopy(packet)
        malicious['nightly']['fixtures'][0]['home']='<img src=x onerror="window.__candidateXss=1">'
        malicious['nightly']['fixtures'][0]['models']['candidate']['modelVersion']='<script>window.__candidateXss=1</script>'
        page.route('**/v1/quality',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(malicious)))
        page.click('#refreshQuality');page.wait_for_function("!document.getElementById('refreshQuality').disabled")
        assert page.locator('#qualityNightly img, #qualityNightly script').count()==0
        assert page.evaluate('window.__candidateXss || 0')==0
        assert not errors,errors
        assert requests and all(method=='GET' for method,_ in requests)
        assert all('/fd/' not in url and '/af/' not in url and '/v1/research/' not in url for _,url in requests)
        result={'registered':data['totals']['registered'],'settled':data['totals']['settled'],'versions':len(data['versions']),
            'downloadedFixtureId':evidence['fixtureId'],'candidateVersion':candidate['modelVersion'],'modelChecksumVerified':True,
            'mobileOverflow':False,'jsErrors':errors,'writes':0,'newProviderRequests':0,'newAiCalls':0}
        (OUTPUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print('CANDIDATE_PASS',json.dumps(result,ensure_ascii=False))
    finally:browser.close()
