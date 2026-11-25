#!/bin/bash
# Send a message to the admin's Telegram (TELEGRAM_ADMIN_CHAT_ID in .env) through the bot.
#   deploy/notify.sh "text"
# Does nothing if TELEGRAM_ADMIN_CHAT_ID is not set.
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
[ -n "${TELEGRAM_ADMIN_CHAT_ID:-}" ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || exit 0
curl -s -m 20 -o /dev/null "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_ADMIN_CHAT_ID}" --data-urlencode "text=[SIAT server] $1" || true
