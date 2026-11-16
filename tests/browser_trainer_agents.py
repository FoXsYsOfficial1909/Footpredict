"""Read the public trainer journal lazily, without training or writing forecasts."""
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright

BASE=os.environ.get('FOOTPREDICT_TEST_URL','http://127.0.0.1:8000/').split('#')[0]
OUTPUT=Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT',tempfile.mkdtemp(prefix='footpredict-agents-browser-')))
OUTPUT.mkdir(parents=True,exist_ok=True)

with sync_playwright() as playwright:
    options={'executable_path':'/usr/bin/chromium','headless':True,'args':['--no-sandbox']}
    if os.environ.get('HTTPS_PROXY'):
        options['proxy']={'server':os.environ['HTTPS_PROXY'],'bypass':'127.0.0.1,localhost'}
    browser=playwright.chromium.launch(**options)
    try:
        page=browser.new_page(viewport={'width':1280,'height':960})
        errors=[];requests=[];writes=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        def request_seen(request):
            if 'trainer-agent-reports/latest.json' in request.url:requests.append(request.url)
            if 'footpredict-api.' in request.url and request.method not in ('GET','OPTIONS'):writes.append(request.method)
        page.on('request',request_seen)
        page.goto(BASE+'#qualityNightly',wait_until='networkidle',timeout=45000)
        assert requests==[],'The journal should load only when opened'
        page.locator('#trainingStatus > summary').click()
        page.wait_for_function("document.getElementById('trainerAgentsBody').textContent.includes('Нейросеть: neural-agent-')",timeout=35000)
        message=page.locator('#trainerAgentsBody').inner_text()
        assert 'Последняя проверка:' in message and 'Последнее обучение:' in message
        assert 'Основная модель автоматически не заменяется' in message
        assert len(requests)==1
        page.locator('#trainingStatus').screenshot(path=str(OUTPUT/'agents-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#trainingStatus').screenshot(path=str(OUTPUT/'agents-mobile.png'))
        assert not writes,writes
        assert not errors,errors
        result={'lazyLoading':True,'noApiWrites':True,'mobileOverflow':False,'jsErrors':errors,'message':message}
        (OUTPUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('TRAINER_AGENTS_PASS',json.dumps(result,ensure_ascii=False))
    finally:browser.close()
