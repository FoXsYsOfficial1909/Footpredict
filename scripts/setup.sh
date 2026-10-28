#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
test -s index.html
command -v node >/dev/null
python3 -m venv --system-site-packages /workspace/.venvs/footpredict
/workspace/.venvs/footpredict/bin/python -m pip install --disable-pip-version-check --no-input -r requirements-research.txt -r requirements-browser.txt
python3 scripts/trust_browser_ca.py --optional
/workspace/.venvs/footpredict/bin/python -c 'import numpy, scipy, playwright.sync_api; print("Research and browser dependencies available.")'
node --version
echo 'Setup complete. Static app: python3 -m http.server 8000 --bind 127.0.0.1'
