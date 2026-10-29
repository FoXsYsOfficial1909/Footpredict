#!/usr/bin/env python3
"""Compile a small, validated source inventory for the dependency-free Worker."""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
source=json.loads((ROOT/'research/sources.json').read_text())
if source.get('schema_version')!=1 or not source.get('teams'):
    raise SystemExit('Unsupported source configuration')
for name,team in source['teams'].items():
    if not name or not re.fullmatch('[a-z0-9-]{2,80}',team.get('bbc_slug','')) or not team.get('csv_name'):
        raise SystemExit('Invalid source mapping')
    if team.get('official'):
        official=team['official'];url=urlsplit(official['index_url'])
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.port or not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}',url.hostname):
            raise SystemExit('Invalid official source URL')
        if not official.get('article_prefixes') or any(not prefix.startswith('/') or '?' in prefix for prefix in official['article_prefixes']):
            raise SystemExit('Invalid official article paths')
        if not isinstance(official.get('priority',10),int) or not 0<=official.get('priority',10)<=99:
            raise SystemExit('Invalid official source priority')
(ROOT/'worker/research-sources.mjs').write_text('// Generated from research/sources.json.\nexport default '+json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(',',':'))+';\n')
print('Compiled source inventory:',len(source['teams']),'clubs')
