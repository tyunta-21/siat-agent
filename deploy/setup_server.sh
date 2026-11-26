#!/bin/bash
# SIAT agent: one-shot setup on a fresh Ubuntu server (AWS Lightsail / EC2 / any VPS).
#   sudo bash setup_server.sh
# Installs Docker, asks for the OpenAI and Telegram keys, starts PostgreSQL, downloads SIAT data
# (first time 15-25 min), starts the bot and adds a nightly data update at 04:00 Tashkent time.
# Safe to run again: keeps .env and the database, rebuilds the bot with the current code.
set -euo pipefail
CODE_ONLY=0; [ "${1:-}" = "--code" ] && CODE_ONLY=1   # new code only: no data download
DIR=/opt/siat_agent
SRC="$(cd "$(dirname "$0")/.." && pwd)"
say() { printf "\n\033[1m%s\033[0m\n" "$1"; }
[ "$(id -u)" = 0 ] || { echo "Запусти через sudo: sudo bash $0"; exit 1; }

say "1/6 Docker"
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq ca-certificates curl >/dev/null
  curl -fsSL https://get.docker.com | sh >/dev/null
fi
systemctl enable --now docker >/dev/null 2>&1 || true
# small servers (1 GB): add 2 GB swap so pip / the first load never run out of memory
if [ ! -f /swapfile ] && [ "$(free -m | awk '/Mem:/{print $2}')" -lt 1900 ]; then
  if fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile; then
    grep -q /swapfile /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
  else echo "Swap не включился, продолжаю без него"; rm -f /swapfile; fi
fi
docker compose version

say "2/6 Код -> $DIR"
mkdir -p "$DIR"
if [ "$SRC" != "$DIR" ]; then
  tar -C "$SRC" --exclude=.env --exclude=.venv --exclude=data --exclude=__pycache__ -cf - . | tar -C "$DIR" -xf -
fi
cd "$DIR"
chmod +x update_data.sh deploy/*.sh

say "3/6 Ключи (.env)"
if [ ! -f .env ]; then
  read -r -s -p "Ключ OpenAI 'siat' (sk-...): " RAW; echo
  KEY=$(printf '%s' "$RAW" | grep -oE 'sk-[A-Za-z0-9_-]{20,}' | head -1 || true)
  [ -n "$KEY" ] || { echo "Не вижу ключа sk-... Запусти скрипт снова."; exit 1; }
  read -r -s -p "Токен бота от @BotFather (123456:AA...): " RAW; echo
  TOK=$(printf '%s' "$RAW" | grep -oE '[0-9]{6,}:[A-Za-z0-9_-]{30,}' | head -1 || true)
  [ -n "$TOK" ] || { echo "Не вижу токена. Запусти скрипт снова."; exit 1; }
  rnd() { tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24; }
  umask 077
  cat > .env <<ENV
PG_ADMIN_PASSWORD=$(rnd)
PG_READER_PASSWORD=$(rnd)
AI_PROVIDER=openai
OPENAI_API_KEY=$KEY
OPENAI_MODEL=gpt-4.1-mini
TELEGRAM_BOT_TOKEN=$TOK
TELEGRAM_LIMIT_PER_HOUR=30
LOG_LEVEL=INFO
ENV
  echo "Ключи сохранены в $DIR/.env (только root может читать)"
else
  echo ".env уже есть, оставляю"
fi
if ! grep -q '^TELEGRAM_ADMIN_CHAT_ID=[0-9]' .env; then
  echo "Мониторинг: сюда будут приходить уведомления, если бот упадёт или обновление не пройдёт."
  read -r -p "Твой Telegram ID (бот покажет его по команде /id; Enter = без уведомлений): " AID
  AID=$(printf '%s' "$AID" | tr -dc 0-9)
  [ -n "$AID" ] && { sed -i '/^TELEGRAM_ADMIN_CHAT_ID=/d' .env; echo "TELEGRAM_ADMIN_CHAT_ID=$AID" >> .env; }
fi
set -a; . ./.env; set +a

say "4/6 PostgreSQL"
docker compose up -d --wait db
docker compose build -q bot

say "5/6 Данные SIAT: годовые, квартальные, месячные (первый раз 20-40 минут, дальше только изменения)"
HAS=$(docker compose exec -T db psql -tA -U siat -d siat -c "SELECT CASE WHEN to_regclass('siat.observations') IS NULL THEN 0 ELSE (SELECT count(*) FROM siat.observations) END" 2>/dev/null || echo 0)
if [ "$CODE_ONLY" = 1 ] && [ "${HAS:-0}" -gt 0 ]; then
  echo "Только новый код: данные не перекачиваю (ночное обновление в 04:00 сделает это само)"
elif [ "${HAS:-0}" -lt 100000 ] && [ -f deploy/siat_dump.sql.gz ] && [ "${HAS:-0}" -eq 0 ]; then
  echo "Загружаю базу из дампа с Mac (deploy/siat_dump.sql.gz)..."
  gunzip -c deploy/siat_dump.sql.gz | docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' db psql -q -v ON_ERROR_STOP=1 -U siat -d siat >/dev/null
else
  # first install: everything; re-run (upgrade): only new / changed indicators. Districts are
  # downloaded afterwards in the background (step 6), so this step stays under ~30 minutes.
  docker compose run --rm -T -e DISTRICT_MINUTES=0 bot ./update_data.sh 2>&1 \
    | grep --line-buffered -E "progress|done:|normalized:|\| (FAIL|WARN)|FAIL:|ERROR" || true
fi
# schema.sql is idempotent: creates the read-only role and its grants (also after a dump restore)
docker compose exec -T -e PGOPTIONS='-c client_min_messages=warning' db psql -q -U siat -d siat < schema.sql >/dev/null
docker compose exec -T db psql -q -U siat -d siat -c "ALTER ROLE siat_reader PASSWORD '$PG_READER_PASSWORD'" >/dev/null
docker compose exec -T db psql -tA -U siat -d siat -c \
  "SELECT 'В базе: ' || count(DISTINCT indicator_id) || ' индикаторов, ' || count(*) || ' значений' FROM siat.observations"
docker compose run --rm -T bot python query_agent.py "2020–2025-yillarda Toshkent viloyatida sanoat mahsulotlari hajmi qanday o‘zgargan?" | head -3

say "6/6 Бот и ночное обновление"
docker compose up -d bot
mkdir -p /etc/cron.d
cat > /etc/cron.d/siat-update <<CRON
CRON_TZ=Asia/Tashkent
0 4 * * * root $DIR/deploy/update.sh >> /var/log/siat-update.log 2>&1
*/5 * * * * root $DIR/deploy/monitor.sh >> /var/log/siat-monitor.log 2>&1
CRON
mkdir -p /var/lib/siat-monitor
[ -f /var/lib/siat-monitor/last_update_ok ] || date +%s > /var/lib/siat-monitor/last_update_ok
sleep 5
docker compose logs --tail 3 bot
deploy/notify.sh "Мониторинг включён: сюда придёт сообщение, если бот упадёт или ночное обновление не пройдёт."
grep -q '^TELEGRAM_ADMIN_CHAT_ID=[0-9]' .env && echo "Тестовое уведомление отправлено тебе в Telegram."
# district values: a few hours of slow downloads (SIAT limits request speed), so in the background;
# the nightly update continues if this run does not finish
# (a short first run so the first districts, e.g. population, appear within the hour, then a long one)
[ "$CODE_ONLY" = 1 ] || nohup setsid bash -c "DISTRICT_MINUTES=20 '$DIR/deploy/update.sh'; DISTRICT_MINUTES=300 '$DIR/deploy/update.sh'" \
  >> /var/log/siat-update.log 2>&1 < /dev/null &
[ "$CODE_ONLY" = 1 ] || { echo "Районы (туманы и города) загружаются в фоне: первые примерно через час, все за несколько часов."; echo "Бот работает и в это время."; }

say "Готово!"
echo "Бот работает на сервере и перезапускается сам (и после перезагрузки сервера)."
echo "ВАЖНО: останови бота на Mac, иначе два бота с одним токеном будут мешать друг другу:"
echo "   launchctl bootout gui/\$(id -u)/uz.siat.bot ; rm ~/Library/LaunchAgents/uz.siat.bot.plist"
echo "Лог бота:          cd $DIR && sudo docker compose logs -f bot"
echo "Вопрос из консоли: cd $DIR && sudo docker compose run --rm bot python query_agent.py \"...\""
echo "Обновить данные:   sudo $DIR/deploy/update.sh   (сам запускается в 04:00 по Ташкенту)"
echo "Мониторинг:        каждые 5 минут ($DIR/deploy/monitor.sh), лог /var/log/siat-monitor.log"
echo "Ход загрузки районов: tail -f /var/log/siat-update.log"
echo "Проверка вопросов:   cd $DIR && sudo docker compose run --rm bot python eval/run_eval.py --llm"
