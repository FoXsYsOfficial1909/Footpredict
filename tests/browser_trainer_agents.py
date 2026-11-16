"""Read the public trainer journal lazily, without training or writing forecasts."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright

BASE=os.environ.get('FOOTPREDICT_TEST_URL','http://127.0.0.1:8000/').split('#')[0]
OUTPUT=Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT',tempfile.mkdtemp(prefix='footpredict-agents-browser-')))
OUTPUT.mkdir(parents=True,exist_ok=True)
FIXTURE=os.environ.get('FOOTPREDICT_TRAINER_FIXTURE')
REPORT='**/trainer-agent-reports/latest.json*'

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
        if FIXTURE:
            source=Path(FIXTURE)
            page.route(REPORT,lambda route:route.fulfill(status=200,content_type='application/json',body=source.read_text()))
            page.route('**/trainer-agent-reports/dashboards/*.json',lambda route:route.fulfill(status=200,
                content_type='application/json',body=(source.parent/'dashboards'/route.request.url.rsplit('/',1)[1]).read_text()))
        page.goto(BASE+'#qualityNightly',wait_until='networkidle',timeout=45000)
        assert requests==[],'The journal should load only when opened'
        with page.expect_response(lambda r:'/trainer-agent-reports/dashboards/' in r.url and r.status==200) as dashboard_event:
            with page.expect_response(lambda r:'trainer-agent-reports/latest.json' in r.url and r.status==200) as event:
                page.locator('#trainingStatus > summary').click()
        packet=event.value.json()
        page.wait_for_selector('#downloadTrainerAgents',timeout=35000)
        message=page.locator('#trainerAgentsBody').inner_text()
        assert 'Последняя проверка:' in message and 'Последнее обучение:' in message
        assert 'Основная модель автоматически не заменяется' in message
        assert len(requests)==1
        with page.expect_download() as event:page.click('#downloadTrainerAgents')
        view=json.loads(Path(event.value.path()).read_text())
        assert view==dashboard_event.value.json(),'Export must preserve the fetched probabilities exactly'
        assert len(view['rows'])==min(view['counts']['registered'],100)
        assert {m['n'] for m in view['metrics'].values()}=={view['counts']['settled']}
        if not view['counts']['settled']:
            assert 'Точность новой серии пока неизвестна' in page.locator('#trainerAgentsComparison').inner_text()
            assert set(page.locator('#trainerAgentsComparison > .table-scroll td').all_inner_texts())=={'—'}
        page.locator('#trainingStatus').screenshot(path=str(OUTPUT/'agents-desktop.png'))
        page.locator('#trainerAgentsFixtures > summary').click()
        assert page.locator('#trainerAgentsFixtures article').count()==len(view['rows'])
        for row in page.locator('#trainerAgentsFixtures table tbody tr').all():
            numbers=[float(v.replace('%','').replace(',','.')) for v in row.locator('td').all_inner_texts()]
            assert abs(sum(numbers)-100)<1e-10
        for width in (390,320):
            page.set_viewport_size({'width':width,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('#trainerAgentsFixtures .table-scroll').evaluate_all('(nodes)=>nodes.every(n=>n.scrollWidth<=n.clientWidth+1)')
        if view['rows']:
            page.locator('#trainerAgentsFixtures article').first.screenshot(path=str(OUTPUT/'agents-first-match-mobile.png'))
        page.locator('#trainerAgentsFixtures > summary').click()
        page.locator('#trainingStatus').screenshot(path=str(OUTPUT/'agents-mobile.png'))
        original=page.locator('#trainerAgentsComparison').inner_text()
        page.route(REPORT,lambda route:route.fulfill(status=503,body='Unavailable'))
        page.wait_for_function("!document.getElementById('refreshTraining').disabled")
        page.click('#refreshTraining')
        page.wait_for_function("document.getElementById('trainerAgentsViewStatus').textContent.includes('предыдущая успешная загрузка')")
        assert page.locator('#trainerAgentsComparison').inner_text()==original
        bad=copy.deepcopy(packet);bad['latestSuccessful']['prospectiveDashboardSha256']='0'*64
        page.route(REPORT,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(bad)))
        page.route('**/dashboards/'+('0'*64)+'.json',lambda route:route.fulfill(status=200,
            content_type='application/json',body=json.dumps(view)))
        page.wait_for_function("!document.getElementById('refreshTraining').disabled")
        with page.expect_response(lambda r:'/dashboards/'+('0'*64) in r.url):page.click('#refreshTraining')
        page.wait_for_function("document.getElementById('trainerAgentsViewStatus').textContent.includes('предыдущая успешная загрузка')")
        assert page.locator('#trainerAgentsComparison').inner_text()==original
        if view['rows']:
            injected=copy.deepcopy(view);injected['rows'][0]['home']='<img src=x onerror="window.__agentsXss=1">'
            raw=json.dumps(injected,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            digest=hashlib.sha256(raw.encode()).hexdigest();safe_packet=copy.deepcopy(packet)
            safe_packet['latestSuccessful']['prospectiveDashboardSha256']=digest
            page.route(REPORT,lambda route:route.fulfill(status=200,content_type='application/json',body=json.dumps(safe_packet)))
            page.route('**/dashboards/'+digest+'.json',lambda route:route.fulfill(status=200,content_type='application/json',body=raw))
            page.wait_for_function("!document.getElementById('refreshTraining').disabled")
            page.click('#refreshTraining')
            page.wait_for_function("document.querySelector('#trainerAgentsFixtures article h4').textContent.startsWith('<img')")
            assert page.locator('#trainerAgentsComparison img').count()==0
            assert page.evaluate('window.__agentsXss || 0')==0
        assert not writes,writes
        assert not errors,errors
        result={'lazyLoading':True,'noApiWrites':True,'mobileOverflow':False,'jsErrors':errors,'message':message,
            'fixtureMode':bool(FIXTURE),'displayed':len(view['rows']),'settled':view['counts']['settled'],
            'downloadPreservesProbabilities':True,'roundedSums100':True,'failureRetainsView':True,
            'changedChecksumRejected':True,'textInjectionSafe':True}
        (OUTPUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        print('TRAINER_AGENTS_PASS',json.dumps(result,ensure_ascii=False))
    finally:browser.close()
