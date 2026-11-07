"""Russian source messages preserve originals; every remote response is mocked."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright

BASE = os.environ.get('FOOTPREDICT_TEST_URL', 'http://127.0.0.1:8000/').split('#')[0]
OUTPUT = Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT', tempfile.mkdtemp(prefix='footpredict-translation-')))
OUTPUT.mkdir(parents=True, exist_ok=True)
API = 'https://footpredict-api.wzjp5747md.workers.dev'
NOW = datetime.now(timezone.utc).replace(microsecond=0)
CHECKED = NOW.isoformat().replace('+00:00', 'Z')
KICKOFF = (NOW + timedelta(days=1)).isoformat().replace('+00:00', 'Z')
IDENT = 560900
FIXTURE = {'id': IDENT, 'home': 'Arsenal FC', 'away': 'Chelsea FC', 'kickoff': KICKOFF}
TEAMS = [{'id': ident, 'name': name, 'form': [], 'points': 0,
          'home': {'games': 0}, 'away': {'games': 0}, 'calendar': {'status': 'unavailable'}}
         for ident, name in [(1, 'Arsenal FC'), (2, 'Chelsea FC')]]
NEWS = [
    ('Test Injury', 'i', 'Thigh injury - Unknown return date', False),
    ('Test Suspension', 's', 'Suspended until 29 Nov', False),
    ('Test Negation', 'd', 'He is not ruled out and could return if the scan is clear.', False),
    ('Test Old Injury', 'i', 'Knee injury - Unknown return date', True),
    ('Test Unsafe', 'd', '<img src=x onerror="window.__translationXss=1">', False),
]
REPORTS = [{'player': player, 'team': 'Arsenal FC', 'providerStatus': state,
            'news': news, 'reason': news, 'publishedAt': '2026-01-01T12:00:00Z' if stale else CHECKED,
            'stale': stale} for player, state, news, stale in NEWS]
AVAILABILITY = {'status': 'available', 'source': 'Premier League FPL player availability',
                'url': 'https://fantasy.premierleague.com/api/bootstrap-static/', 'fetchedAt': CHECKED,
                'teams': [{'team': 'Arsenal FC', 'status': 'reports_found', 'reports': REPORTS},
                          {'team': 'Chelsea FC', 'status': 'no_supported_reports', 'reports': []}]}
CONTEXT = {'version': 'test', 'fixture': FIXTURE, 'collectedAt': CHECKED, 'teams': TEAMS,
           'availability': AVAILABILITY,
           'lineups': {'status': 'not_announced', 'source': {'provider': 'ESPN', 'url': 'https://www.espn.com/soccer/'},
                       'teams': [{'team': team['name'], 'status': 'not_announced', 'players': []} for team in TEAMS]}}
QUOTE = 'The player has not been ruled out, but a late fitness test is still needed.'
PACKET = {'version': 'test', 'fixture': FIXTURE, 'teams': TEAMS, 'collectedAt': CHECKED,
          'coverage': {}, 'playerAvailability': copy.deepcopy(AVAILABILITY),
          'ai': {'status': 'no_sources', 'facts': []},
          'availabilityReports': [{'subject': 'Test Negation', 'team': 'Arsenal FC', 'category': 'availability',
                                   'quote': QUOTE, 'source': 'Test club', 'sourceType': 'official_club',
                                   'url': 'https://www.arsenal.com/news/test', 'publishedAt': CHECKED}],
          'articles': [{'title': 'The manager discusses a late fitness test', 'summary': QUOTE,
                        'source': 'Test club', 'team': 'Arsenal FC', 'publishedAt': CHECKED,
                        'url': 'https://www.arsenal.com/news/test'}]}

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True, args=['--no-sandbox'])
    try:
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        errors = []
        requests = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: requests.append((request.method, request.url)))

        def remote(route):
            path = route.request.url.split('workers.dev', 1)[-1]
            if path == '/v1/player-context/' + str(IDENT):
                value = CONTEXT
            elif path == '/v1/research/' + str(IDENT) + '/stored':
                value = PACKET
            elif '/fd/competitions/PL/matches' in path:
                value = {'matches': []}
            else:
                raise AssertionError('Unexpected remote request: ' + route.request.url)
            route.fulfill(status=200, content_type='application/json', body=json.dumps(value))

        page.route(API + '/**', remote)
        page.route('https://raw.githubusercontent.com/**', lambda route: route.fulfill(status=404, body='unavailable'))
        page.goto(BASE, wait_until='networkidle')
        page.evaluate('''({api,fixture})=>window.dispatchEvent(new CustomEvent('footpredict:match-selected',{
            detail:{league:'PL',fixtureId:fixture.id,apiBase:api,homeName:fixture.home,awayName:fixture.away,kickoff:fixture.kickoff}}))''',
            {'api': API, 'fixture': FIXTURE})
        page.wait_for_function("document.getElementById('researchTeams').textContent.includes('Test Injury')")
        assert page.get_by_role('tab').count() == 3
        assert page.locator('#loadResearch').is_hidden()
        all_messages = page.locator('.player-availability > details').first
        all_messages.locator(':scope > summary').click()
        rows = all_messages.locator(':scope > ul > li')

        def report(player):
            return rows.filter(has=page.get_by_text(player, exact=True))

        injury = report('Test Injury')
        assert 'Травма бедра' in injury.inner_text()
        assert 'неизвест' in injury.inner_text().lower()
        assert NEWS[0][2] not in injury.inner_text()
        original = injury.locator('.source-original')
        assert original.locator('p').is_hidden()
        original.locator('summary').click()
        assert original.locator('p').inner_text() == NEWS[0][2]
        assert 'Дата сообщения:' in injury.inner_text()
        assert '29 ноября' in report('Test Suspension').inner_text()
        assert 'не подтверждает' not in report('Test Suspension').inner_text()
        negation = report('Test Negation')
        assert NEWS[2][2] in negation.inner_text()
        assert 'перевод пока недоступен' in negation.inner_text()
        assert negation.locator('.source-original').count() == 0
        assert 'Сообщение давно не обновлялось' in report('Test Old Injury').inner_text()
        assert '2026' in report('Test Old Injury').inner_text()
        assert page.locator('#researchTeams img').count() == 0
        assert page.evaluate('window.__translationXss || 0') == 0

        page.locator('#researchEvidence').evaluate('(element)=>element.open=true')
        assert QUOTE in page.locator('#researchAi').inner_text()
        assert 'перевод пока недоступен' in page.locator('#researchAi').inner_text()
        page.locator('#researchNewsDetails').evaluate('(element)=>element.open=true')
        assert PACKET['articles'][0]['title'] in page.locator('#researchNews').inner_text()
        assert QUOTE in page.locator('#researchNews').inner_text()
        sources = page.locator('#researchPlayerSources').inner_text()
        assert 'МСК' in sources and 'проверено' in sources and 'Fantasy Premier League' in sources
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.locator('#matchResearch').screenshot(path=str(OUTPUT / 'translation-mobile.png'))

        # Export is an unchanged source packet; translations only affect display.
        page.wait_for_function("!document.getElementById('exportResearch').disabled")
        page.evaluate("document.body.classList.add('owner-mode')")
        with page.expect_download() as event:
            page.click('#exportResearch')
        exported = json.loads(Path(event.value.path()).read_text())
        assert exported == PACKET
        assert exported['playerAvailability']['teams'][0]['reports'][0]['news'] == NEWS[0][2]
        assert all(method == 'GET' for method, _ in requests)
        assert all(url.startswith(BASE) or url.startswith(API + '/') or url.startswith('https://raw.githubusercontent.com/')
                   for _, url in requests)
        assert all('/v1/research/' not in url or url.endswith('/stored') for _, url in requests)
        assert not errors, errors
        result = {'russianAvailability': True, 'originalPreserved': True, 'unknownReturnPreserved': True,
                  'suspensionDateTranslated': True, 'unsupportedNegationUnchanged': True,
                  'sourceAndDatePreserved': True, 'exportUnchanged': True, 'mobileOverflow': False,
                  'jsErrors': errors, 'translationRequests': 0, 'newAiCalls': 0, 'writes': 0}
        (OUTPUT / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print('SOURCE_TRANSLATION_PASS', json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        browser.close()
