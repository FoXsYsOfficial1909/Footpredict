"""Exercise the simplified public flow and opt-in owner tools; GET only."""
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
    if os.environ.get('HTTPS_PROXY'):options['proxy']={'server':os.environ['HTTPS_PROXY'],'bypass':'127.0.0.1,localhost'}
    browser=playwright.chromium.launch(**options)
    try:
        page=browser.new_page(viewport={'width':390,'height':844});errors=[];requests=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:requests.append((r.method,r.url)) if 'workers.dev' in r.url else None)
        page.goto(BASE,wait_until='networkidle',timeout=60000)
        page.wait_for_function("!document.getElementById('loadMatches').disabled",timeout=35000)
        assert page.get_by_role('tab').count()==3
        assert page.locator('#calculator').is_hidden() and page.locator('#matchResearch').is_hidden()
        assert page.locator('#loadCompetitions').is_hidden() and page.locator('#content button').count()>0
        assert all('/competitions/PL/matches' in url for _,url in requests),requests
        page.screenshot(path=str(OUTPUT/'matches-mobile.png'),full_page=True)
        page.locator('#content button').first.click()
        page.wait_for_function("!document.getElementById('loadMatches').disabled",timeout=35000)
        assert page.locator('#results').is_visible() and page.locator('#matchResearch').is_visible()
        assert page.locator('#researchCoverage').is_hidden() and page.locator('#exportResearch').is_hidden()
        assert page.locator('#calculator').is_hidden()
        page.locator('#results').screenshot(path=str(OUTPUT/'forecast-mobile.png'))
        page.click('#tab-history')
        page.wait_for_function("!document.getElementById('historyStatus').textContent.startsWith('Загружаю')",timeout=35000)
        assert page.locator('#ownerPanel').is_hidden() and page.locator('#historyMode').is_hidden()
        assert page.locator('.record-check:visible').count()==0 and page.locator('.record-details:visible').count()==0
        page.click('#tab-quality')
        page.wait_for_function("document.querySelectorAll('#qualityOverview .overview-stat').length===3",timeout=35000)
        assert page.locator('#qualityNightly').is_hidden() and page.locator('#trainingStatus').is_hidden()
        assert page.locator('#qualityResult').is_hidden()
        page.screenshot(path=str(OUTPUT/'quality-mobile.png'),full_page=True)
        page.go_back(wait_until='networkidle');assert page.locator('#history').is_visible()
        page.go_forward(wait_until='networkidle');assert page.locator('#quality').is_visible()
        page.focus('#tab-matches');page.keyboard.press('End')
        assert page.locator('#tab-quality').get_attribute('aria-selected')=='true'
        page.keyboard.press('Home');assert page.locator('#tab-matches').get_attribute('aria-selected')=='true'
        for key in ('matches','history','quality'):
            page.click('#tab-'+key)
            assert page.locator('[data-app-panel]:visible').count()==1
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),key
        page.click('#ownerToolsLink')
        assert page.locator('#data').is_visible() and page.get_by_role('tab').count()==4
        page.click('#tab-matches');page.locator('#advancedSettings > summary').click()
        page.fill('#homeName','Служебная проверка')
        page.click('#tab-quality');assert page.locator('#qualityNightly').is_visible()
        page.click('#tab-data');page.click('#exitOwnerMode')
        page.wait_for_function("!document.getElementById('loadMatches').disabled",timeout=35000)
        assert page.get_by_role('tab').count()==3 and page.locator('#calculator').is_hidden()
        assert page.locator('#provider').input_value()=='fd' and page.locator('#competition').input_value()=='PL'
        assert requests and all(method=='GET' for method,_ in requests)
        assert all('/v1/research/' not in url for _,url in requests)
        assert not errors,errors
        result={'publicTabs':3,'automaticEpl':True,'forecastWorks':True,'technicalControlsHidden':True,
            'ownerToolsOptIn':True,'mobileOverflow':False,'keyboard':True,'readOnlyRequests':len(requests),'jsErrors':errors}
        (OUTPUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('NAVIGATION_PASS',json.dumps(result,ensure_ascii=False),flush=True)
    finally:browser.close()
