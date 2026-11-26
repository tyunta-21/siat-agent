"""Telegram bot for the SIAT agent (long polling, no extra libraries).

Settings in .env:
  TELEGRAM_BOT_TOKEN=123456:ABC...          from @BotFather
  TELEGRAM_ALLOWED_USERS=12345,67890        optional: only these Telegram user ids may ask (empty = everyone)
  TELEGRAM_LIMIT_PER_HOUR=30                optional: questions per user per hour (protects the OpenAI balance)

  TELEGRAM_ADMIN_CHAT_ID=12345              optional: where server monitoring sends alerts (deploy/monitor.sh)

Commands: /start, /help, /id (shows your Telegram id, for TELEGRAM_ADMIN_CHAT_ID).
The bot touches $SIAT_DATA_DIR/heartbeat every polling cycle, so the monitor can see it is alive.

Run:  .venv/bin/python telegram_bot.py
"""
from __future__ import annotations

import html
import os
import re
import time
import traceback
from collections import defaultdict, deque

import requests

from query_agent import ask
from siat_common import DATA_DIR, log

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED = {int(x) for x in re.findall(r"\d+", os.environ.get("TELEGRAM_ALLOWED_USERS", ""))}
LIMIT = int(os.environ.get("TELEGRAM_LIMIT_PER_HOUR", "30") or 30)
API = f"https://api.telegram.org/bot{TOKEN}"
MAX_LEN = 4000  # Telegram limit is 4096 characters per message

HELP = (
    "<b>SIAT statistika agenti</b>\n"
    "O‘zbekiston rasmiy statistikasi (siat.stat.uz) bo‘yicha savol bering. "
    "Raqamlar faqat SIAT bazasidan olinadi.\n\n"
    "<b>Misollar:</b>\n"
    "• 2020–2025-yillarda Toshkent viloyatida sanoat mahsulotlari hajmi qanday o‘zgargan?\n"
    "• Samarqand va Buxoro viloyatlarida ish haqi 2021-2025\n"
    "• Andijonda ishsizlik darajasi 2019-2025\n"
    "• 2025-yil yanvar–sentabr Samarqand viloyatida sanoat mahsulotlari\n"
    "• Xorazm viloyatida go‘sht yetishtirish\n"
    "• Chilonzor tumanida aholi soni 2020-2025\n\n"
    "<b>Ko‘rsatkichlar:</b> SIAT’ning 2 700 dan ortiq hududiy ko‘rsatkichi: yillik, choraklik, oylik va o‘quv "
    "yillari bo‘yicha (aholi, ish haqi, sanoat, qishloq xo‘jaligi, savdo, narxlar, ta’lim va boshqalar). "
    "Ko‘rsatkich nomini o‘z so‘zlaringiz bilan yozing, bot katalogdan eng yaqinini topadi.\n"
    "Hududlar: respublika, Qoraqalpog‘iston, 12 viloyat, Toshkent shahri, SIAT e’lon qilgan bo‘lsa — tumanlar "
    "va shaharlar."
)

_hits: dict[int, deque] = defaultdict(deque)
HEARTBEAT = DATA_DIR / "heartbeat"


def beat() -> None:
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(str(int(time.time())))
    except OSError as e:
        log.warning("heartbeat not written: %s", e)


def tg(method: str, **params):
    r = requests.post(f"{API}/{method}", json=params, timeout=70)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description')}")
    return data["result"]


def md_to_html(text: str) -> str:
    """The agent writes **bold**; Telegram HTML wants <b>."""
    t = html.escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)


def table_to_pre(md: str) -> str:
    """Markdown tables do not render in Telegram: show them as aligned monospace text."""
    blocks = []
    for part in md.split("\n\n"):
        lines = [l for l in part.splitlines() if l.strip()]
        rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in lines
                if l.strip().startswith("|") and not re.match(r"^\|[\s:|-]+\|$", l.strip())]
        if not rows:
            blocks.append(html.escape(part))
            continue
        widths = [max(len(r[i]) if i < len(r) else 0 for r in rows) for i in range(len(rows[0]))]
        out = []
        for r in rows:
            cells = [(r[i] if i < len(r) else "") for i in range(len(widths))]
            out.append("  ".join(c.ljust(widths[0]) if i == 0 else c.rjust(widths[i]) for i, c in enumerate(cells)))
        blocks.append("<pre>" + html.escape("\n".join(out)) + "</pre>")
    return "\n".join(blocks)


def format_answer(ans) -> str:
    body = md_to_html(ans.text)
    if ans.table_md:
        body += "\n\n" + table_to_pre(ans.table_md)
    if len(body) > MAX_LEN:
        body = body[:MAX_LEN - 60] + "\n…\n<i>Javob uzun, savolni toraytiring (hudud yoki yillar).</i>"
        # never leave an unclosed <pre>
        if body.count("<pre>") > body.count("</pre>"):
            body = body.replace("\n…\n<i>", "</pre>\n…\n<i>", 1)
    return body


def allowed_now(uid: int) -> bool:
    q, now = _hits[uid], time.time()
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= LIMIT:
        return False
    q.append(now)
    return True


def handle(msg: dict) -> str | None:
    text = (msg.get("text") or "").strip()
    uid = (msg.get("from") or {}).get("id", 0)
    if not text:
        return None
    if text.startswith("/start") or text.startswith("/help"):
        return HELP
    if text.startswith("/id"):
        return f"Sizning Telegram ID: <code>{uid}</code>"
    if ALLOWED and uid not in ALLOWED:
        return "Kechirasiz, bu bot hozircha yopiq rejimda ishlaydi."
    if not allowed_now(uid):
        return f"Bir soatda {LIMIT} tadan ko‘p savol berib bo‘lmaydi. Birozdan keyin urinib ko‘ring."
    log.info("question from %s: %s", uid, text[:200])
    try:
        return format_answer(ask(text))
    except Exception:
        log.error("answer failed: %s", traceback.format_exc())
        return "Xatolik yuz berdi, savolni qayta yuboring yoki keyinroq urinib ko‘ring."


def main() -> None:
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is empty: add it to .env")
    me = tg("getMe")
    log.info("bot @%s started (allowed users: %s, limit %d/h)", me["username"],
             sorted(ALLOWED) or "everyone", LIMIT)
    tg("setMyCommands", commands=[{"command": "help", "description": "Qanday savol berish mumkin"}])
    offset = 0
    while True:
        beat()
        try:
            updates = tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
        except Exception as e:
            log.warning("getUpdates failed: %s; retry in 5s", e)
            time.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message")
            if not msg:
                continue
            chat = msg["chat"]["id"]
            try:
                tg("sendChatAction", chat_id=chat, action="typing")
                reply = handle(msg)
                if reply:
                    tg("sendMessage", chat_id=chat, text=reply, parse_mode="HTML",
                       disable_web_page_preview=True, reply_to_message_id=msg["message_id"])
            except Exception as e:
                log.error("send failed: %s", e)


if __name__ == "__main__":
    main()
