#!/bin/bash
# Refresh SIAT data on the server (only indicators changed on SIAT are downloaded), then
# re-apply the read-only role password. Cron runs it nightly; safe to run by hand.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
docker compose run --rm -T bot ./update_data.sh
docker compose exec -T db psql -q -U siat -d siat -c "ALTER ROLE siat_reader PASSWORD '$PG_READER_PASSWORD'" >/dev/null
docker compose restart bot >/dev/null   # the bot rebuilds its search index on start
echo "$(date '+%F %T') update ok"
