# SIAT AI Agent

Агент берёт официальную статистику с портала SIAT (siat.stat.uz), хранит её в PostgreSQL и отвечает на
вопросы на узбекском (латиница и кириллица) и русском точными цифрами из базы. В базе все годовые
индикаторы SIAT по регионам (около 2 000), нужный индикатор агент находит поиском по каталогу. Числа приходят только из SQL:
LLM выбирает индикатор, регион и годы и пишет SELECT, а текст ответа и процент изменения считает Python.

## Быстрый старт

```bash
pip install -r requirements.txt
cp .env.example .env            # впиши DATABASE_URL, при желании OPENAI_API_KEY
createdb siat                   # любая PostgreSQL 13+

python siat_catalog.py          # каталог всех 3 261 индикаторов -> data/catalog/catalog.csv
python siat_downloader.py --all-annual-regional --workers 4   # ~2 000 индикаторов -> data/raw/<id>/ (15–25 мин)
python siat_normalizer.py       # -> data/normalized/ (observations.csv и др.)
python load_data.py             # schema.sql + загрузка в PostgreSQL (можно запускать повторно)
python check_data.py            # проверки качества -> data/quality_report.md

./update_data.sh                # то же одной командой; повтор качает только обновлённые на SIAT индикаторы

python query_agent.py "2020–2025-yillarda Toshkent viloyatida sanoat mahsulotlari hajmi qanday o‘zgargan?"
python siat_search.py "kartoshka yetishtirish"   # какой индикатор найдёт поиск
python eval/run_eval.py         # контрольные вопросы, каждая цифра сверяется с живым API SIAT
```

Пример ответа:

```
**Sanoat mahsulotlari ishlab chiqarish hajmi (yillik)** (mlrd. so‘m, 2020–2025):
- Toshkent viloyatida 2020-yildagi 77 246,5 dan 2025-yilda 192 416,9 ga oshgan, ya’ni +149,1% (mlrd. so‘m).

Manba: SIAT (Milliy statistika qo‘mitasi), dataset kodi 1.02.01.0001 (SIAT id 552), SIAT’da yangilangan: 2026-11-13
| Yil | Toshkent viloyati |
|---|---:|
| 2020 | 77 246,5 |
| ... | ... |
```

## Файлы

| Файл | Что делает |
|---|---|
| `siat_common.py` | Настройки, список индикаторов MVP, HTTP с повторами (SIAT иногда рвёт соединение) |
| `siat_catalog.py` | Всё дерево каталога SIAT -> `catalog.json` / `catalog.csv` |
| `siat_downloader.py` | Raw-данные индикатора без изменений + `manifest.json` (sha256) + `download_log.jsonl`. Повторный запуск пропускает неизменённые индикаторы. `--all-annual-regional --workers 4` для всего каталога |
| `siat_normalizer.py` | Длинный формат: индикатор × регион × период. Понимает `2020`, `2020-Q3`, `2020-M09`. Чистит единицы и пустые значения |
| `schema.sql` | Схема `siat`: raw_json, datasets, indicators, regions, periods, observations, metadata, load_log, view `v_obs`, роль `siat_reader` только на чтение |
| `load_data.py` | Идемпотентная загрузка (upsert) с журналом в `load_log` |
| `siat_search.py` | Поиск индикатора по всему каталогу: названия uz/ru/en/кириллица, раздел каталога, синонимы. Учитывает окончания слов, редкость слов и «производные» (темп роста, на душу, доля). Без внешних библиотек |
| `check_data.py` | Проверки после загрузки: полнота против raw-файлов, 15 регионов, Toshkent 1726/1727, годы, единицы, даты обновления, республика ≈ сумма регионов |
| `update_data.sh` | Обновление данных одной командой: каталог -> скачать изменённое -> нормализовать -> загрузить -> проверить |
| `siat_dictionary.py` | Синонимы индикаторов и регионов (uz латиница/кириллица, ru, en). Новые слова добавлять сюда |
| `query_agent.py` | Вопрос -> поиск индикатора -> SQL -> таблица + текст + источник. В режиме LLM модель получает 25 лучших кандидатов из поиска (не весь каталог). Режим правил работает без ключа |
| `telegram_bot.py` | Telegram-бот: вопрос -> ответ агента с таблицей. Токен и лимиты в `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_LIMIT_PER_HOUR`) |
| `eval/run_eval.py` | Контрольные вопросы; каждая цифра сверяется с живым `siat.stat.uz/api/sdmx/<id>/table/` |

## API SIAT (открытый, без ключа)

База `https://siat.stat.uz/api`:
`/sdmx/json/` дерево каталога, `/sdmx/last-layer/?page=N` список индикаторов с классификатором (1 = Hududlar),
`/sdmx/search/?q=` поиск, `/sdmx/<id>/table/` значения, `/sdmx/<id>/metadata/` паспорт индикатора,
`/sdmx/<id>/table/download/` ссылки на официальные XML и JSON v2 (с районами).

## Безопасность SQL

1. Только один `SELECT`/`WITH`, без комментариев и `;`, без опасных слов (`pg_*`, `copy`, `set`...).
2. В `FROM`/`JOIN` разрешён только `v_obs`.
3. PostgreSQL сам проверяет план (`EXPLAIN`): запрос может касаться только таблиц за `v_obs`.
4. Транзакция `READ ONLY`, `statement_timeout = 5s`, не больше 1000 строк.
5. Агент подключается ролью `siat_reader` (только SELECT). Пароль из `schema.sql` поменяй.

## Правила ответа

- Цифры только из базы. Нет данных, значит агент так и говорит, без догадок.
- «Toshkent» без уточнения: агент переспрашивает, шахар (1726) или вилоят (1727).
- Для показателей в процентах (ishsizlik) изменение даётся в процентных пунктах, для остальных в %.
- В ответе всегда: индикатор, регион, период, таблица, изменение, источник SIAT, код датасета, дата обновления.
- Если похожих индикаторов несколько, агент отвечает по самому близкому и перечисляет 2–3 похожих.
- Если поиск не уверен (слова вопроса не объясняются ни одним индикатором), агент не гадает, а предлагает варианты.
- Если у индикатора нет данных по нужному региону или годам, агент говорит, какие регионы и годы есть.

## Что дальше

- Поиск по смыслу (embeddings) поверх текущего поиска по словам, если появятся вопросы, которые он не понимает.
- Квартальные и месячные данные (нормализатор уже их понимает; учесть, что SIAT даёт их нарастающим итогом).
- Районы (JSON v2 из `/table/download/`), графики, Telegram-бот или веб-чат, ночное обновление по `updated_xlsx`.

## Сервер (AWS Lightsail / любой VPS с Ubuntu)

Всё в Docker: `db` (PostgreSQL 16, наружу не открыт) и `bot` (Telegram-бот). Данные обновляются
каждую ночь в 04:00 по Ташкенту (`/etc/cron.d/siat-update` → `deploy/update.sh`).

1. Скопировать `install_siat_server.sh` на сервер (в Lightsail: SSH в браузере → «Upload file»).
2. `sudo bash install_siat_server.sh` — спросит ключ OpenAI и токен бота, скачает данные SIAT (15–25 мин).
   Если SIAT не отвечает серверу: на Mac `bash ~/siat_agent/deploy/export_from_mac.sh`,
   положить `siat_dump.sql.gz` рядом со скриптом на сервере и запустить скрипт ещё раз.
3. Остановить бота на Mac: `launchctl bootout gui/$(id -u)/uz.siat.bot` (один токен — один бот).

Мониторинг (`deploy/monitor.sh`, cron каждые 5 минут): если бот не запущен или не опрашивает Telegram
больше 5 минут, база не отвечает, данные не обновлялись больше 36 часов или диск заполнен на 90 %, бот
присылает сообщение в Telegram на `TELEGRAM_ADMIN_CHAT_ID` (свой ID бот показывает по команде `/id`).
Когда всё восстановится, придёт второе сообщение. Ошибка ночного обновления (`deploy/update.sh`) тоже
приходит сразу. Остановленный бот монитор перезапускает сам.

Код на сервере: `/opt/siat_agent`, ключи: `/opt/siat_agent/.env` (только root).
Лог: `sudo docker compose logs -f bot`, проверка: `sudo docker compose run --rm bot python eval/run_eval.py --llm`.
