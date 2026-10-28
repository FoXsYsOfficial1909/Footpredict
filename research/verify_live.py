"""One auditable live check; never authenticates a prediction write or retries a provider."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.error
import urllib.request

from research.features import feature_row
from research.monitor import API, HEADERS, fetch
from scripts.overnight import atomic_json, locked_state


def unauthenticated_status(path, *, method='GET', origin=None):
    headers = {**HEADERS}
    if origin:
        headers['Origin'] = origin
    request = urllib.request.Request(API + path, method=method, data=b'{}' if method == 'POST' else None, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


def run(root, fixture_id, require_ai):
    root = Path(root)
    now = datetime.now(timezone.utc)
    checks = []
    def check(name, passed, evidence):
        checks.append({'name': name, 'status': 'passed' if passed else 'failed', 'evidence': evidence})
    try:
        body, _ = fetch(API + '/v1/health', 100000)
        health = json.loads(body)
        check('Live Worker configuration', health.get('configured') is True and health.get('research', {}).get('aiBinding') is True,
              json.dumps({'version': health.get('version'), 'research': health.get('research')}, ensure_ascii=False))
        for path in ('/v1/predictions', f'/v1/research/{fixture_id}/refresh'):
            code = unauthenticated_status(path, method='POST')
            check('Unauthenticated owner route ' + path, code == 401, 'HTTP ' + str(code) + '; no authorized write sent')
        code = unauthenticated_status('/v1/health', origin='https://untrusted.example')
        check('Live foreign-origin rejection', code == 403, 'HTTP ' + str(code))
        suffix = '' if require_ai else '/stored'
        body, _ = fetch(API + f'/v1/research/{fixture_id}' + suffix, 500000)
        packet = json.loads(body)
        row = feature_row(packet)
        check('Frozen prospective probabilities and point-in-time features', True,
              'fixture=' + str(row['fixture_id']) + '; snapshot=' + row['snapshot_sha256'] + '; collected=' + row['collected_at'])
        ai = packet.get('ai', {})
        if require_ai:
            check('Real Workers AI extraction after daily reset', ai.get('status') in ('complete', 'partial'),
                  json.dumps({'status': ai.get('status'), 'facts': len(ai.get('facts', [])), 'model': ai.get('model'),
                              'diagnostic': ai.get('diagnostic'), 'collectedAt': packet.get('collectedAt')}, ensure_ascii=False))
    except Exception as error:
        check('Live verification completed', False, type(error).__name__ + (' HTTP ' + str(error.code) if isinstance(error, urllib.error.HTTPError) else ''))
    directory = root / 'verification'
    directory.mkdir(exist_ok=True)
    path = directory / ('live-' + now.strftime('%Y%m%dT%H%M%SZ') + '.json')
    result = {'checked_at': now.isoformat(), 'requires_ai': require_ai, 'checks': checks,
              'passed': bool(checks) and all(item['status'] == 'passed' for item in checks)}
    atomic_json(path, result)
    with locked_state(root) as state:
        state['tests'].extend({'at': now.isoformat(), **item} for item in checks)
        state.setdefault('artifacts', []).append(str(path))
        state['live_verification'] = {'path': str(path), 'passed': result['passed'], 'checked_at': now.isoformat()}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if not result['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--fixture-id', type=int, default=560626)
    parser.add_argument('--require-ai', action='store_true')
    args = parser.parse_args()
    run(args.run_dir, args.fixture_id, args.require_ai)
