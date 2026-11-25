#!/bin/bash
# Refresh SIAT data on the server (only indicators changed on SIAT are downloaded), then
# re-apply the read-only role password. Cron runs it nightly; safe to run by hand.
# On failure (or if the quality check reports FAIL) the admin gets a Telegram message.
set -uo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
STATE=/var/lib/siat-monitor; mkdir -p "$STATE"
LOG=$(mktemp)
fail() { deploy/notify.sh "❌ Ночное обновление данных не прошло ($1). Последние строки:
$(tail -n 12 "$LOG")"; echo "$(date '+%F %T') update FAILED: $1"; rm -f "$LOG"; exit 1; }

docker compose run --rm -T bot ./update_data.sh >"$LOG" 2>&1 || fail "update_data.sh"
cat "$LOG"
docker compose exec -T db psql -q -U siat -d siat -c "ALTER ROLE siat_reader PASSWORD '$PG_READER_PASSWORD'" >/dev/null || fail "database"
docker compose restart bot >/dev/null || fail "bot restart"   # the bot rebuilds its search index on start
FAILS=$(grep -oE '^FAIL: [0-9]+' "$LOG" | tail -1 | tr -dc 0-9)
[ "${FAILS:-0}" -gt 0 ] && deploy/notify.sh "⚠️ Данные обновлены, но проверка качества нашла ошибок: $FAILS. Лог: /var/log/siat-update.log"
date +%s > "$STATE/last_update_ok"
echo "$(date '+%F %T') update ok"
rm -f "$LOG"
