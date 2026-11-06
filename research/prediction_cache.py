"""Content-addressed, verified caches for deterministic historical predictions."""
from __future__ import annotations

import json
from pathlib import Path
import re

from research.models import loss
from research.nightly import sha


class PredictionCache:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stats = {'hits': 0, 'misses': 0, 'corrupt': 0, 'fits': 0}

    def get(self, inputs, compute):
        key = sha(inputs)
        if not re.fullmatch('[0-9a-f]{64}', key):
            raise ValueError('Invalid prediction cache key')
        path = self.directory / key[:2] / (key + '.json')
        if path.exists():
            try:
                packet = json.loads(path.read_text())
                value = packet['probabilities']
                if packet['inputSha256'] != key or packet['valueSha256'] != sha(value):
                    raise ValueError('Cache checksum mismatch')
                loss(value, 0)
                self.stats['hits'] += 1
                return value
            except (OSError, ValueError, KeyError, TypeError):
                self.stats['corrupt'] += 1
        value = compute()
        loss(value, 0)
        path.parent.mkdir(parents=True, exist_ok=True)
        packet = {'inputSha256': key, 'valueSha256': sha(value), 'probabilities': value}
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(packet, allow_nan=False))
        temporary.replace(path)
        self.stats['misses'] += 1
        return value
