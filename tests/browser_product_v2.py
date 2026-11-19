"""Browser checks against the live or staged provider and immutable ledger reads."""
import json
import os
from pathlib import Path
import tempfile

from playwright.sync_api import sync_playwright

URL = os.environ.get('FOOTPREDICT_TEST_URL', 'http://127.0.0.1:8000/').split('#')[0]
OUTPUT = Path(os.environ.get('FOOTPREDICT_TEST_OUTPUT') or tempfile.mkdtemp(prefix='footpredict-v2-'))
OUTPUT.mkdir(parents=True, exist_ok=True)

with sync_playwright() as playwright:
    options = {'headless': True, 'executable_path':'/usr/bin/chromium', 'args':['--no-sandbox']}
    if os.environ.get('HTTPS_PROXY'):
        options['proxy'] = {'server':os.environ['HTTPS_PROXY'], 'bypass':'127.0.0.1,localhost'}
    with_browser = playwright.chromium.launch(**options)
    try:
        page = with_browser.new_page(viewport={'width':1440,'height':1000})
        errors, requests = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda r: requests.append((r.method,r.url)) if 'workers.dev' in r.url else None)
        with page.expect_response(lambda r:r.url.endswith('/v1/quality') and r.ok) as event:
            page.goto(URL, wait_until='networkidle', timeout=60000)
        quality = event.value.json()
        page.wait_for_selector('.v2-match', timeout=35000)
        n = page.locator('.v2-match').count()
        card = page.locator('.v2-match').first
        assert page.locator('#v2MatchCount').inner_text().find('С прогнозом') >= 0
        forecast_labels = card.locator('.v2-prob-labels strong').all_text_contents()
        assert len(forecast_labels) == 3
        assert sum(round(float(label.rstrip('%').replace(',','.'))*10) for label in forecast_labels) == 1000
        page.screenshot(path=str(OUTPUT/'matches-desktop.png'), full_page=True)
        page.fill('#v2Search','footpredict-unknown-team')
        assert page.locator('.v2-match').count() == 0
        page.fill('#v2Search','')
        assert page.locator('.v2-match').count() == n
        card.locator('.favorite').click()
        page.locator('.v2-more-filters > summary').click()
        page.check('#v2Favorites'); assert page.locator('.v2-match').count() == 1
        page.uncheck('#v2Favorites')
        page.select_option('#v2Status','live')
        assert page.locator('.v2-match').count() == 0
        page.select_option('#v2Status','all')
        page.locator('.v2-more-filters > summary').click()
        for size in (390,320):
            page.set_viewport_size({'width':size,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(OUTPUT/'matches-mobile.png'))
        card.screenshot(path=str(OUTPUT/'card-compact.png'))
        start_url = page.url
        assert card.locator('.v2-score-summary strong').count() == 1
        assert card.locator('.v2-best-market').count() == 1
        assert page.locator('#v2MatchDetail').count() == 0
        card.get_by_role('button',name='Показать прогноз').click()
        detail = card.locator('#v2MatchDetail')
        assert detail.is_visible() and page.url == start_url
        assert card.locator('.v2-toggle').get_attribute('aria-expanded') == 'true'
        assert detail.locator('tbody tr').count() == 6
        for section, count in [('totals',8),('teamGoals',14),('handicap',8),('scores',7)]:
            detail.locator('#v2ForecastSection').select_option(section)
            assert detail.locator('tbody tr').count() == count,section
            assert detail.locator('td.v2-market-risk').count() == count
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),section
        detail.locator('#v2ForecastSection').select_option('compare')
        chances = [float(x.lstrip('<>').rstrip('%').replace(',','.')) for x in detail.locator('.v2-market-chance').all_text_contents()]
        assert len(chances) == 12 and chances == sorted(chances,reverse=True)
        detail.get_by_role('button',name='Показать ещё рынки').click()
        assert detail.locator('tbody tr').count() > 12
        detail.locator('.v2-market-filter select').select_option('high')
        assert set(detail.locator('.v2-market-risk').all_text_contents()) == {'Высокий'}
        detail.locator('#v2ForecastSection').select_option('history')
        assert detail.locator('.v2-team-grid .v2-recent').count() == 2
        assert detail.get_by_role('heading',name='Очные встречи').count() == 1
        assert not any('/player-context/' in url or '/v1/research/' in url for _,url in requests)
        detail.locator('#v2ForecastSection').select_option('totals')
        card.screenshot(path=str(OUTPUT/'forecast-mobile.png'))
        card.get_by_role('button',name='Свернуть прогноз').click()
        assert page.locator('#v2MatchDetail').count() == 0
        page.click('#tab-history')
        page.wait_for_selector('.v2-saved-item', timeout=35000)
        page.click('#v2SavedMore')
        assert page.locator('.v2-saved-item').count() == min(20,len(quality['nightly']['fixtures']))
        row = next(row for row in quality['nightly']['fixtures'][:20] if row['state']=='settled')
        item = page.locator(f'.v2-saved-item[data-fixture-id="{row["fixtureId"]}"]')
        assert 'Итог' in item.inner_text()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.click('#tab-quality')
        if quality['nightly'].get('windows'):
            page.wait_for_selector('#v2AnalyticsBody table', timeout=35000)
            for period in ('7','30','0'):
                page.select_option('#v2QualityPeriod',period)
                entry = quality['nightly']['windows']['groups']['all'][period]
                assert f'на {entry["paired"]} матчах' in page.locator('#v2AnalyticsBody').inner_text()
            page.select_option('#v2QualityLeague','PL')
            assert page.locator('#v2AnalyticsBody > .table-scroll tbody tr').count() == 3
            calibration = page.locator('#v2AnalyticsBody details').filter(has_text='Калибровка основной модели')
            calibration.locator('summary').click()
            assert calibration.locator('tbody tr').count() > 0
            calibration.locator('summary').click()
        assert sum(url.endswith('/v1/quality') for _,url in requests) == 1, requests
        assert all(method == 'GET' for method,_ in requests),requests
        goal_report=quality['nightly'].get('goalMarkets')
        if goal_report:
            goals=page.locator('#v2GoalQuality')
            goals.locator(':scope > summary').click()
            assert 'сверено '+str(goal_report['counts']['settled']) in goals.inner_text()
            assert goals.get_by_role('link',name='Сравнение на истории АПЛ').is_visible()
            assert goals.locator(':scope > div > h4').count() == (2 if goal_report['counts']['settled'] else 0)
            trial = goal_report.get('bothScoreTrial')
            if trial:
                panel = goals.locator('#v2BothScoreTrial')
                assert f"Записано {trial['counts']['registered']} из {trial['counts']['eligible']}" in panel.inner_text()
                assert panel.locator(':scope > .table-scroll tbody tr').count() == (3 if trial['counts']['settled'] else 0)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            goals.locator(':scope > summary').click()
        page.screenshot(path=str(OUTPUT/'analytics-mobile.png'), full_page=True)
        page.locator('#v2PublicTraining > summary').click()
        page.wait_for_selector('#v2PublicTraining a', timeout=25000)
        page.click('#v2Theme')
        assert page.evaluate("document.documentElement.dataset.theme") == 'light'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if quality['nightly'].get('windows'):
            assert page.locator('#v2AnalyticsBody > .table-scroll').evaluate('e=>e.scrollWidth<=e.clientWidth')
        page.screenshot(path=str(OUTPUT/'analytics-light-mobile.png'), full_page=True)
        page.click('#tab-history')
        item.get_by_role('link',name='Открыть матч').click()
        archived_card = page.locator(f'.v2-match[data-fixture-id="{row["fixtureId"]}"]')
        archived_card.locator('.v2-match-score').wait_for(timeout=35000)
        if row.get('homeId') is not None and row.get('awayId') is not None:
            archived_card.locator('.v2-saved').wait_for(timeout=25000)
            probabilities = row['models'][row.get('primaryModel','v1')]['probabilities']
            labels = archived_card.locator('.v2-prob-labels strong').all_text_contents()
            assert len(labels) == 3
            for i,label in enumerate(labels):
                v = float(label.rstrip('%').replace(',','.'))
                assert abs(v-probabilities[i]*100) < .101
            archived_card.locator('#v2ForecastSection').select_option('model')
            assert archived_card.locator('a[href$="/evidence"]').count() == 1
            assert archived_card.locator('.v2-best-market').count() == 0
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors,errors
        report={'calendar_cards':n,'saved_result':row['fixtureId'],'mobile_320':True,'read_only':True,'errors':errors,
            'backend_windows':bool(quality['nightly'].get('windows')),'saved_identity_verified':bool(row.get('homeId')),
            'inline_markets':True,'same_page':True}
        (OUTPUT/'summary.json').write_text(json.dumps(report,indent=2))
        print('PRODUCT_V2_PASS',json.dumps(report))
    finally:
        with_browser.close()
