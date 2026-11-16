"""Verified League One history for one fixed cross-division experiment."""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import math
from pathlib import Path
import urllib.request

from research.data import Match
from research.division_model import DivisionGame
from research.performance import Performance, parse_performance

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT/'docs/research/league-one-data-2026-11-15'
DATA = DIRECTORY/'training-data.json.gz'
PROTOCOL = 'league-one-source-audit-1'
SEASONS = tuple(range(2020, 2027))
CUTOFF = date(2026, 11, 14)
JOINT_SOURCE = ROOT/'docs/research/cross-division-2026-11-14/training-data.json.gz'
JOINT_SHA256 = '797641f4f0945de795ed202eb7107751172c6027a6cc3d993d4d8ded54cd40cd'
REGISTERED_DATA_SHA256 = 'fbf0464efca60b683faf73b834dd4633d5246ccbfdbbee335b7dab29d3b53290'
REGISTERED_COMPRESSED_SHA256 = '0cca3ea709651393f0448c68d198f52d489afd53d854e785e252cba8be9d2cba'


def raw_sha(body):
    return hashlib.sha256(body).hexdigest()


def canonical_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def normalized(games):
    return [[g.division, o.match.day.isoformat(), o.match.season, o.match.home, o.match.away,
             o.match.home_goals, o.match.away_goals, o.home_shots, o.away_shots,
             o.home_sot, o.away_sot, list(o.issues)]
            for g in sorted(games, key=lambda g: (*g.observation.match.key, g.division))
            for o in [g.observation]]


def validate_rows(rows, *, cutoff=CUTOFF, expected_counts=None):
    """Validate normalized scores/statistics without filling missing shots."""
    seen = set(); pairings = set(); games = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 12 or row[0] != 'E2':
            raise ValueError('Malformed League One normalized row')
        _, day, season, home, away, hg, ag, hs, aws, hst, ast, issues = row
        day = date.fromisoformat(day)
        key = (day.isoformat(), home, away)
        if (type(season) is not int or season not in SEASONS or key in seen
                or not isinstance(home, str) or not isinstance(away, str) or not home or not away or home == away
                or not date(season, 7, 1) <= day <= date(season+1, 7, 31) or day >= cutoff
                or any(type(v) is not int or not 0 <= v <= 30 for v in (hg, ag))):
            raise ValueError('Invalid League One fixture or information cutoff')
        pairing = (season, home, away)
        if pairing in pairings:
            raise ValueError('A League One home/away pairing repeats within one season')
        values = (hs, aws, hst, ast)
        if (any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 100) for v in values)
                or any(a is not None and b is not None and b > a for a, b in ((hs, hst), (aws, ast)))
                or not isinstance(issues, list) or len(issues) > 20
                or any(not isinstance(v, str) or len(v) > 100 for v in issues)):
            raise ValueError('Invalid League One shot statistic or provenance')
        seen.add(key); pairings.add(pairing)
        games.append(DivisionGame('E2', Performance(Match(day, season, home, away, hg, ag),
                                                   hs, aws, hst, ast, issues=tuple(issues))))
    if normalized(games) != rows:
        raise ValueError('League One snapshot is not in canonical fixture order')
    if expected_counts is not None:
        counts = {year: sum(g.observation.match.season == year for g in games) for year in SEASONS}
        if counts != {int(k): v for k, v in expected_counts.items()}:
            raise ValueError('The League One cohort size changed')
    return games


def parse_source(raw, season):
    if season not in SEASONS or len(raw) > 3_000_000:
        raise ValueError('Unexpected League One season or source size')
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    if not {'Div', 'Date', 'HomeTeam', 'AwayTeam', 'FTHG', 'FTAG', 'FTR', 'HS', 'AS', 'HST', 'AST'}.issubset(reader.fieldnames or []):
        raise ValueError('League One source lacks required identity or statistics columns')
    observations = parse_performance(raw, season, division='E2')
    if (season < 2026 and len(observations) != 552) or (season == 2026 and not 0 < len(observations) <= 552):
        raise ValueError('A completed League One season needs 552 regular-season results')
    clubs = {team for o in observations for team in (o.match.home, o.match.away)}
    pairings = {(o.match.home, o.match.away) for o in observations}
    if len(pairings) != len(observations) or len(clubs) > 24:
        raise ValueError('League One source repeats a seasonal pairing or has too many clubs')
    if season < 2026 and (len(clubs) != 24 or any(
            sum(o.match.home == team for o in observations) != 23
            or sum(o.match.away == team for o in observations) != 23 for team in clubs)):
        raise ValueError('Closed League One club coverage is not a complete home/away season')
    return observations, reader.fieldnames


def load_csv(directory, *, cutoff=CUTOFF):
    """Read the seven pinned source seasons; never contact providers here."""
    games, sources = [], []
    for season in SEASONS:
        raw = (Path(directory)/f'{season}-E2.csv').read_bytes()
        observations, columns = parse_source(raw, season)
        accepted = [o for o in observations if o.match.day < cutoff]
        games.extend(DivisionGame('E2', o) for o in accepted)
        sources.append({'division': 'E2', 'league': 'L1', 'season': season,
            'url': f'https://www.football-data.co.uk/mmz4281/{season%100:02d}{(season+1)%100:02d}/E2.csv',
            'sha256': raw_sha(raw), 'bytes': len(raw), 'sourceRows': len(observations), 'acceptedRows': len(accepted),
            'excludedAtOrAfterCutoff': len(observations)-len(accepted), 'columns': columns,
            'firstSourceMatch': min(o.match.day for o in observations).isoformat(),
            'lastSourceMatch': max(o.match.day for o in observations).isoformat(),
            'completeShotsOnTargetRows': sum(o.home_sot is not None and o.away_sot is not None for o in accepted),
            'goalFallbackRows': sum(o.home_sot is None or o.away_sot is None for o in accepted),
            'invalidStatisticsRows': sum(bool(o.issues) for o in accepted)})
    games.sort(key=lambda g: (*g.observation.match.key, g.division))
    validate_rows(normalized(games), cutoff=cutoff)
    return games, sources


def check_club_memberships(games, joint_path=JOINT_SOURCE):
    """Names are exact canonical source names; no fuzzy matching of clubs."""
    compressed = Path(joint_path).read_bytes()
    if raw_sha(compressed) != JOINT_SHA256:
        raise ValueError('The fixed E0/E1 membership source changed')
    other = json.loads(gzip.decompress(compressed)); memberships = {}
    for row in other + normalized(games):
        for team in row[3:5]:
            key = (row[2], team)
            if key in memberships and memberships[key] != row[0]:
                raise ValueError('A club appears in two divisions in the same season')
            memberships[key] = row[0]
    return {'checkedE0E1Matches': len(other), 'checkedE2Matches': len(games),
            'sameSeasonDivisionConflicts': 0, 'matching': 'exact canonical source club names',
            'sourceCompressedSha256': JOINT_SHA256}


def load_snapshot(path=DATA, *, expected_sha=REGISTERED_DATA_SHA256):
    """Return verified DivisionGame objects and the pinned source manifests."""
    path = Path(path); manifest = json.loads(path.with_name('input-manifest.json').read_text())
    compressed = path.read_bytes()
    if (len(compressed) > 3_000_000 or manifest.get('protocol') != PROTOCOL or manifest.get('division') != 'E2'
            or manifest.get('cutoffExclusive') != CUTOFF.isoformat()
            or raw_sha(compressed) != manifest.get('compressedSha256')):
        raise ValueError('League One snapshot scope or compressed checksum mismatch')
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
        raw = stream.read(5_000_001)
    if (len(raw) > 5_000_000 or raw_sha(raw) != manifest.get('uncompressedSha256')
            or manifest.get('dataSha256') != expected_sha):
        raise ValueError('League One snapshot data checksum mismatch')
    rows = json.loads(raw)
    if raw_sha(canonical_bytes(rows)) != expected_sha:
        raise ValueError('The pinned League One observations changed')
    games = validate_rows(rows, expected_counts=manifest['countsBySeason'])
    if len(games) != manifest.get('acceptedMatches'):
        raise ValueError('The League One cohort count changed')
    check_club_memberships(games)
    return games, manifest['sources']


def audit(directory, output=DIRECTORY):
    output = Path(output).resolve()
    protected = (ROOT/'docs/research').resolve()
    if (output == protected or protected in output.parents) and output != DIRECTORY.resolve():
        raise ValueError('Refusing to overwrite another registered research study')
    games, sources = load_csv(directory); rows = normalized(games)
    body = json.dumps(rows, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    compressed = gzip.compress(body, mtime=0)
    if raw_sha(body) != REGISTERED_DATA_SHA256 or raw_sha(compressed) != REGISTERED_COMPRESSED_SHA256:
        raise ValueError('Fresh sources differ from the registered League One dataset')
    memberships = check_club_memberships(games)
    histories = {}
    for team in ('Bolton', 'Lincoln'):
        recent = [g.observation.match for g in games if team in (g.observation.match.home, g.observation.match.away)
                  and 0 < (CUTOFF-g.observation.match.day).days <= 1095]
        histories[team] = {'E2GamesIn1095DayWindow': len(recent),
                           'latestMatch': max((m.day.isoformat() for m in recent), default=None)}
    manifest = {'schemaVersion': 1, 'protocol': PROTOCOL, 'division': 'E2', 'league': 'L1',
        'observedAt': datetime.now(timezone.utc).isoformat(), 'cutoffExclusive': CUTOFF.isoformat(),
        'seasons': list(SEASONS), 'sourceMatches': sum(s['sourceRows'] for s in sources), 'acceptedMatches': len(games),
        'countsBySeason': {str(y): sum(g.observation.match.season == y for g in games) for y in SEASONS},
        'completeShotsOnTargetMatches': sum(s['completeShotsOnTargetRows'] for s in sources),
        'goalFallbackMatches': sum(s['goalFallbackRows'] for s in sources),
        'firstMatch': rows[0][1], 'latestMatch': rows[-1][1],
        'dataSha256': raw_sha(canonical_bytes(rows)), 'uncompressedSha256': raw_sha(body),
        'compressedSha256': raw_sha(compressed), 'sources': sources, 'clubMembershipAudit': memberships,
        'sparseELCClubHistory': histories,
        'problematicStatistics': [{'date': o.match.day.isoformat(), 'home': o.match.home, 'away': o.match.away,
             'issues': list(o.issues), 'homeShots': o.home_shots, 'awayShots': o.away_shots,
             'homeShotsOnTarget': o.home_sot, 'awayShotsOnTarget': o.away_sot}
             for o in (g.observation for g in games) if o.issues],
        'limitations': [
            'Only league regular-season scores are included; cup games and play-offs are not invented or added.',
            'The first requested season is 2020/21 because the earliest 2023/24 evaluation uses a 1095-day window. Earlier E2 games cannot enter these fits.',
            'The abandoned 2019/20 season is outside the needed information window; no incomplete season is padded to 552 games.',
            'The current source ends on November 4, earlier than the pinned E0/E1 source dates. Its coverage is reported explicitly, not called real-time.',
            'One inconsistent shot observation retains its actual score and missing-statistic marker; it falls back to goals, never a fabricated shot value.',
            'Shots on target are an observed proxy, not measured xG. This audit does not compute forecast quality.']}
    output.mkdir(parents=True, exist_ok=True)
    (output/'training-data.json.gz').write_bytes(compressed)
    (output/'input-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    lines = ['# Проверка источника League One', '',
        f"Проверено {len(games)} матчей E2, из них {manifest['completeShotsOnTargetMatches']} с полными ударами в створ. "
        f"Последний результат источника: {manifest['latestMatch']}. Срез исключает игры с {CUTOFF.isoformat()}.", '',
        '| Сезон | Результатов в CSV | Принято | Полные удары в створ | Голевой резерв |',
        '| --- | ---: | ---: | ---: | ---: |']
    for s in sources:
        lines.append(f"| {s['season']}/{str(s['season']+1)[2:]} | {s['sourceRows']} | {s['acceptedRows']} | {s['completeShotsOnTargetRows']} | {s['goalFallbackRows']} |")
    lines += ['', 'Команды сопоставлены по точным каноническим именам источника; противоречий дивизионов одного клуба в одном сезоне нет.',
        f"У Bolton и Lincoln найдено по {histories['Bolton']['E2GamesIn1095DayWindow']} игр E2 в текущем трёхлетнем окне.", '',
        'Архив начинается с 2020/21: это начало необходимого окна для первого исторического прогноза 2023/24, '
        'а не исключение трудных матчей. Отменённый сезон 2019/20 не нужен этому окну и не дополняется вымышленными играми.', '',
        'Все шесть завершённых сезонов содержат по 552 регулярных результата. Текущий сезон содержит только наблюдаемые 158 игр. '
        'Кубки и плей-офф сюда не включаются. Текущий CSV заканчивается 4 ноября; это явно более раннее покрытие, чем у закреплённых E0/E1.', '',
        'Одна противоречивая запись ударов сохранена с исходным счётом и отсутствующими значениями статистики. '
        'Неполная статистика не превращается в нули; модель использует голевой резерв. Удары в створ не являются измеренным xG.', '',
        'URL, SHA-256 каждого CSV, полный список полей, контрольные суммы архива и проблемная запись доступны в `input-manifest.json`.', '',
        'Это аудит данных без расчёта качества прогнозов.']
    (output/'report.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps({k: manifest[k] for k in ('acceptedMatches','completeShotsOnTargetMatches','goalFallbackMatches','latestMatch','dataSha256','compressedSha256')}))
    return manifest


def download(directory):
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    for season in SEASONS:
        path = directory/f'{season}-E2.csv'
        if path.exists():continue
        url = f'https://www.football-data.co.uk/mmz4281/{season%100:02d}{(season+1)%100:02d}/E2.csv'
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent':'FootpredictResearch/1.0'}), timeout=30) as response:
            raw = response.read(3_000_001)
        parse_source(raw, season)
        path.write_bytes(raw)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=DIRECTORY)
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    if args.download:download(args.data_dir)
    audit(args.data_dir, args.output)
