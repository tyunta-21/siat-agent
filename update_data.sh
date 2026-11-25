#!/bin/bash
# Refresh SIAT data: catalog -> download changed indicators -> normalize -> load -> quality checks.
# Indicators whose SIAT update date did not change are skipped, so a re-run takes a few minutes.
#   ./update_data.sh            all annual indicators by region (about 2 000)
#   ./update_data.sh --mvp      only the 10 MVP indicators
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
$PY siat_catalog.py
if [ "${1:-}" = "--mvp" ]; then $PY siat_downloader.py
else $PY siat_downloader.py --all-annual-regional --workers 4 --pause 0.3 || echo "Часть индикаторов не скачалась (см. data/raw/download_log.jsonl), продолжаю с остальными"; fi
$PY siat_normalizer.py
$PY load_data.py
$PY check_data.py
