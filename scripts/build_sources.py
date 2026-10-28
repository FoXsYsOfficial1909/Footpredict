#!/usr/bin/env python3
"""Compile a small, validated source inventory for the dependency-free Worker."""
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
source=json.loads((ROOT/'research/sources.json').read_text())
if source.get('schema_version')!=1 or not source.get('teams'):
    raise SystemExit('Unsupported source configuration')
for name,team in source['teams'].items():
    if not name or not re.fullmatch('[a-z0-9-]{2,80}',team.get('bbc_slug','')) or not team.get('csv_name'):
        raise SystemExit('Invalid source mapping')
(ROOT/'worker/research-sources.mjs').write_text('// Generated from research/sources.json.\nexport default '+json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(',',':'))+';\n')
print('Compiled source inventory:',len(source['teams']),'clubs')
