"""Download and validate public match CSVs; exclude all post-match predictor columns."""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import io
import json
from pathlib import Path
import urllib.request


@dataclass(frozen=True)
class Match:
    day: date
    season: int
    home: str
    away: str
    home_goals: int
    away_goals: int

    @property
    def outcome(self):
        return 0 if self.home_goals > self.away_goals else 1 if self.home_goals == self.away_goals else 2

    @property
    def key(self):
        return (self.day.isoformat(), self.home, self.away)


ALIASES = {
    'Arsenal FC': 'Arsenal', 'Aston Villa FC': 'Aston Villa', 'AFC Bournemouth': 'Bournemouth',
    'Brentford FC': 'Brentford', 'Brighton & Hove Albion FC': 'Brighton', 'Chelsea FC': 'Chelsea',
    'Crystal Palace FC': 'Crystal Palace', 'Everton FC': 'Everton', 'Fulham FC': 'Fulham',
    'Liverpool FC': 'Liverpool', 'Manchester City FC': 'Man City', 'Manchester United FC': 'Man United',
    'Newcastle United FC': 'Newcastle', 'Nottingham Forest FC': "Nott'm Forest",
    'Tottenham Hotspur FC': 'Tottenham', 'West Ham United FC': 'West Ham',
    'Wolverhampton Wanderers FC': 'Wolves', 'Sunderland AFC': 'Sunderland', 'Leeds United FC': 'Leeds',
    'Burnley FC': 'Burnley', 'Coventry City FC': 'Coventry', 'Hull City AFC': 'Hull',
    'Leicester City FC': 'Leicester', 'Southampton FC': 'Southampton', 'Ipswich Town FC': 'Ipswich',
    'Luton Town FC': 'Luton', 'Sheffield United FC': 'Sheffield United', 'Watford FC': 'Watford',
    'Norwich City FC': 'Norwich', 'West Bromwich Albion FC': 'West Brom',
}


def canonical(name):
    return ALIASES.get(name.strip(), name.strip())


def parse_day(value):
    for pattern in ('%d/%m/%Y', '%d/%m/%y'):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            pass
    raise ValueError('Unrecognized match date: ' + value)


def parse_csv(body, season):
    rows = csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
    required = {'Date', 'HomeTeam', 'AwayTeam', 'FTHG', 'FTAG', 'FTR'}
    if not required.issubset(rows.fieldnames or []):
        raise ValueError('Match CSV is missing required columns')
    matches, seen = [], set()
    for row in rows:
        if not (row.get('Date') or '').strip():
            continue
        day = parse_day(row['Date'])
        if not date(season, 7, 1) <= day <= date(season + 1, 7, 31):
            raise ValueError('Match outside declared season')
        home, away = canonical(row['HomeTeam']), canonical(row['AwayTeam'])
        if not home or not away or home == away:
            raise ValueError('Invalid teams')
        hg, ag = int(row['FTHG']), int(row['FTAG'])
        if hg < 0 or ag < 0 or hg > 30 or ag > 30:
            raise ValueError('Invalid final score')
        expected = 'H' if hg > ag else 'D' if hg == ag else 'A'
        if row['FTR'].strip() != expected:
            raise ValueError('Score/result disagreement')
        match = Match(day, season, home, away, hg, ag)
        if match.key in seen:
            raise ValueError('Duplicate fixture')
        seen.add(match.key)
        matches.append(match)
    return sorted(matches, key=lambda m: m.key)


def download_season(season, directory, refresh=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    code = f'{season % 100:02d}{(season + 1) % 100:02d}'
    url = f'https://www.football-data.co.uk/mmz4281/{code}/E0.csv'
    path = directory / (str(season) + '-E0.csv')
    if path.exists() and not refresh:
        body = path.read_bytes()
    else:
        request = urllib.request.Request(url, headers={'User-Agent': 'FootpredictResearch/0.1'})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(3_000_001)
        if len(body) > 3_000_000:
            raise ValueError('CSV size limit exceeded')
        parse_csv(body, season)  # Do not cache a malformed or unexpected response.
        temporary = path.with_suffix('.csv.tmp')
        temporary.write_bytes(body)
        temporary.replace(path)
    matches = parse_csv(body, season)
    metadata = {'season': season, 'url': url, 'sha256': hashlib.sha256(body).hexdigest(),
                'rows': len(matches), 'first_date': matches[0].day.isoformat() if matches else None,
                'last_date': matches[-1].day.isoformat() if matches else None,
                'used_columns': ['Date', 'HomeTeam', 'AwayTeam', 'FTHG', 'FTAG', 'FTR'],
                'not_used': 'Odds, target-match shots, cards, corners and every other post-match field'}
    path.with_suffix('.manifest.json').write_text(json.dumps(metadata, indent=2))
    return matches, metadata


def load_seasons(seasons, directory):
    matches, manifests = [], []
    for season in seasons:
        rows, manifest = download_season(season, directory)
        matches.extend(rows)
        manifests.append(manifest)
    matches.sort(key=lambda m: m.key)
    if len({m.key for m in matches}) != len(matches):
        raise ValueError('Duplicate match across seasons')
    return matches, manifests
