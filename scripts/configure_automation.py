#!/usr/bin/env python3
"""Add autonomous schedules without removing an existing trigger; verify readback."""
import argparse
import json
from pathlib import Path

from scripts.deploy_worker import Client, private_write

CRONS = ['17 */2 * * *', '7,37 * * * *', '50 4 * * *']


def cron_values(result):
    rows = result['schedules'] if isinstance(result, dict) else result
    return sorted({row['cron'] for row in rows})


def merged_schedules(current):
    return sorted(set(cron_values(current)) | set(CRONS))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    client = Client()
    before = client.schedules()
    desired = merged_schedules(before)
    private_write(args.backup, json.dumps(before, indent=2).encode())
    print(json.dumps({'mode': 'apply' if args.apply else 'dry_run', 'before': cron_values(before), 'desired': desired,
                      'backup': str(args.backup)}), flush=True)
    if not args.apply or cron_values(before) == desired:
        return
    if client.schedules() != before:
        raise SystemExit('Concurrent schedule change; no mutation sent')
    body = json.dumps([{'cron': cron} for cron in desired]).encode()
    result = json.loads(client.request('/schedules', method='PUT', body=body, content_type='application/json')[0])
    if not result.get('success'):
        raise SystemExit('Cloudflare did not confirm schedule update')
    after = client.schedules()
    private_write(args.backup.with_name(args.backup.stem + '-after.json'), json.dumps(after, indent=2).encode())
    if cron_values(after) != desired:
        raise SystemExit('Schedule readback differs; inspect backup before continuing')
    print('PASS: recurring collection and 07:50 Moscow report schedules verified; existing triggers retained.')


if __name__ == '__main__':
    main()
