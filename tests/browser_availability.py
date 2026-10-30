"""Check the current availability view using stored cards, never new collection.

FOOTPREDICT_RESEARCH_FIXTURE optionally supplies a preview before API deployment.
"""
import json
import os
from pathlib import Path
import re
import tempfile

from playwright.sync_api import sync_playwright

OUTPUT=Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT',tempfile.mkdtemp(prefix='footpredict-availability-')))
OUTPUT.mkdir(parents=True,exist_ok=True)
BASE=os.environ.get('FOOTPREDICT_TEST_URL','http://127.0.0.1:8000/')
FIXTURE=os.environ.get('FOOTPREDICT_RESEARCH_FIXTURE')
API='https://footpredict-api.wzjp5747md.workers.dev'

with sync_playwright() as playwright:
    options={'executable_path':'/usr/bin/chromium','headless':True,'args':['--no-sandbox']}
    if os.environ.get('HTTPS_PROXY'):
        options['proxy']={'server':os.environ['HTTPS_PROXY'],'bypass':'127.0.0.1,localhost'}
    browser=playwright.chromium.launch(**options)
    try:
        page=browser.new_page(viewport={'width':1280,'height':960})
        errors=[];requests=[];stored_reads=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.on('request',lambda request:requests.append((request.method,request.url)) if 'workers.dev' in request.url else None)

        def research(route):
            if FIXTURE:
                route.fulfill(status=200,content_type='application/json',body=Path(FIXTURE).read_text())
            else:
                # Keep the user-facing action/rendering intact, replace its
                # network operation with the explicitly stored-only endpoint.
                address=API+'/v1/research/560626/stored'
                stored_reads.append(address)
                route.fulfill(response=route.fetch(url=address))

        page.route('**/v1/research/560626',research)
        page.goto(BASE,wait_until='networkidle',timeout=60000)
        page.evaluate("""api=>window.dispatchEvent(new CustomEvent('footpredict:match-selected',{
            detail:{league:'PL',fixtureId:560626,apiBase:api}}))""",API)
        before_score=page.locator('#score').inner_text()
        page.click('#loadResearch')
        page.wait_for_function("!document.getElementById('loadResearch').disabled",timeout=40000)
        assert page.locator('#researchResult').is_visible(),page.locator('#researchStatus').inner_text()
        assert page.locator('#score').inner_text()==before_score
        with page.expect_download() as event:page.click('#exportResearch')
        packet=json.loads(Path(event.value.path()).read_text())
        assert packet['sourcePolicy']['version']=='availability-policy-2'
        assert packet['sourcePolicy']['archivedSnapshotUnchanged'] is True
        assert packet['teams'][0]['availability']['medicalStatus']=='unknown'
        assert all(not re.search(r'\b(fpl|fantasy)\b',fact['quote'],re.I) for fact in packet.get('availabilityReports',[])+packet['ai']['facts'])
        quotes=page.locator('#researchAi .research-quote').all_text_contents()
        assert all(not re.search(r'\b(fpl|fantasy)\b',quote,re.I) for quote in quotes)
        assert len(quotes)>0
        ai_labels=page.locator('#researchAi').get_by_text('Фрагмент выбран ИИ; цитата взята из источника.',exact=True).count()
        assert ai_labels==len(packet['ai']['facts'])
        for fact in packet['availabilityReports']:
            if 'will not' in fact['quote']:
                assert any(fact['quote'] in quote for quote in quotes)
        if packet['sourcePolicy']['aiFactsExcluded']:
            assert packet['ai']['status'] in ['partial','filtered']
            assert 'фэнтези-футболе' in page.locator('#researchAi').inner_text()
        assert any('FPL' in text for text in page.locator('#researchNews').all_text_contents())==any('FPL' in article['title'] for article in packet['articles'])
        page.locator('#researchAi').screenshot(path=str(OUTPUT/'availability-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#researchAi').screenshot(path=str(OUTPUT/'availability-mobile.png'))

        unavailable={**packet,'ai':{'status':'unavailable','facts':[]},'coverage':{**packet['coverage'],'ai':False}}
        page.route('**/v1/research/560626',lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(unavailable)))
        page.click('#loadResearch');page.wait_for_function("!document.getElementById('loadResearch').disabled")
        assert 'ИИ не вернул завершённый проверяемый разбор' in page.locator('#researchAi').inner_text()
        assert page.locator('#researchAi .research-quote').count()==len(packet['availabilityReports'])
        assert not errors,errors
        assert requests and all(method=='GET' for method,_ in requests)
        assert all('/fd/' not in url and '/af/' not in url for _,url in requests)
        result={'collectedAt':packet['collectedAt'],'sourcePolicy':packet['sourcePolicy'],'displayAiFacts':len(packet['ai']['facts']),
                'displayAvailabilityReports':len(packet['availabilityReports']),'storedOnlyRequests':stored_reads,
                'jsErrors':errors,'providerRequests':0,'newAiCalls':0,'writes':0}
        (OUTPUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('AVAILABILITY_PASS',json.dumps(result,ensure_ascii=False),flush=True)
    finally:
        browser.close()
