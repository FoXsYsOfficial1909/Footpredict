"""Re-score pinned historical predictions and inspect all three classes, without fitting."""
import argparse
from datetime import date, datetime, timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / 'docs/research/quality-audit-2026-11-15'
METHODS = ('v1', 'candidate', 'base', 'glm', 'elo')
CLASSES = ('P1', 'X', 'P2')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def checked_probabilities(values):
    if (not isinstance(values, list) or len(values) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in values)
            or abs(math.fsum(values)-1) > 1e-8):
        raise ValueError('Invalid 1X2 probabilities')
    return values


def validate_rows(rows):
    seen = set()
    for row in rows:
        day, cutoff = date.fromisoformat(row['date']), date.fromisoformat(row['cutoff_date'])
        key = (row['date'], row['home'], row['away'])
        if (key in seen or not row['home'] or not row['away'] or row['home'] == row['away']
                or cutoff != day-timedelta(days=1) or type(row['actual']) is not int or row['actual'] not in range(3)
                or not date(row['season'], 7, 1) <= day <= date(row['season']+1, 7, 31)):
            raise ValueError('Invalid identity, outcome or chronological information fence')
        seen.add(key)
        for method in METHODS:
            if row.get(method) is None:
                if method not in ('v1', 'candidate'):
                    raise ValueError('Missing expected historical probability')
            else:
                checked_probabilities(row[method])


def reliability(rows, method, bins=10):
    if not isinstance(bins, int) or bins <= 0:
        raise ValueError('The number of probability bins must be positive')
    result = {}
    for index, label in enumerate(CLASSES):
        cells = [[] for _ in range(bins)]
        for row in rows:
            p = checked_probabilities(row[method])[index]
            cells[min(bins-1, int(p*bins))].append((p, int(row['actual'] == index)))
        values = []
        for number, cell in enumerate(cells):
            n = len(cell)
            mean = math.fsum(p for p, _ in cell)/n if n else None
            frequency = sum(y for _, y in cell)/n if n else None
            interval = None
            if n:
                z = 1.959963984540054
                center = (frequency+z*z/(2*n))/(1+z*z/n)
                radius = z*math.sqrt(frequency*(1-frequency)/n+z*z/(4*n*n))/(1+z*z/n)
                interval = [max(0, center-radius), min(1, center+radius)]
            values.append({'lowerInclusive': number/bins, 'upper': (number+1)/bins,
                           'upperInclusive': number == bins-1, 'n': n,
                           'meanProbability': mean, 'observedFrequency': frequency,
                           'observedFrequencyWilson95': interval})
        n = len(rows)
        mean = math.fsum(r[method][index] for r in rows)/n if n else None
        frequency = sum(r['actual'] == index for r in rows)/n if n else None
        result[label] = {'n': n, 'meanProbability': mean, 'observedFrequency': frequency,
            'meanProbabilityMinusFrequency': mean-frequency if n else None,
            'binaryBrier': math.fsum((r[method][index]-(r['actual'] == index))**2 for r in rows)/n if n else None,
            'ece': math.fsum(v['n']*abs(v['meanProbability']-v['observedFrequency']) for v in values if v['n'])/n if n else None,
            'bins': values}
    return result


def metrics(rows, method):
    n, correct, unique, unique_correct = len(rows), 0, 0, 0
    brier, losses = [], []
    for row in rows:
        p = checked_probabilities(row[method]); target = row['actual']
        if type(target) is not int or target not in range(3):
            raise ValueError('Invalid outcome')
        largest = max(p); winners = [i for i, v in enumerate(p) if abs(v-largest) <= 1e-12]
        # Original NumPy argmax keeps the first exact maximum. Preserve this published denominator.
        correct += p.index(largest) == target
        if len(winners) == 1:
            unique += 1; unique_correct += winners[0] == target
        brier.append(math.fsum((v-(i == target))**2 for i, v in enumerate(p)))
        losses.append(-math.log(max(p[target], 1e-12)))
    return {'n': n, 'brier': math.fsum(brier)/n if n else None,
        'logLoss': math.fsum(losses)/n if n else None,
        'accuracy': correct/n if n else None, 'correct': correct,
        'topEligible': unique, 'topTies': n-unique,
        'uniqueTopAccuracy': unique_correct/unique if unique else None}


def evaluate(rows):
    validate_rows(rows)
    common = [r for r in rows if all(r.get(k) is not None for k in METHODS)]
    return {'completedMatches': len(rows), 'checkedCommonMatches': len(common),
        'excludedFromEveryModel': len(rows)-len(common),
        'coverage': len(common)/len(rows) if rows else None,
        'from': min((r['date'] for r in rows), default=None), 'to': max((r['date'] for r in rows), default=None),
        'excludedFixtures': [{'date': r['date'], 'home': r['home'], 'away': r['away'],
            'reason': 'insufficient_past_home_or_away_history_for_v1'} for r in rows if r['v1'] is None],
        'metrics': {method: metrics(common, method) for method in METHODS},
        'calibration': {method: reliability(common, method) for method in METHODS},
        'fullCoverageMetrics': {method: metrics([r for r in rows if r.get(method) is not None], method) for method in METHODS}}


def verify_references(rows, archive):
    """Rebuild the unchanged v1 and prior frequencies from past results, using no NumPy."""
    from scripts.verify_candidate import distribution
    games = [(date.fromisoformat(r[1]), *r[2:7]) for r in archive if r[0] == 'E0']
    by_key = {(day.isoformat(), home, away): (season, hg, ag) for day, season, home, away, hg, ag in games}
    if len(by_key) != len(games):
        raise ValueError('Duplicated historical result source')
    cache = {}
    for row in rows:
        identity = (row['date'], row['home'], row['away'])
        season, hg, ag = by_key[identity]
        if season != row['season'] or row['actual'] != (0 if hg > ag else 1 if hg == ag else 2):
            raise ValueError('The historical outcome disagrees with the result archive')
        cutoff = date.fromisoformat(row['cutoff_date']); key = (cutoff, season)
        if key not in cache:
            past = []
            for day, year, home, away, h, a in games:
                if day < cutoff and year in (season, season-1):
                    weight = math.exp(-math.log(2)*(cutoff-day).days/120)*(1 if year == season else .35)
                    past.append((home, away, h, a, weight))
            counts = [1., 1., 1.]
            for _, _, h, a, w in past:
                counts[0 if h > a else 1 if h == a else 2] += w
            mass = sum(w for _, _, _, _, w in past)
            lh = sum(h*w for _, _, h, _, w in past)/mass if mass else 0
            la = sum(a*w for _, _, _, a, w in past)/mass if mass else 0
            cache[key] = (past, [v/sum(counts) for v in counts], lh, la)
        past, base, lh, la = cache[key]
        if max(abs(a-b) for a, b in zip(base, row['base'])) > 1e-11:
            raise ValueError('Cannot reproduce the past-only frequency baseline')
        home = [r for r in past if r[0] == row['home']]
        away = [r for r in past if r[1] == row['away']]
        expected = None
        if len(past) >= 15 and len(home) >= 2 and len(away) >= 2 and min(lh, la) >= .25:
            def smooth(games, index, mean):
                return (sum(r[index]*r[4] for r in games)+5*mean)/(sum(r[4] for r in games)+5)
            home_goals = max(.05, min(5, smooth(home, 2, lh)*smooth(away, 2, lh)/lh))
            away_goals = max(.05, min(5, smooth(away, 3, la)*smooth(home, 3, la)/la))
            expected = distribution(home_goals, away_goals)
        if (expected is None) != (row['v1'] is None):
            raise ValueError('Cannot reproduce the original missing-history exclusions')
        if expected is not None and max(abs(a-b) for a, b in zip(expected, row['v1'])) > 1e-11:
            raise ValueError('Cannot reproduce the unchanged v1 probabilities')


def live_diagnostic(directory, manifest):
    metadata = manifest['liveSnapshot']; compressed = (Path(directory)/metadata['file']).read_bytes()
    if digest(compressed) != metadata['gzipSha256']:
        raise ValueError('Changed live quality snapshot')
    raw = gzip.decompress(compressed)
    if digest(raw) != metadata['rawSha256']:
        raise ValueError('Changed original live quality response')
    source = json.loads(raw)
    if source['fixtureScope']['total'] != len(source['fixtures']):
        raise ValueError('Live archive response is truncated')

    def actual(score):
        if any(type(score.get(k)) is not int or not 0 <= score[k] <= 30 for k in ('home', 'away')):
            raise ValueError('Invalid live score')
        return 0 if score['home'] > score['away'] else 1 if score['home'] == score['away'] else 2

    def compare(rows, method, expected):
        measured = metrics(rows, method)
        keys = {'n': 'n', 'brier': 'brier', 'logLoss': 'logLoss', 'accuracy': 'uniqueTopAccuracy', 'topEligible': 'topEligible'}
        for api_key, measured_key in keys.items():
            value, recorded = measured[measured_key], expected[api_key]
            if (value is None) != (recorded is None) or value is not None and abs(value-recorded) > 1e-12:
                raise ValueError('Cannot reproduce the published live quality metrics')
        return measured

    archives = {}
    for method in ('v1', 'v2'):
        rows = [{'actual': actual(f[method]['score']), method: f[method]['probabilities']}
                for f in source['fixtures'] if method in f and f[method]['state'] == 'settled']
        measured = compare(rows, method, source['cohorts'][method]['metrics'])
        archives[method] = {'registered': source['cohorts'][method]['total'], 'pending': source['cohorts'][method]['pending'],
            'modelVersion': source['cohorts'][method]['modelVersion'], 'leagues': source['cohorts'][method]['leagues'],
            'metrics': measured}
    shared = source['nightly']; fixtures = shared['fixtures']; methods = ('v1', 'v2', 'candidate', 'base')
    if shared['fixtureScope']['total'] != len(fixtures) or len({f['fixtureId'] for f in fixtures}) != len(fixtures):
        raise ValueError('Truncated or duplicated paired live fixtures')
    rows = []
    for fixture in fixtures:
        for method in methods:
            checked_probabilities(fixture['models'][method]['probabilities'])
        kickoff = datetime.fromisoformat(fixture['kickoff'].replace('Z', '+00:00'))
        saved = datetime.fromisoformat(fixture['savedAt'].replace('Z', '+00:00'))
        if kickoff-saved < timedelta(hours=24):
            raise ValueError('The candidate was not registered at least one day before kickoff')
        if fixture['state'] == 'settled':
            checked = datetime.fromisoformat(fixture['latest']['checkedAt'].replace('Z', '+00:00'))
            if fixture['latest']['status'] != 'settled' or checked-kickoff < timedelta(hours=4):
                raise ValueError('Unverified live result observation')
            rows.append({'actual': actual(fixture['latest']['score']), 'version': fixture['models']['candidate']['modelVersion'],
                         **{key: fixture['models'][key]['probabilities'] for key in methods}})
    measured = {method: compare(rows, method, shared['models'][method]) for method in methods}
    versions = []
    for version in shared['versions']:
        selected = [r for r in rows if r['version'] == version['modelVersion']]
        values = {method: compare(selected, method, version['models'][method]) for method in methods}
        versions.append({'modelVersion': version['modelVersion'], 'modelSha256': version['modelSha256'],
                         'totals': version['totals'], 'metrics': values})
    return {'observedAt': source['generatedAt'], 'legacyArchives': archives,
            'legacyArchivesComparable': False, 'legacyPairedMatches': source['paired']['common'],
            'nightlyComparison': {'totals': shared['totals'], 'metrics': measured, 'versions': versions,
                'liveMetricsIndependentlyReproduced': True},
            'note': 'Legacy v1 and v2 percentages use different fixtures and leagues. Nightly models are compared on shared fixtures; ten settled games are too few to establish improvement. Newer versions have no settled fixtures.'}


def load_inputs(directory=DIRECTORY):
    directory = Path(directory); manifest = json.loads((directory/'inputs.json').read_text())
    if manifest['protocol'] != 'quality-diagnostic-1' or manifest['independentTest'] is not False:
        raise ValueError('Unexpected diagnostic protocol')
    summary_path, selection_path = ROOT/'docs/research/summary.json', ROOT/'docs/research/selection.json'
    for path in (summary_path, selection_path):
        if digest(path.read_bytes()) != manifest[path.name]['sha256']:
            raise ValueError('The historical summary or settings changed')
    selection = json.loads(selection_path.read_text()); cohorts = {}
    archive_raw = (ROOT/manifest['matchSource']['path']).read_bytes()
    if digest(archive_raw) != manifest['matchSource']['gzipSha256']:
        raise ValueError('The historical result archive changed')
    archive = json.loads(gzip.decompress(archive_raw))
    for label, name in [('validation', 'validation-predictions.json'), ('held_out', 'held-out-predictions.json')]:
        compressed = (directory/(name+'.gz')).read_bytes()
        if digest(compressed) != manifest['rawFiles'][name]['gzipSha256']:
            raise ValueError('The saved prediction snapshot changed')
        raw = gzip.decompress(compressed)
        if digest(raw) != manifest['rawFiles'][name]['rawSha256']:
            raise ValueError('The original historical predictions changed')
        rows = json.loads(raw)
        if len(rows) != manifest['requiredMatches'][label]:
            raise ValueError('Changed historical cohort size')
        for row in rows:
            candidate = [selection['glm_weight']*a+(1-selection['glm_weight'])*b for a, b in zip(row['glm'], row['elo'])]
            if 'candidate' in row and max(abs(a-b) for a, b in zip(candidate, row['candidate'])) > 1e-12:
                raise ValueError('The candidate no longer uses its saved mixture')
            row['candidate'] = candidate
        validate_rows(rows)
        verify_references(rows, archive)
        cohorts[label] = rows
    return manifest, selection, json.loads(summary_path.read_text()), cohorts


def execute(output, directory=DIRECTORY):
    manifest, selection, summary, cohorts = load_inputs(directory)
    results = {key: evaluate(rows) for key, rows in cohorts.items()}
    for method, expected in summary['metrics_common'].items():
        actual = results['held_out']['metrics'][method]
        for key, value in expected.items():
            if abs(actual['logLoss' if key == 'logloss' else key]-value) > 1e-12:
                raise ValueError('Cannot reproduce the original held-out metrics')
    expected = selection['validation']; actual = results['validation']['metrics']['candidate']
    if any(abs(actual['logLoss' if k == 'logloss' else k]-v) > 1e-12 for k, v in expected.items()):
        raise ValueError('Cannot reproduce the original early candidate metrics')
    report = {'schemaVersion': 1, 'protocol': manifest['protocol'], 'inputManifestSha256': digest(json.dumps(manifest, sort_keys=True).encode()),
        'league': 'PL', 'mainMetric': 'logLoss', 'probabilityOrder': list(CLASSES),
        'brierFormula': manifest['brierFormula'], 'selection': selection,
        'history': {'parameterEstimationSeasons': summary['protocol']['development_seasons'],
                    'settingSelectionSeasons': summary['protocol']['validation_seasons'],
                    'originalLaterEvaluationSeasons': summary['protocol']['held_out_seasons']},
        'results': results, 'liveSnapshot': live_diagnostic(directory, manifest),
        'publishedMetricsReproduced': True, 'referenceProbabilitiesRebuilt': True,
        'outcomesMatchedToPinnedResultArchive': True, 'newTrainingPerformed': False,
        'independentTest': False, 'promotedToDefault': False, 'limitations': [
            'This audit recalculates proper scores and calibration from exact persisted predictions, not a new model fit.',
            'All historical periods were inspected before; this is not a new independent test or proof of future advantage.',
            'v1 and base are rebuilt from the pinned past-only match archive. The learned candidate uses its persisted historical predictions; its original fits are not rerun.',
            'Equal-width ten-bin calibration and its intervals are descriptive and unstable for small bins; no calibrator is fitted.',
            'Historical accuracy preserves first-argmax ties; uniqueTopAccuracy separately follows future-cohort tie handling.',
            'Comparison excludes the same missing-v1 fixtures from every method. Full coverage and excluded fixture identities remain visible.',
            'No injuries, lineups, weather, betting odds or target-match post-game statistics are added.']}
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    lines = ['# Проверка сохранённых прогнозов АПЛ', '',
        'Повторно рассчитаны метрики и калибровка точных исторических прогнозов. Нового обучения и подбора настроек нет. '
        'История уже изучалась; независимая будущая точность не подтверждена.', '',
        'Основная метрика — Log Loss. Brier = средняя сумма трёх квадратов ошибок, без деления на 3, шкала 0–2. '
        'Обе ошибки лучше при меньших значениях. Попадание — совпадение самого вероятного исхода с результатом.', '',
        '| Период | Завершено | Общих проверено | Исключено у всех | Модель | Попадания | Log Loss ↓ | Brier ↓ |',
        '| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |']
    for label, block in results.items():
        for method in ('v1', 'candidate', 'base', 'glm', 'elo'):
            m = block['metrics'][method]
            lines.append(f"| {label} | {block['completedMatches']} | {m['n']} | {block['excludedFromEveryModel']} | {method} | "
                         f"{m['accuracy']:.2%} | {m['logLoss']:.6f} | {m['brier']:.6f} |")
    lines += ['', 'В раннем периоде: сезоны 2023/24 и 2024/25. Поздний период: 2025/26. '
        'Параметры модели оценивались последовательно по прошлым играм; настройки выбирались в раннем периоде. '
        'У каждого прогноза указан срез за предыдущий день; результаты этого дня также исключались.', '',
        '## Калибровка позднего периода', '',
        'Средняя вероятность сравнивается с фактической частотой соответствующего исхода. '
        'ECE — средняя абсолютная разница внутри десяти фиксированных интервалов вероятностей; меньше лучше. '
        'Полные интервалы, размеры ячеек и 95% интервалы Уилсона для частот сохранены в JSON.', '',
        '| Модель | Исход | Средняя вероятность | Фактическая частота | ECE ↓ |',
        '| --- | --- | ---: | ---: | ---: |']
    for method in ('v1', 'candidate', 'base'):
        for label, values in results['held_out']['calibration'][method].items():
            lines.append(f"| {method} | {label} | {values['meanProbability']:.2%} | {values['observedFrequency']:.2%} | {values['ece']:.4f} |")
    live = report['liveSnapshot']; shared = live['nightlyComparison']
    lines += ['', '## Что означает процент на работающем сайте', '',
        f"Отдельный снимок публичного API: {live['observedAt']}. Все приведённые ниже метрики заново рассчитаны из "
        'сохранённых вероятностей и фактических счетов. Этот снимок не смешан с историческими сезонами.', '',
        f"Старый архив v1: {live['legacyArchives']['v1']['metrics']['correct']} попадания из "
        f"{live['legacyArchives']['v1']['metrics']['n']} ({live['legacyArchives']['v1']['metrics']['accuracy']:.1%}). "
        'Его процент не растёт от появления новых весов кандидата: он оценивает прежние сохранённые прогнозы v1. '
        'Архив v2 относится к другому набору игр и лиг; сравнивать эти два общих процента напрямую нельзя.', '',
        f"В совместной будущей серии ночного кандидата зарегистрировано {shared['totals']['registered']} игр: "
        f"{shared['totals']['settled']} завершённых и {shared['totals']['pending']} ожидающих.", '',
        '| Общие будущие игры | Завершено | Попадания | Log Loss ↓ | Brier ↓ |',
        '| --- | ---: | ---: | ---: | ---: |']
    for method in ('v1', 'candidate', 'base'):
        m = shared['metrics'][method]
        lines.append(f"| {method} | {m['n']} | {m['uniqueTopAccuracy']:.1%} | {m['logLoss']:.6f} | {m['brier']:.6f} |")
    lines += ['', 'Это лишь десять общих завершённых игр, а у простой базы та же доля попаданий, что у кандидата. '
        'Такой размер не устанавливает устойчивого преимущества. Все десять относятся к версии кандидата от 5 ноября; '
        'у зарегистрированных 14 ноября версий семь ожидающих игр и ни одного завершённого результата. '
        'Новые веса ещё не получили собственной будущей оценки. Разбор по точным версиям сохранён в JSON.']
    lines += ['', '## Вывод и воспроизведение', '',
        'Сохранённые метрики воспроизведены с точностью 1e-12. Все исходы сверены с закреплённым архивом результатов; '
        'вероятности v1 и базы заново рассчитаны из прошлых дат, команд и голов с допуском 1e-11. '
        'Параметры обученного кандидата заново не подгонялись: оценены его сохранённые исторические прогнозы. '
        'Кандидат снизил ошибки вероятностей на известном позднем периоде, '
        'но доля попаданий ниже v1. Это не основание обещать рост попаданий или автоматически менять публичную модель. '
        'Проверка на ещё не сыгранных матчах остаётся необходимой.', '',
        'Исторический код выбирает первый максимум при равенстве вероятностей; будущая серия не выбирает исход при равном максимуме. '
        'В отчёте сохранены обе оценки и число таких случаев. Это различие определения метрики, а не дополнительная точность модели.', '',
        'Пропуски v1 вызваны недостаточной прошлой домашней или выездной историей. '
        'Их список и оценка полного охвата других моделей доступны в JSON; исключения не маскируются фильтром уверенности.', '',
        'Точные исходные прогнозы сохранены в двух gzip-файлах рядом с отчётом; SHA-256 исходных и сжатых байтов, '
        'настроек и прежней сводки находятся в `inputs.json`. Команда использует только стандартную библиотеку и не обращается к сети:', '',
        '```bash', 'python -m research.quality_diagnostic --output /tmp/footpredict-quality-audit', '```', '']
    (output/'report.md').write_text('\n'.join(lines))
    print(json.dumps({'publishedMetricsReproduced': True,
        'matches': {k: {'common': v['checkedCommonMatches'], 'excluded': v['excludedFromEveryModel']} for k, v in results.items()},
        'independentTest': False, 'newTrainingPerformed': False}))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DIRECTORY)
    execute(parser.parse_args().output)
