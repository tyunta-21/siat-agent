#!/bin/bash
# Refresh SIAT data: catalog -> download changed indicators -> normalize -> load -> quality checks.
# Indicators whose SIAT update date did not change are skipped, so a re-run takes a few minutes.
#   ./update_data.sh            all annual, quarterly and monthly indicators by region (about 2 700),
#                               then district values for up to DISTRICT_MINUTES minutes (default 120;
#                               the next run continues where this one stopped; 0 = skip districts)
#   ./update_data.sh --mvp      only the 10 MVP indicators
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python; [ -x "$PY" ] || PY=python3
$PY siat_catalog.py
if [ "${1:-}" = "--mvp" ]; then $PY siat_downloader.py
else
  $PY siat_downloader.py --all-regional --workers 4 --pause 0.3 || echo "Часть индикаторов не скачалась (см. data/raw/download_log.jsonl), продолжаю с остальными"
  if [ "${DISTRICT_MINUTES:-120}" != "0" ]; then
    $PY siat_downloader.py --districts --max-minutes "${DISTRICT_MINUTES:-120}" --workers 2 --pause 1 || echo "Районы скачались не полностью, продолжу в следующий раз"
  fi
fi
$PY siat_normalizer.py
$PY load_data.py
$PY check_data.py
