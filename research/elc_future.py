"""Freeze and settle an ELC research comparison using only the standard library."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path

from research.hundred import (DIRECTORY as ORIGINAL, REGISTERED_SHA as ORIGINAL_SHA,
                              instant, score_rows, verify_files as verify_original)
from scripts.verify_candidate import ensemble, valid_probabilities

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / 'docs/research/elc-future-2026-11-14'
PLAN = ROOT / 'docs/research/elc-blend-plan-2026-11-14.json'
SELECTION = ROOT / 'docs/research/elc-blend-2026-11-14/selection.json'
TEAM_MAP = ROOT / 'research/elc-teams-2026.json'
PROTOCOL = 'elc-shots-future-1'
REGISTERED_SHA = '700408138bd35bfbec3ff1e807ae99020856f83b7f9c636a1a4c0bde6c346d6c'


def sha(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def team_mapping(original, mapping):
    if (mapping.get('league') != 'ELC' or mapping.get('division') != 'E1' or mapping.get('season') != 2026
            or len(mapping['teams']) != 24 or len({t['id'] for t in mapping['teams']}) != 24
            or len({t['csv'] for t in mapping['teams']}) != 24):
        raise ValueError('Invalid one-to-one ELC identity mapping')
    teams = {t['id']: t for t in mapping['teams']}
    for fixture in original['fixtures']:
        if fixture['league'] != 'ELC':continue
        for side in ('home', 'away'):
            if fixture[side+'Id'] not in teams or teams[fixture[side+'Id']]['name'] != fixture[side]:
                raise ValueError('A current ELC identity is missing or changed')
    rows = json.loads(gzip.decompress((ROOT/'docs/research/championship-2026-11-14/training-data.json.gz').read_bytes()))
    current = {(r[0], r[2], r[3]): r for r in rows if r[1] == 2026}
    fd = [g for g in json.loads((ORIGINAL/'inputs/ELC.json').read_bytes()) if g['__year'] == 2026]
    if len(current) != 180 or len(fd) != 180:
        raise ValueError('The registered current-season identity corpus changed')
    matched = set()
    for game in fd:
        h, a = teams[game['homeTeam']['id']]['csv'], teams[game['awayTeam']['id']]['csv']
        key = (instant(game['utcDate']).date().isoformat(), h, a)
        row = current.get(key); score = game['score']['fullTime']
        if row is None or row[4:6] != [score['home'], score['away']] or key in matched:
            raise ValueError('ELC mapping fails a historical fixture or score check')
        matched.add(key)
    if matched != set(current):raise ValueError('Some current E1 identities were not verified')
    return teams, {'season': 2026, 'verifiedHistoricalFixtures': len(matched), 'teamMappingSha256': sha(mapping)}


def shot_forecast(model, home, away):
    if (model.get('league') != 'ELC' or model.get('division') != 'E1'
            or model.get('method') != 'shots_on_target' or model.get('promoted_to_default') is not False
            or model.get('is_xg') is not False
            or any(t not in model['glm']['teams'] or t not in model['elo']['ratings'] for t in (home, away))):
        raise ValueError('Unverified shot model or unknown E1 team')
    return ensemble({**model, 'aliases': {}}, home, away)


def selection_inputs():
    plan = json.loads(PLAN.read_text()); selection = json.loads(SELECTION.read_text())
    source = ROOT / plan['sourceStudy']; report_bytes = (source/'report.json').read_bytes()
    report = json.loads(report_bytes); model = json.loads((source/'model.json').read_bytes())
    if (hashlib.sha256(report_bytes).hexdigest() != plan['sourceReportSha256']
            or selection['planSha256'] != sha(plan) or selection.get('selectedBeforeLaterMetrics') is not True
            or selection['shotWeight'] not in plan['shotWeights'] or selection.get('promotedToDefault') is not False
            or sha(model) != report['modelSha256']):
        raise ValueError('The registered selection, source study or exact weights changed')
    validation = json.loads((source/'validation-predictions.json').read_text())
    if (sha(validation) != plan['sourcePredictionSha256']['validation']
            or len(validation) != plan['expectedMatches']['validation']
            or [t['shotWeight'] for t in selection['trials']] != plan['shotWeights']):
        raise ValueError('The original validation predictions or bounded search changed')
    for trial in selection['trials']:
        weight = trial['shotWeight']
        probabilities = [[(1-weight)*a+weight*b for a, b in zip(row['v1'], row['shots'])] for row in validation]
        losses = [-math.log(max(p[row['actual']], 1e-12)) for row, p in zip(validation, probabilities)]
        brier = [sum((v-(i == row['actual']))**2 for i, v in enumerate(p)) for row, p in zip(validation, probabilities)]
        if (trial['metrics']['n'] != len(validation)
                or abs(math.fsum(losses)/len(losses)-trial['metrics']['logloss']) > 1e-12
                or abs(math.fsum(brier)/len(brier)-trial['metrics']['brier']) > 1e-12):
            raise ValueError('The selected mixture cannot be reproduced from earlier labels')
    expected = min(selection['trials'], key=lambda t: (t['metrics']['logloss'], t['shotWeight']))['shotWeight']
    if selection['shotWeight'] != expected:raise ValueError('The selected weight was changed after validation')
    return plan, selection, model


def build_packet(now, *, mapping=None):
    original = verify_original(); plan, selection, model = selection_inputs()
    now = instant(now) if isinstance(now, str) else now
    mapping = mapping or json.loads(TEAM_MAP.read_text()); teams, proof = team_mapping(original, mapping)
    cutoff = model['training_cutoff_exclusive']
    if (instant(model['as_of']+'T00:00:00Z') > now
            or model['glm']['as_of'] != cutoff or model['elo']['as_of'] != cutoff
            or max(model['glm']['fit']['latest_training_date'], model['elo']['latest_training_date']) >= cutoff
            or instant(cutoff+'T00:00:00Z') > now - timedelta(days=1)):
        raise ValueError('The shot model crosses its pre-registration training fence')
    fixtures, skipped = [], []
    weight = selection['shotWeight']
    for fixture in original['fixtures']:
        if fixture['league'] != 'ELC':continue
        if instant(fixture['kickoff']) - now < timedelta(hours=plan['minimumLeadHours']):
            skipped.append({'fixtureId': fixture['fixtureId'], 'reason': 'inside_24_hour_registration_deadline'})
            continue
        home, away = teams[fixture['homeId']]['csv'], teams[fixture['awayId']]['csv']
        shots = shot_forecast(model, home, away)
        candidate = [(1-weight)*a+weight*b for a, b in zip(fixture['probabilities'], shots)]
        fixtures.append({k: fixture[k] for k in ('fixtureId', 'league', 'season', 'homeId', 'awayId', 'home', 'away', 'kickoff')}
                        | {'homeCsv': home, 'awayCsv': away,
                           'v1': fixture['probabilities'], 'base': fixture['baseline'],
                           'shots': shots, 'candidate': candidate})
    if not fixtures:raise ValueError('No ELC fixtures remain outside the 24-hour deadline')
    return {'schemaVersion': 1, 'protocol': PROTOCOL, 'market': '1X2_REGULATION',
        'registeredAt': now.isoformat(), 'sourceCohortSha256': ORIGINAL_SHA,
        'originalRegisteredAt': original['frozenAt'], 'planSha256': sha(plan),
        'selectionSha256': sha(selection), 'selection': selection, 'shotWeight': weight,
        'modelSha256': sha(model), 'model': model, 'mapping': mapping, 'mappingProof': proof,
        'forecastCount': len(fixtures), 'fixtures': fixtures, 'skipped': skipped,
        'candidateEqualsRawShots': weight == 1, 'candidateEqualsV1': weight == 0,
        'historicalGatePassed': False, 'experimental': True, 'promotedToDefault': False,
        'originalHundredChanged': False, 'limitations': plan['limitations']}


def verify_files(directory=DIRECTORY, expected_sha=REGISTERED_SHA):
    directory = Path(directory); raw = (directory/'packet.json').read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    if (expected_sha is None or checksum != expected_sha
            or (directory/'packet.sha256').read_text().strip() != expected_sha):
        raise ValueError('The registered ELC packet changed')
    packet = json.loads(raw)
    rebuilt = build_packet(packet['registeredAt'], mapping=packet['mapping'])
    if len(packet['fixtures']) != len(rebuilt['fixtures']):raise ValueError('The ELC cohort cannot be reproduced')
    for actual, computed in zip(packet['fixtures'], rebuilt['fixtures']):
        for key in ('candidate', 'shots', 'v1', 'base'):
            if (not valid_probabilities(actual[key]) or not valid_probabilities(computed[key])
                    or max(abs(a-b) for a, b in zip(actual[key], computed[key])) > 1e-11):
                raise ValueError('The ELC probabilities cannot be reproduced')
            computed[key] = actual[key]  # Packet bytes remain exact; allow libm rounding in recomputation.
    if packet != rebuilt:raise ValueError('The ELC forecasts or original control cannot be reproduced')
    return packet


def freeze(directory=DIRECTORY):
    directory = Path(directory)
    if (directory/'packet.json').exists():raise ValueError('Refusing to overwrite a registered ELC cohort')
    packet = build_packet(datetime.now(timezone.utc))
    raw = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory/'packet.json').open('xb') as stream:stream.write(raw)
    checksum = hashlib.sha256(raw).hexdigest()
    with (directory/'packet.sha256').open('x') as stream:stream.write(checksum+'\n')
    with (directory/'forecasts.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream, delimiter=';', lineterminator='\n')
        writer.writerow(['fixtureId', 'kickoffUTC', 'home', 'away', 'v1P1', 'v1Draw', 'v1P2',
                         'candidateP1', 'candidateDraw', 'candidateP2'])
        for fixture in packet['fixtures']:
            writer.writerow([fixture['fixtureId'], fixture['kickoff'], fixture['home'], fixture['away'],
                             *fixture['v1'], *fixture['candidate']])
    print(json.dumps({'forecasts': len(packet['fixtures']), 'registeredAt': packet['registeredAt'],
                      'packetSha256': checksum, 'shotWeight': packet['shotWeight'], 'mappingProof': packet['mappingProof']}))
    return packet, checksum


def settle(packet, source, now, *, packet_sha=REGISTERED_SHA, protocol=PROTOCOL):
    now = instant(now) if isinstance(now, str) else now
    if packet.get('protocol') != protocol or packet.get('sourceCohortSha256') != ORIGINAL_SHA:
        raise ValueError('Different future comparison identity')
    checked = instant(source['checkedAt'])
    if (source.get('schemaVersion') != 1 or source.get('protocol') != 'hundred-future-1'
            or source.get('packetSha256') != ORIGINAL_SHA or source.get('forecastsChanged') is not False
            or source.get('status') not in ('active', 'complete') or checked > now + timedelta(minutes=5)):
        raise ValueError('Invalid, failed or future-dated hundred-results source')
    original = verify_original(); original_ids = {f['fixtureId'] for f in original['fixtures']}
    rows = source.get('fixtures', [])
    if len(rows) != 100 or len({r['fixtureId'] for r in rows}) != 100 or {r['fixtureId'] for r in rows} != original_ids:
        raise ValueError('The result source is not the complete original hundred')
    if source.get('finalized') is True and (source['status'] != 'complete'
            or any(r.get('status') in ('pending', 'unavailable') for r in rows)):
        raise ValueError('An unfinished source cannot be finalized')
    index = {r['fixtureId']: r for r in rows}; results = []
    for fixture in packet['fixtures']:
        observed = index[fixture['fixtureId']]; status = observed.get('status')
        outcome, score = observed.get('outcome'), observed.get('score')
        if status not in ('pending', 'settled', 'review', 'unavailable'):
            raise ValueError('Unknown source result status')
        if status == 'settled':
            if (not isinstance(score, dict) or any(type(score.get(k)) is not int or not 0 <= score[k] <= 30 for k in ('home', 'away'))
                    or type(outcome) is not int or outcome != (0 if score['home'] > score['away'] else 1 if score['home'] == score['away'] else 2)
                    or checked < instant(fixture['kickoff']) + timedelta(hours=4)):
                raise ValueError('Unverified score, outcome or four-hour information fence')
        elif outcome is not None or score is not None:
            raise ValueError('An unsettled source cannot contain a final outcome')
        results.append({**fixture, 'status': status, 'outcome': outcome, 'score': score, 'reason': observed.get('reason')})
    counts = {key: sum(r['status'] == key for r in results) for key in ('pending', 'settled', 'review', 'unavailable')}
    return {'schemaVersion': 1, 'protocol': protocol, 'packetSha256': packet_sha,
        'sourceCohortSha256': ORIGINAL_SHA, 'sourceResultsSha256': sha(source),
        'checkedAt': source['checkedAt'], 'status': 'complete' if not counts['pending'] and not counts['unavailable'] else 'active',
        'finalized': source.get('finalized', False), 'forecastCount': len(results), 'counts': counts,
        'metrics': {key: score_rows(results, key) for key in ('candidate', 'v1', 'base')},
        'fixtures': results, 'forecastsChanged': False, 'additionalProviderRequests': 0,
        'promotedToDefault': False, 'note': 'Small experimental future cohort; the candidate equals shots when weight is 1. No duplicated-model evidence.'}


def settle_files(source_path, output, *, previous_source=None, packet_loader=None, result_fn=None,
                 protocol=PROTOCOL, packet_sha=REGISTERED_SHA):
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    (output/'report.md').unlink(missing_ok=True)
    try:
        packet = (packet_loader or verify_files)()
        path = Path(source_path)
        if not path.exists() and previous_source:
            path = Path(previous_source)
            previous = json.loads(path.read_text())
            if previous.get('finalized') is not True:raise ValueError('Missing current result observation')
        source = json.loads(path.read_text())
        report = (result_fn or settle)(packet, source, datetime.now(timezone.utc))
        if report['protocol'] != protocol or report['packetSha256'] != packet_sha:
            raise ValueError('Result adapter returned another cohort identity')
        lines = ['# Будущая проверка Чемпионшипа', '', f"Дата настоящей сверки источника: {report['checkedAt']}.", '',
            '| Модель | Матчей | Brier ↓ | Log Loss ↓ | Совпал исход |', '| --- | ---: | ---: | ---: | ---: |']
        for name, metrics in report['metrics'].items():
            values = [f"{metrics[k]:.6f}" if metrics[k] is not None else '—' for k in ('brier', 'logLoss')]
            accuracy = f"{metrics['accuracy']:.1%}" if metrics['accuracy'] is not None else '—'
            lines.append(f"| {name} | {metrics['n']} | {values[0]} | {values[1]} | {accuracy} |")
        if 'applicableMatches' in report:
            lines += ['', f"Поправка применима к {report['applicableMatches']} играм и меняет {report['changedMatches']} прогнозов. "
                      f"Из применимых завершено: {report['applicableCounts']['settled']}. Остальные прогнозы повторяют v1.", '',
                      '| Только применимые игры | Матчей | Brier ↓ | Log Loss ↓ | Совпал исход |',
                      '| --- | ---: | ---: | ---: | ---: |']
            for name, metrics in report['applicableMetrics'].items():
                values = [f"{metrics[k]:.6f}" if metrics[k] is not None else '—' for k in ('brier', 'logLoss')]
                accuracy = f"{metrics['accuracy']:.1%}" if metrics['accuracy'] is not None else '—'
                lines.append(f"| {name} | {metrics['n']} | {values[0]} | {values[1]} | {accuracy} |")
        lines += ['', 'Вероятности зафиксированы до матчей. Это отдельная малая экспериментальная серия; '
            'основные прогнозы и исходная сотня не изменяются. Дополнительных запросов к поставщику: 0.']
        (output/'report.md').write_text('\n'.join(lines)+'\n')
    except Exception as error:
        report = {'schemaVersion': 1, 'protocol': protocol, 'packetSha256': packet_sha,
            'sourceCohortSha256': ORIGINAL_SHA, 'status': 'failed', 'checkedAt': datetime.now(timezone.utc).isoformat(),
            'forecastsChanged': False, 'errorType': type(error).__name__,
            'message': 'The future comparison could not verify its result source.'}
        raise
    finally:
        (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False))
    print(json.dumps({'counts': report['counts'], 'metrics': report['metrics'], 'additionalProviderRequests': 0}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('freeze', 'verify', 'settle'))
    parser.add_argument('--source', type=Path); parser.add_argument('--previous-source', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'freeze':freeze()
    elif args.command == 'verify':
        packet = verify_files(); print(json.dumps({'verified': True, 'forecasts': packet['forecastCount'], 'packetSha256': REGISTERED_SHA}))
    else:
        if not args.source or not args.output:parser.error('settle requires --source and --output')
        settle_files(args.source, args.output, previous_source=args.previous_source)
