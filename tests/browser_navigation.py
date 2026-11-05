"""Exercise tab navigation, state preservation and read-only lazy loading."""
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright

OUTPUT=Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT',tempfile.mkdtemp(prefix='footpredict-tabs-')))
OUTPUT.mkdir(parents=True,exist_ok=True)
BASE=os.environ.get('FOOTPREDICT_TEST_URL','http://127.0.0.1:8000/').split('#')[0]

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
        page.goto(BASE,wait_until='networkidle',timeout=45000)
        assert page.get_by_role('tab').count()==4
        assert page.locator('[data-app-panel]:visible').count()==1
        assert page.locator('#matches').is_visible()
        assert requests==[],requests
        page.locator('#advancedSettings > summary').click()
        page.fill('#homeName','Команда для проверки')
        page.locator('#matches').screenshot(path=str(OUTPUT/'matches-desktop.png'))
        page.click('#tab-history')
        page.wait_for_function("!document.getElementById('historyStatus').textContent.startsWith('Загружаю')",timeout=35000)
        assert page.locator('#history').is_visible() and page.locator('#matchPicker').is_hidden()
        page.select_option('#historyMode','local')
        page.click('#tab-quality')
        page.wait_for_function("!document.getElementById('qualityResult').hidden",timeout=35000)
        assert page.locator('#qualityDashboard').is_visible()
        assert page.locator('#qualityNightly').is_visible()
        page.go_back(wait_until='networkidle')
        assert page.locator('#history').is_visible()
        assert page.locator('#historyMode').input_value()=='local'
        page.go_forward(wait_until='networkidle')
        assert page.locator('#quality').is_visible()
        page.click('#tab-matches')
        assert page.locator('#homeName').input_value()=='Команда для проверки'
        assert page.locator('#advancedSettings').get_attribute('open') is not None
        page.focus('#tab-matches');page.keyboard.press('End')
        assert page.locator('#tab-data').get_attribute('aria-selected')=='true'
        assert page.locator('#data').is_visible()
        page.keyboard.press('Home')
        assert page.locator('#tab-matches').get_attribute('aria-selected')=='true'
        page.keyboard.press('ArrowLeft')
        assert page.locator('#tab-data').get_attribute('aria-selected')=='true'
        page.set_viewport_size({'width':390,'height':844})
        for key in ['matches','history','quality','data']:
            page.click('#tab-'+key)
            assert page.locator('[data-app-panel]:visible').count()==1
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),key
            page.screenshot(path=str(OUTPUT/(key+'-mobile.png')))
        for fragment,key in [('matchResearch','matches'),('predictionHistory','history'),('qualityDashboard','quality'),('serviceStatus','data'),('backtesting','quality')]:
            page.evaluate('fragment=>{location.hash=fragment}',fragment)
            page.wait_for_function("key=>document.getElementById('tab-'+key).getAttribute('aria-selected')==='true'",arg=key)
            assert page.locator('#'+fragment).is_visible(),fragment
        assert page.locator('.backtest-advanced').get_attribute('open') is not None
        assert requests and all(method=='GET' for method,_ in requests)
        assert all('/fd/' not in url and '/af/' not in url and '/v1/research/' not in url for _,url in requests)
        assert not errors,errors
        result={'tabs':4,'mobileOverflow':False,'preservedFormState':True,'keyboard':True,'legacyLinks':True,'readOnlyRequests':len(requests),'jsErrors':errors}
        (OUTPUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('NAVIGATION_PASS',json.dumps(result,ensure_ascii=False),flush=True)
    finally:
        browser.close()
