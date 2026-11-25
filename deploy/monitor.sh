#!/bin/bash
# Health check (cron, every 5 minutes). Sends a Telegram message to the admin when something
# breaks and again when it is fixed; silent while nothing changes.
# Checks: bot container running and polling (heartbeat < 5 min), database healthy,
# last successful data update < 36 h ago, disk < 90 %. A stopped bot is restarted automatically.
cd "$(dirname "$0")/.."
STATE=/var/lib/siat-monitor; mkdir -p "$STATE"
NOW=$(date +%s)

check() {  # check <name> <ok:0|1> <problem text>
  local name=$1 ok=$2 text=$3 f="$STATE/$1.bad"
  if [ "$ok" = 1 ]; then
    [ -f "$f" ] && { deploy/notify.sh "✅ Tuzaldi / исправлено: $name"; rm -f "$f"; }
  else
    [ -f "$f" ] || { deploy/notify.sh "⚠️ $text"; echo "$NOW" > "$f"; }
  fi
}

# 1. bot running (restart it if not) and alive
if [ -z "$(docker compose ps -q --status running bot 2>/dev/null)" ]; then
  docker compose up -d bot >/dev/null 2>&1; sleep 20
fi
if [ -n "$(docker compose ps -q --status running bot 2>/dev/null)" ]; then
  HB=$(docker compose exec -T bot cat /data/heartbeat 2>/dev/null || echo 0)
  AGE=$((NOW - ${HB:-0}))
  if [ "$AGE" -lt 300 ]; then check bot 1 ""
  else check bot 0 "Бот не отвечает уже $((AGE/60)) мин. Лог: cd /opt/siat_agent && sudo docker compose logs --tail 50 bot"; fi
else
  check bot 0 "Бот не запущен и не перезапускается. Лог: cd /opt/siat_agent && sudo docker compose logs --tail 50 bot"
fi

# 2. database
if docker compose exec -T db pg_isready -U siat >/dev/null 2>&1; then check db 1 ""
else check db 0 "База PostgreSQL не отвечает."; fi

# 3. nightly update
LAST=$(cat "$STATE/last_update_ok" 2>/dev/null || echo "$NOW")
[ -f "$STATE/last_update_ok" ] || echo "$NOW" > "$STATE/last_update_ok"
if [ $((NOW - LAST)) -lt $((36*3600)) ]; then check update 1 ""
else check update 0 "Данные SIAT не обновлялись больше 36 часов. Лог: /var/log/siat-update.log"; fi

# 4. disk
USED=$(df --output=pcent / | tail -1 | tr -dc 0-9)
if [ "${USED:-0}" -lt 90 ]; then check disk 1 ""
else check disk 0 "Диск сервера заполнен на ${USED}%."; fi
