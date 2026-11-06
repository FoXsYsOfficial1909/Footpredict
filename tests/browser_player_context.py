"""Automatic player facts, source uncertainty and selection races; all remote data mocked."""
import copy
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get('FOOTPREDICT_TEST_URL', 'http://127.0.0.1:8000/').split('#')[0]
OUTPUT = Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT', tempfile.mkdtemp(prefix='footpredict-player-context-')))
OUTPUT.mkdir(parents=True, exist_ok=True)
API = 'https://footpredict-api.wzjp5747md.workers.dev'
NOW = datetime.now(timezone.utc).replace(microsecond=0)
CHECKED = NOW.isoformat().replace('+00:00', 'Z')
KICKOFF = (NOW + timedelta(days=1)).isoformat().replace('+00:00', 'Z')


def team(name, ident):
    return {'id': ident, 'name': name, 'form': [], 'points': 0,
            'home': {'games': 0}, 'away': {'games': 0}, 'calendar': {'status': 'unavailable'}}


def context(ident, home='Arsenal FC', away='Chelsea FC'):
    return {'version': 'test', 'fixture': {'id': ident, 'home': home, 'away': away, 'kickoff': KICKOFF},
            'collectedAt': CHECKED, 'teams': [team(home, 1), team(away, 2)],
            'availability': {'status': 'available', 'source': 'Premier League FPL player availability',
                'url': 'https://fantasy.premierleague.com/api/bootstrap-static/', 'fetchedAt': CHECKED,
                'teams': [{'team': name, 'status': 'no_supported_reports', 'reports': []} for name in [home, away]]},
            'lineups': {'status': 'not_announced', 'source': {'provider': 'ESPN', 'url': 'https://www.espn.com/soccer/'},
                'fetchedAt': CHECKED, 'teams': [{'team': name, 'status': 'not_announced', 'players': []} for name in [home, away]]}}


payloads = {ident: context(ident) for ident in [560900, 560901, 560902, 560903]}
payloads[560900]['availability']['teams'][0].update(status='reports_found', reports=[
    {'player': 'Test Doubtful Player', 'providerStatus': 'd', 'status': 'doubtful',
     'news': 'Fitness check before the match.', 'publishedAt': CHECKED, 'stale': False},
    {'player': 'Test Old Player', 'providerStatus': 'i', 'status': 'injured',
     'news': '<img src=x onerror="window.__playerXss=1">', 'publishedAt': '2026-01-01T12:00:00Z', 'stale': True}])
payloads[560900]['availability']['teams'][0]['reports'].extend(
    {'player': 'Test Extra Player ' + str(i), 'providerStatus': 'd', 'status': 'doubtful',
     'news': 'Awaiting team update.', 'publishedAt': CHECKED, 'stale': False} for i in range(1, 4))
payloads[560901] = context(560901, 'Liverpool FC', 'Everton FC')
payloads[560902]['availability'].update(status='unavailable', reason='provider_unavailable')
for row in payloads[560902]['availability']['teams']:
    row['status'] = 'unknown'
payloads[560902]['lineups']['status'] = 'unavailable'
for row in payloads[560902]['lineups']['teams']:
    row['status'] = 'unavailable'
payloads[560903]['lineups']['teams'][0].update(status='reported_by_provider', formation='4-3-3',
    players=[{'id': str(i), 'name': 'Starter ' + str(i)} for i in range(1, 12)], officialConfirmation=False)
payloads[560903]['lineups']['status'] = 'reported_by_provider'

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
    try:
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        errors = []
        requests = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append((request.method, request.url)) if 'workers.dev' in request.url else None)

        def remote(route):
            path = route.request.url.split('workers.dev', 1)[-1]
            if '/v1/player-context/' in path:
                ident = int(path.rsplit('/', 1)[-1])
                route.fulfill(status=200, content_type='application/json', body=json.dumps(payloads[ident]))
            elif path.startswith('/v1/research/') and path.endswith('/stored'):
                route.fulfill(status=404, content_type='application/json', body='{"message":"No saved research"}')
            elif '/fd/competitions/PL/matches' in path:
                route.fulfill(status=200, content_type='application/json', body='{"matches":[]}')
            else:
                raise AssertionError('Unexpected remote request: ' + route.request.url)

        page.route('https://footpredict-api.wzjp5747md.workers.dev/**', remote)
        page.route('https://raw.githubusercontent.com/**', lambda route: route.fulfill(status=404, body='unavailable'))
        page.goto(BASE, wait_until='networkidle')
        assert page.get_by_role('tab').count() == 3

        def choose(ident):
            data = payloads[ident]
            page.evaluate('''({api,fixture})=>window.dispatchEvent(new CustomEvent('footpredict:match-selected',{
                detail:{league:'PL',fixtureId:fixture.id,apiBase:api,homeName:fixture.home,awayName:fixture.away,kickoff:fixture.kickoff}}))''',
                {'api': API, 'fixture': data['fixture']})

        choose(560900)
        page.wait_for_function("document.getElementById('researchTeams').textContent.includes('Test Doubtful Player')")
        assert page.locator('#researchResult').is_visible()
        assert page.locator('#loadResearch').is_hidden()
        assert page.locator('#exportResearch').is_hidden()
        assert page.locator('.research-team').count() == 2
        body = page.locator('#researchTeams').inner_text()
        assert 'Под вопросом' in body
        assert page.locator('.player-availability li:visible').count() == 3
        assert page.locator('.player-availability').get_by_text('Test Extra Player 3', exact=True).is_hidden()
        assert page.locator('.player-availability').get_by_text('Test Old Player', exact=True).is_hidden()
        sources=page.locator('#researchPlayerSources').inner_text()
        assert 'МСК' in sources and 'проверено' in sources and 'Fantasy Premier League' in sources
        page.locator('#matchResearch').screenshot(path=str(OUTPUT / 'player-context-mobile.png'))
        page.locator('.player-availability details summary').first.click()
        assert 'Дата сообщения:' in page.locator('#researchTeams').inner_text()
        assert 'Сообщение давно не обновлялось' in page.locator('#researchTeams').inner_text()
        assert page.locator('#researchTeams img').count() == 0
        assert page.evaluate('window.__playerXss || 0') == 0
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('.player-availability details summary').first.click()

        choose(560901)
        page.wait_for_function("document.getElementById('researchMatch').textContent.includes('Liverpool')")
        body = page.locator('#researchTeams').inner_text()
        assert 'Это не подтверждает отсутствие травм.' in body
        assert 'Test Doubtful Player' not in body

        choose(560902)
        page.wait_for_function("document.getElementById('researchTeams').textContent.includes('Свежие сведения об игроках не получены')")
        assert 'Стартовые 11 неизвестны' in page.locator('#researchTeams').inner_text()
        assert 'Все здоровы' not in page.locator('#researchTeams').inner_text()

        choose(560903)
        page.wait_for_function("document.getElementById('researchTeams').textContent.includes('Starter 11')")
        assert 'Стартовый состав по ESPN' in page.locator('#researchTeams').inner_text()
        assert 'отдельное подтверждение клуба не получено' in page.locator('#researchTeams').inner_text()

        # An uncooperative transport may resolve after abort; fixture generation still wins.
        late = copy.deepcopy(payloads[560900])
        late['availability']['teams'][0]['reports'][0]['player'] = 'LATE OLD FIXTURE PLAYER'
        page.evaluate('''({payload})=>{
          window.__baseFetch=window.fetch;
          window.fetch=(url,options)=>String(url).endsWith('/v1/player-context/560900')
            ?new Promise(resolve=>window.__releaseOld=()=>resolve(new Response(JSON.stringify(payload),{status:200})))
            :window.__baseFetch(url,options);
        }''', {'payload': late})
        choose(560900)
        page.wait_for_function("typeof window.__releaseOld==='function'")
        choose(560901)
        page.wait_for_function("document.getElementById('researchMatch').textContent.includes('Liverpool')")
        page.evaluate('window.__releaseOld()')
        page.wait_for_timeout(100)
        assert 'Liverpool' in page.locator('#researchMatch').inner_text()
        assert 'LATE OLD FIXTURE PLAYER' not in page.locator('#researchTeams').inner_text()

        assert requests and all(method == 'GET' for method, _ in requests)
        assert all('/v1/research/' not in url or url.endswith('/stored') for _, url in requests)

        # An explicit owner refresh must survive an older automatic stored read.
        fresh_packet = {'fixture': payloads[560900]['fixture'], 'teams': payloads[560900]['teams'],
            'collectedAt': CHECKED, 'coverage': {}, 'ai': {'status': 'no_sources', 'facts': []},
            'articles': [{'title': 'MANUAL LATEST EVIDENCE', 'url': 'https://www.bbc.com/sport/football/test',
                'source': 'Test source', 'team': 'Arsenal FC', 'publishedAt': CHECKED}]}
        old_packet = {**fresh_packet, 'articles': []}
        page.evaluate('''({fresh,old})=>{
          window.fetch=(url,options)=>{
            if(String(url).endsWith('/v1/research/560900/stored'))return new Promise(resolve=>
              window.__releaseStored=()=>resolve(new Response(JSON.stringify(old),{status:200})));
            if(String(url).endsWith('/v1/research/560900'))return Promise.resolve(new Response(JSON.stringify(fresh),{status:200}));
            return window.__baseFetch(url,options);
          };
          document.body.classList.add('owner-mode');
        }''', {'fresh': fresh_packet, 'old': old_packet})
        choose(560900)
        page.wait_for_function("typeof window.__releaseStored==='function'")
        page.click('#loadResearch')
        page.wait_for_function("document.getElementById('researchNews').textContent.includes('MANUAL LATEST EVIDENCE')")
        page.evaluate('window.__releaseStored()')
        page.wait_for_timeout(100)
        assert 'MANUAL LATEST EVIDENCE' in page.locator('#researchNews').text_content()
        page.evaluate("window.dispatchEvent(new CustomEvent('footpredict:selection-reset'))")
        assert page.locator('#matchResearch').is_hidden()
        assert not errors, errors
        result = {'automaticContext': True, 'storedOnlyResearch': True, 'emptyDoesNotMeanHealthy': True,
                  'providerLineupsLabeled': True, 'selectionRaceSafe': True, 'ownerRefreshRaceSafe': True, 'mobileOverflow': False,
                  'jsErrors': errors, 'requests': len(requests), 'writes': 0}
        (OUTPUT / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print('PLAYER_CONTEXT_PASS', json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        browser.close()
