#!/bin/bash
# Mac: dump the SIAT database into deploy/siat_dump.sql.gz, so the server can start without
# downloading from SIAT (useful if siat.stat.uz is slow or blocks the server's IP).
#   bash ~/siat_agent/deploy/export_from_mac.sh
set -euo pipefail
cd "$(dirname "$0")"
docker exec siat-pg pg_dump -U siat -d siat --schema=siat --no-owner --no-privileges | gzip > siat_dump.sql.gz
echo "Готово: $(pwd)/siat_dump.sql.gz ($(du -h siat_dump.sql.gz | cut -f1))"
