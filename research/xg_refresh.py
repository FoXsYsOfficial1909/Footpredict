"""Refresh Understat observations while retaining the last verified snapshot.

Fetch time is never reported as match-data freshness. Previously accepted rows
are immutable; provider corrections require review rather than silent changes.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request

from research.data import download_season
from research.performance import load_performance
from research.xg_data import merge_xg, validate_match
from research.xg_recent import (EXPECTED_TEAMS, REPOSITORY as RECENT_REPOSITORY,
                                key, load_recent, merge_sources)
from research.xg_summary import (EXPECTED_FILES, REPOSITORY as SUMMARY_REPOSITORY,
                                 parse_summary_csv)


class SourceUnavailable(RuntimeError):
    """Transport failure or the provider's maintenance response."""


def stamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Refresh time must include a timezone')
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def digest(body):
    return hashlib.sha256(body).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def atomic_write(path, body):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(body); temporary.replace(path)


def network_get(url, headers=None):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'understat.com', 'raw.githubusercontent.com'}:
        raise ValueError('Unregistered xG source URL')
    request = urllib.request.Request(url, headers={'User-Agent': 'FootpredictResearch/1.0', **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            body = response.read(2_000_001)
    except (OSError, TimeoutError) as error:
        raise SourceUnavailable(str(error)) from error
    if len(body) > 2_000_000:
        raise ValueError('Oversized xG source response')
    return body


def github_api(path):
    try:
        result = subprocess.run(['gh', 'api', path], check=True, capture_output=True, text=True, timeout=35)
    except (subprocess.SubprocessError, OSError) as error:
        raise SourceUnavailable('GitHub API request failed for ' + path.split('/')[1]) from error
    return json.loads(result.stdout)


def validate_reference(reference, matches):
    _, join = merge_xg(reference, matches)
    if join['unmatchedArchiveMatches']:
        raise ValueError('An xG source fixture lacks an exact reference date/team match')
    return join


def validate_reference_coverage(reference, season, prior_sources=(), *, closed_seasons=False):
    counts = {}
    for observation in reference:
        year = observation.match.season
        counts[year] = counts.get(year, 0) + 1
    if closed_seasons and any(counts.get(year) != 380 for year in range(2019, season)):
        raise ValueError('A closed reference EPL season is incomplete; expected 380 results')
    floor = max((int(row['rows']) for row in prior_sources if int(row['season']) == season), default=0)
    if counts.get(season, 0) < floor:
        raise ValueError('Current reference result count regressed below the last verified count')


def seed_reference_sources(seed, body):
    manifest_path = Path(seed).with_name('xg-manifest.json')
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('normalizedMatchesSha256') != digest(body):
        raise ValueError('Seed provenance does not identify the seed dataset')
    return manifest.get('referenceSources', [])


def unique_matches(matches):
    result = []; seen = set()
    for value in matches:
        row = validate_match(value)
        if key(row) in seen:
            raise ValueError('Duplicate normalized xG fixture')
        seen.add(key(row)); result.append(row)
    return sorted(result, key=key)


def parse_understat(body, season):
    if body.lstrip().lower().startswith((b'<!doctype html', b'<html')):
        raise SourceUnavailable('Understat returned its HTML maintenance page instead of league data')
    packet = json.loads(body)
    if not isinstance(packet, dict) or not isinstance(packet.get('dates'), list):
        raise ValueError('Understat league response is missing dates')
    result = []; ids = set()
    for raw in packet['dates']:
        if type(raw.get('isResult')) is not bool:
            raise ValueError('Invalid Understat result flag')
        match_id = raw['id']
        if str(match_id) in ids:
            raise ValueError('Duplicate Understat fixture ID')
        ids.add(str(match_id))
        if not raw['isResult']:
            continue
        result.append(validate_match({'date': raw['datetime'], 'home': raw['h']['title'],
                                      'away': raw['a']['title'], 'home_goals': raw['goals']['h'],
                                      'away_goals': raw['goals']['a'], 'home_xg': raw['xG']['h'],
                                      'away_xg': raw['xG']['a'], 'source_match_id': match_id,
                                      'season': season}))
    return unique_matches(result)


def github_source(repository, kind, *, fetch=network_get, api=github_api):
    """Resolve HEAD once, then verify raw bytes against that immutable tree."""
    if repository not in {SUMMARY_REPOSITORY, RECENT_REPOSITORY}:
        raise ValueError('Unregistered GitHub xG repository')
    commit = api(f'repos/{repository}/commits/HEAD')
    revision = commit.get('sha', '')
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('Invalid resolved source commit')
    tree = api(f'repos/{repository}/git/trees/{revision}?recursive=1')
    if tree.get('truncated') is not False:
        raise ValueError('Incomplete GitHub source tree')
    expected = ({'data/raw/understat/eng/' + name for name in EXPECTED_FILES} if kind == 'summary'
                else {'data/processed/' + name + '.csv' for name in EXPECTED_TEAMS})
    entries = {row['path']: row for row in tree['tree'] if row.get('type') == 'blob' and row['path'] in expected}
    if set(entries) != expected:
        raise ValueError('The registered source CSV set changed or is incomplete')
    def read(entry):
        url = f'https://raw.githubusercontent.com/{repository}/{revision}/' + urllib.parse.quote(entry['path'])
        body = fetch(url)
        blob = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
        if len(body) != entry['size'] or blob != entry['sha']:
            raise ValueError('Source CSV differs from resolved Git blob: ' + entry['path'])
        return body, {'path': entry['path'], 'url': url, 'gitBlobSha1': blob,
                      'sha256': digest(body), 'bytes': len(body)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        pairs = list(pool.map(read, [entries[name] for name in sorted(entries)]))
    if kind == 'summary':
        rows = []
        for body, entry in pairs:
            year = 2000 + int(Path(entry['path']).stem[4:6])
            parsed, _ = parse_summary_csv(body, year); rows.extend(parsed)
        rows = unique_matches(rows)
    else:
        with tempfile.TemporaryDirectory(prefix='footpredict-recent-refresh-') as directory:
            for body, entry in pairs:
                (Path(directory) / Path(entry['path']).name).write_bytes(body)
            rows, _ = load_recent(directory)
    return rows, {'provider': 'Understat league-summary cache', 'repository': repository,
                  'revision': revision, 'sourceCommittedAt': commit['commit']['committer']['date'],
                  'files': [entry for _, entry in pairs], 'lastDataDate': rows[-1]['date'] if rows else None}


def coverage(reference, matches, season):
    original = [r.match for r in reference if r.match.season == season]
    lookup = {key(row): row for row in matches}
    matched = []; missing = []
    for match in original:
        row = lookup.get(match.key)
        if row and (row['home_goals'], row['away_goals'], row['season']) == (match.home_goals, match.away_goals, match.season):
            matched.append(row)
        else:
            missing.append({'date': match.day.isoformat(), 'home': match.home, 'away': match.away})
    return {'season': season, 'completedMatches': len(original), 'matchedXg': len(matched),
            'missingXg': len(missing), 'latestResultDate': max((m.day.isoformat() for m in original), default=None),
            'latestXgDate': max((r['date'] for r in matched), default=None), 'missingFixtures': missing}


def record_failed_attempt(state_dir, failure, now):
    """Record reference-download/bootstrap failures without trusting bad state."""
    state = Path(state_dir); generated = stamp(now); previous = {}; success = {}; available = False
    if (state / 'report.json').exists():
        try:
            previous = json.loads((state / 'report.json').read_text())
        except (OSError, ValueError):
            pass
    try:
        success = json.loads((state / 'success.json').read_text())
        body = (state / 'xg-matches.json').read_bytes()
        available = success.get('schemaVersion') == 1 and digest(body) == success.get('normalizedMatchesSha256')
        if available:
            unique_matches(json.loads(body))
    except (OSError, ValueError, KeyError, TypeError):
        available = False
    latest = success.get('dataLastMatchDate') if available else None
    report = {'schemaVersion': 1, 'generatedAt': generated, 'status': 'failed', 'dataStatus': 'stale',
              'normalizedMatchesSha256': success.get('normalizedMatchesSha256') if available else None,
              'lastSuccessfulAcceptedAt': success.get('acceptedAt') if available else None,
              'lastSuccessfulAvailable': available, 'dataLastMatchDate': latest,
              'dataAgeDays': (date.fromisoformat(generated[:10]) - date.fromisoformat(latest)).days if latest else None,
              'retainedPreviousSnapshot': available, 'newMatchesAdded': 0,
              'coverage': previous.get('coverage'), 'referenceUnavailable': True, 'sources': [],
              'error': str(failure)[:600], 'promotedToDefault': False}
    atomic_write(state / 'attempt.json', encoded(report)); atomic_write(state / 'report.json', encoded(report))
    atomic_write(state / 'report.md', (f'xG refresh: **failed**. Last verified data: **{latest or "unavailable"}**. '
                                     'Current reference coverage could not be verified.\n').encode())
    return report


def refresh(state_dir, seed, reference, *, now=None, fetch=network_get, api=github_api,
            direct=True, reference_sources=None, max_data_age_days=14):
    """Return an attempt report; network/validation errors preserve accepted bytes.

    Seed imports are validated against reference scores. Once present, both the
    state file and its recorded hash must agree before a refresh can proceed.
    """
    generated = stamp(now or datetime.now(timezone.utc)); today = date.fromisoformat(generated[:10])
    season = today.year if today.month >= 7 else today.year - 1
    state = Path(state_dir); state.mkdir(parents=True, exist_ok=True)
    data_path, success_path = state / 'xg-matches.json', state / 'success.json'
    diagnostics = []; error = None; added = 0; changed = False
    if data_path.exists() != success_path.exists():
        raise ValueError('Incomplete last-success xG snapshot; refusing to overwrite it')
    if data_path.exists():
        previous_bytes = data_path.read_bytes(); success = json.loads(success_path.read_text())
        if success.get('schemaVersion') != 1 or success.get('normalizedMatchesSha256') != digest(previous_bytes):
            raise ValueError('Last-success xG snapshot checksum mismatch')
        previous = unique_matches(json.loads(previous_bytes))
    else:
        previous_bytes = Path(seed).read_bytes(); previous = unique_matches(json.loads(previous_bytes))
        seed_sources = seed_reference_sources(seed, previous_bytes)
        validate_reference_coverage(reference, season, seed_sources)
        validate_reference(reference, previous)
        success = {'schemaVersion': 1, 'status': 'success', 'normalizedMatchesSha256': digest(previous_bytes),
                   'acceptedAt': generated, 'dataLastMatchDate': previous[-1]['date'] if previous else None,
                   'matchCount': len(previous), 'sourceManifest': {'seedFile': str(Path(seed)),
                   'seedSha256': digest(previous_bytes), 'sources': [],
                   'referenceSources': reference_sources or seed_sources}}
        atomic_write(data_path, previous_bytes); atomic_write(success_path, encoded(success))
    accepted = previous; reference_verified = False
    try:
        validate_reference_coverage(reference, season, success.get('sourceManifest', {}).get('referenceSources', []))
        validate_reference(reference, previous)
        reference_verified = True
        sources = []
        for repository, kind in ((SUMMARY_REPOSITORY, 'summary'), (RECENT_REPOSITORY, 'recent')):
            try:
                rows, manifest = github_source(repository, kind, fetch=fetch, api=api)
            except SourceUnavailable as unavailable:
                diagnostics.append({'repository': repository, 'status': 'unavailable', 'error': str(unavailable)[:300]})
                continue
            validate_reference(reference, rows)
            diagnostics.append({**manifest, 'status': 'verified', 'matches': len(rows)})
            sources.append((rows, manifest))
        if direct:
            url = f'https://understat.com/getLeagueData/EPL/{season}'
            try:
                body = fetch(url, {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest',
                                   'Referer': f'https://understat.com/league/EPL/{season}'})
                rows = parse_understat(body, season)
            except SourceUnavailable as unavailable:
                diagnostics.append({'provider': 'Understat direct', 'url': url, 'status': 'unavailable', 'error': str(unavailable)[:300]})
            else:
                validate_reference(reference, rows)
                manifest = {'provider': 'Understat direct league summary', 'url': url,
                            'sha256': digest(body), 'bytes': len(body), 'lastDataDate': rows[-1]['date'] if rows else None}
                diagnostics.append({**manifest, 'status': 'verified', 'matches': len(rows)})
                sources.append((rows, manifest))
        if not sources:
            raise SourceUnavailable('No registered xG source could be verified')
        candidate = []; candidate_manifests = []
        for rows, manifest in sources:
            if not rows:
                raise ValueError('A registered xG source returned no completed observations')
            candidate, _ = merge_sources(candidate, rows)
            candidate_manifests.append(manifest)
        accepted, overlap = merge_sources(previous, candidate)
        if previous and not overlap['overlappingMatches']:
            raise ValueError('No overlap available to establish source consistency')
        validate_reference(reference, accepted)
        added = len(accepted) - len(previous); changed = added > 0
        if changed:
            raw = encoded(accepted)
            success = {'schemaVersion': 1, 'status': 'success', 'normalizedMatchesSha256': digest(raw),
                       'acceptedAt': generated, 'dataLastMatchDate': accepted[-1]['date'], 'matchCount': len(accepted),
                       'sourceManifest': {'previousSnapshotSha256': digest(previous_bytes),
                                          'previousAcceptedAt': json.loads(success_path.read_text())['acceptedAt'],
                                          'sources': candidate_manifests, 'overlap': overlap,
                                          'referenceSources': reference_sources or []}}
            atomic_write(data_path, raw); atomic_write(success_path, encoded(success))
    except (ValueError, KeyError, TypeError, SourceUnavailable) as failure:
        error = str(failure)[:600]; accepted = previous; added = 0; changed = False
    current_coverage = coverage(reference, accepted, season) if reference_verified else None
    latest = max((row['date'] for row in accepted), default=None)
    age = (today - date.fromisoformat(latest)).days if latest else None
    stale = bool(error) or current_coverage is None or current_coverage['missingXg'] > 0 or not current_coverage['matchedXg'] or age is None or age > max_data_age_days
    report = {'schemaVersion': 1, 'generatedAt': generated,
              'status': 'failed' if error else 'stale' if stale else 'success',
              'dataStatus': 'stale' if stale else 'current',
              'normalizedMatchesSha256': digest(data_path.read_bytes()),
              'lastSuccessfulAcceptedAt': success['acceptedAt'], 'lastSuccessfulAvailable': True,
              'dataLastMatchDate': latest, 'dataAgeDays': age,
              'retainedPreviousSnapshot': not changed, 'newMatchesAdded': added,
              'coverage': current_coverage, 'sources': diagnostics, 'referenceSources': reference_sources or [],
              'referenceUnavailable': not reference_verified, 'error': error, 'promotedToDefault': False}
    atomic_write(state / 'attempt.json', encoded(report)); atomic_write(state / 'report.json', encoded(report))
    coverage_text = (f"Current season: **{current_coverage['matchedXg']}/{current_coverage['completedMatches']}** matches with xG; "
                     f"**{current_coverage['missingXg']}** missing. " if current_coverage else 'Current reference coverage could not be verified. ')
    summary = (f"xG refresh: **{report['status']}**. Match data through **{latest or 'unknown'}** "
               f"({age if age is not None else 'unknown'} days old). "
               f"{coverage_text}Added {added}. Last verified snapshot retained: {not changed}.\n")
    if error:
        summary += '\nRefresh failed; previously verified data remain in use.\n'
    atomic_write(state / 'report.md', summary.encode())
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', required=True, type=Path)
    parser.add_argument('--seed', required=True, type=Path)
    parser.add_argument('--data-dir', required=True, type=Path)
    parser.add_argument('--now')
    parser.add_argument('--refresh-reference', action='store_true')
    args = parser.parse_args()
    now = stamp(args.now or datetime.now(timezone.utc)); year = int(now[:4]) if int(now[5:7]) >= 7 else int(now[:4]) - 1
    try:
        if args.refresh_reference:
            for season in range(2019, year + 1):
                download_season(season, args.data_dir, refresh=season == year)
        reference, manifests = load_performance(args.data_dir, range(2019, year + 1))
        reference = [row for row in reference if row.match.day.isoformat() <= now[:10]]
        validate_reference_coverage(reference, year, closed_seasons=True)
        report = refresh(args.state_dir, args.seed, reference, now=now, reference_sources=manifests)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as failure:
        report = record_failed_attempt(args.state_dir, failure, now)
    print(json.dumps({key: report[key] for key in ('status', 'dataLastMatchDate', 'dataAgeDays', 'newMatchesAdded', 'coverage', 'error')}))


if __name__ == '__main__':
    main()
