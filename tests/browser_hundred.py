"""The real frozen cohort with mocked result states; no forecasts/results are written."""
import copy
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import sys
from playwright.sync_api import sync_playwright

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from research.hundred import verify_files,REGISTERED_SHA,settle,instant
from scripts.publish_hundred import compact

BASE=os.environ.get('FOOTPREDICT_TEST_URL','http://127.0.0.1:8000/').split('#')[0]
OUT=Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT',tempfile.mkdtemp(prefix='footpredict-hundred-browser-')));OUT.mkdir(parents=True,exist_ok=True)
URL='https://raw.githubusercontent.com/FoXsYsOfficial1909/Footpredict/hundred-results/latest.json'
packet=verify_files()
empty=settle(packet,{code:{'competition':{'code':code},'filters':{'season':2026},'matches':[
    {'id':f['fixtureId'],'utcDate':f['kickoff'],'status':'TIMED','homeTeam':{'id':f['homeId'],'name':f['home']},
        'awayTeam':{'id':f['awayId'],'name':f['away']}} for f in packet['fixtures'] if f['league']==code]}
    for code in packet['leagues']},instant(packet['frozenAt']))
view={'schemaVersion':1,'protocol':packet['protocol'],'packetSha256':REGISTERED_SHA,'latestAttempt':compact(empty)}
with sync_playwright() as p:
    options={'executable_path':'/usr/bin/chromium','headless':True,'args':['--no-sandbox']}
    if os.environ.get('HTTPS_PROXY'):options['proxy']={'server':os.environ['HTTPS_PROXY'],'bypass':'127.0.0.1,localhost'}
    browser=p.chromium.launch(**options)
    try:
        page=browser.new_page(viewport={'width':390,'height':844});errors=[];requests=[]
        page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:requests.append((r.method,r.url)))
        page.route(URL,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(view)))
        page.goto(BASE+'#quality',wait_until='networkidle',timeout=60000)
        assert page.get_by_role('tab').count()==3
        assert all('hundred-cohort-data.js' not in url and 'hundred-results' not in url for _,url in requests)
        page.click('#tab-history')
        page.wait_for_function("document.querySelectorAll('#hundredRows article').length===10")
        assert 'проверено 0 / 100' in page.locator('#hundredSummary').inner_text()
        assert 'МСК' in page.locator('#hundredFrozen').inner_text()
        assert packet['fixtures'][0]['home'] in page.locator('#hundredRows article').first.inner_text()
        with page.expect_download() as event:page.click('#hundredCsv')
        text=Path(event.value.path()).read_text(encoding='utf-8-sig')
        rows=list(csv.reader(io.StringIO(text),delimiter=';'))
        assert len(rows)==101 and len({row[0] for row in rows[1:]})==100
        assert rows[1][0]==str(packet['fixtures'][0]['fixtureId'])
        assert abs(float(rows[1][5])/100-packet['fixtures'][0]['probabilities'][0])<1e-9
        page.click('#hundredMore');assert page.locator('#hundredRows article').count()==20
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#hundredSeries').screenshot(path=str(OUT/'hundred-mobile.png'))
        # Synthetic result ONLY in the intercepted browser response to verify display.
        changed=copy.deepcopy(view['latestAttempt']);changed['fixtures'][0].update(status='settled',outcome=2,score={'home':0,'away':1})
        view={'schemaVersion':1,'protocol':packet['protocol'],'packetSha256':REGISTERED_SHA,
            'latestAttempt':{'status':'failed'},'latestSuccessful':changed}
        page.reload(wait_until='networkidle');page.wait_for_function("document.getElementById('hundredChecked').textContent.includes('не удалась')")
        assert 'проверено 1 / 100' in page.locator('#hundredSummary').inner_text()
        assert 'Результат: 0 : 1' in page.locator('#hundredRows article').first.inner_text()
        assert not errors,errors
        assert all(method=='GET' for method,_ in requests)
        assert all('/v1/research/' not in url for _,url in requests)
        result={'forecastCount':100,'lazyLoad':True,'publicTabs':3,'csvRows':100,'pagination':True,
            'failedCheckPreservesPrevious':True,'mobileOverflow':False,'jsErrors':errors,'writes':0}
        (OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('HUNDRED_UI_PASS',json.dumps(result,ensure_ascii=False),flush=True)
    finally:browser.close()
