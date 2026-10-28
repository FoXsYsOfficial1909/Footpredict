#!/usr/bin/env python3
"""Trust the managed environment's public CA in Chromium's NSS store.

This is optional during setup: a read-only browser profile must not prevent the
static app, offline tests or Python research tools from starting.
"""
import argparse
from pathlib import Path
import shutil
import subprocess


def install():
    certificate = Path('/usr/local/share/ca-certificates/environment-proxy-ca.crt')
    if not certificate.is_file() or not shutil.which('certutil'):
        raise RuntimeError('Managed public CA or certutil is unavailable')
    database = Path.home() / '.pki' / 'nssdb'
    database.mkdir(parents=True, exist_ok=True)
    if not (database / 'cert9.db').exists():
        subprocess.run(['certutil', '-N', '--empty-password', '-d', 'sql:' + str(database)], check=True, capture_output=True)
    subprocess.run(['certutil', '-A', '-d', 'sql:' + str(database), '-n', 'Codex-environment-proxy',
                    '-t', 'C,,', '-i', str(certificate)], check=True, capture_output=True)
    print('Browser CA trust configured; TLS verification remains enabled.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--optional', action='store_true')
    args = parser.parse_args()
    try:
        install()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print('Browser CA trust could not be configured (' + type(error).__name__ + '). Python and offline checks remain available.')
        if not args.optional:
            raise SystemExit(1)
