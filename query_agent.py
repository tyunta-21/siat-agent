"""SIAT query agent: Uzbek question -> SQL over siat.v_obs -> table + text answer with source.

Two modes:
  * LLM mode (OPENAI_API_KEY or OPENROUTER_API_KEY set): the model reads the question plus the best
    candidate indicators from the catalog search (siat_search.py) and the regions, and writes one SELECT over v_obs. The SQL is checked, run read-only, and the answer
    text is built by Python from the SQL result (the model never supplies numbers).
  * Rule mode (no key, or --rules): the indicator is found with the synonym dictionary and the
    catalog-wide search, region / years with dictionaries, and the SQL is built from a template. Good for typical questions and for testing without a key.

Usage:
  python query_agent.py "2020–2025-yillarda Toshkent viloyatida sanoat mahsulotlari hajmi qanday o‘zgargan?"
  python query_agent.py --json "..."        # machine-readable result
  python query_agent.py                     # interactive mode
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field

import psycopg
import requests

from siat_common import DATABASE_URL, log
from siat_dictionary import INDICATOR_SYNONYMS, REGION_ALIASES, norm
from siat_search import get_index
from siat_time import TimeAsk, parse_time, period_label, school_years

AGENT_DB_URL = os.environ.get("AGENT_DATABASE_URL", DATABASE_URL)  # ideally the read-only siat_reader role
# Any OpenAI-compatible chat API. Default: OpenAI (same key as Qonunchi). Alternative: OpenRouter.
AI_PROVIDER = os.environ.get("AI_PROVIDER", "openai").strip().lower()
if AI_PROVIDER == "openrouter":
    LLM_URL = "https://openrouter.ai/api/v1/chat/completions"
    LLM_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
    LLM_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4.1-mini")
else:
    LLM_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
    LLM_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
    LLM_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
MAX_ROWS = 1000
MIN_CONFIDENCE = 0.7   # share of the question's words the found indicator must explain
LLM_CANDIDATES = 25    # indicators shown to the LLM (the whole catalog is too long for every question)


# ----------------------------------------------------------------------------- data classes
@dataclass
class Plan:
    sql: str
    params: dict = field(default_factory=dict)
    mode: str = "rules"
    indicator_ids: list[int] = field(default_factory=list)
    region_codes: list[str] = field(default_factory=list)
    year_from: int | None = None
    year_to: int | None = None
    alternatives: list[dict] = field(default_factory=list)  # similar indicators to suggest
    freq: str = "A"                 # A annual, Q quarterly, M monthly, S school year
    sub: int | None = None          # quarter / month shown for every year (None = all of them)
    note: str = ""                  # e.g. "only yearly data, the asked part of the year is not published"


@dataclass
class Answer:
    ok: bool
    text: str
    table_md: str = ""
    rows: list[dict] = field(default_factory=list)
    sql: str = ""
    mode: str = ""
    sources: list[dict] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)


class NeedClarification(Exception):
    pass


# ----------------------------------------------------------------------------- formatting
def fmt_num(v: float | None, digits: int = 1) -> str:
    """Uzbek style: space as thousands separator, comma as decimal mark, fixed decimals."""
    if v is None:
        return "—"
    return f"{v:,.{digits}f}".replace(",", " ").replace(".", ",")


def decimals_of(values) -> int:
    """How many decimals the series uses (0..2), so 98 482 is shown as 98 482,0 next to 77 246,5."""
    d = 0
    for v in values:
        if v is None:
            continue
        s = f"{float(v):.2f}".rstrip("0").split(".")
        d = max(d, len(s[1]) if len(s) > 1 else 0)
    return min(d, 2)


def fmt_pct(p: float) -> str:
    return ("+" if p > 0 else "") + fmt_num(p, 1) + "%"


# ----------------------------------------------------------------------------- rule-based parsing
def _alias_hits(q: str, table: dict) -> list[tuple[int, object]]:
    hits = []
    for key, words in table.items():
        for w in words:
            w2 = norm(w)
            if re.search(r"(?<![a-zа-я'])" + re.escape(w2), q):
                hits.append((len(w2), key))
    return hits


def find_indicators(q: str) -> list[int]:
    hits = _alias_hits(q, INDICATOR_SYNONYMS)
    if not hits:
        return []
    hits.sort(reverse=True)
    best = hits[0][1]
    # "jon boshiga YaHM" contains "YaHM": keep the most specific match, plus other unrelated indicators
    chosen = [best]
    for ln, k in hits[1:]:
        if k in chosen:
            continue
        overlaps = any(norm(w) in norm(x) for x in INDICATOR_SYNONYMS[best] for w in INDICATOR_SYNONYMS[k])
        if not overlaps:
            chosen.append(k)
    return chosen


_districts: dict[str, list[dict]] = {}


def district_list(conn) -> list[dict]:
    """Districts and district-level cities in the database (cached per process)."""
    if conn is None:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM siat.regions WHERE level = 'district'")
        key = str(cur.fetchone()[0])
        if key not in _districts:
            cur.execute("""SELECT d.code, d.name_uz, d.parent_code, p.name_uz, d.aliases
                           FROM siat.regions d LEFT JOIN siat.regions p ON p.code = d.parent_code
                           WHERE d.level = 'district' ORDER BY d.code""")
            _districts.clear()
            _districts[key] = [dict(code=c, name=n, parent=pc, parent_name=pn, aliases=al or [])
                               for c, n, pc, pn, al in cur.fetchall()]
    return _districts[key]


def find_districts(q: str, conn) -> list[str]:
    """District codes named in the question ("Chilonzor tumanida", "Nukus shahrida")."""
    found: dict[str, tuple[int, str]] = {}  # code -> (alias length, alias)
    for d in district_list(conn):
        for a in d["aliases"]:
            if a.split(" ")[0] in q and re.search(r"(?<![a-zа-я'])" + re.escape(a), q):
                if len(a) > found.get(d["code"], (0, ""))[0]:
                    found[d["code"]] = (len(a), a)
    if not found:
        return []
    by_code = {d["code"]: d for d in district_list(conn)}
    # the same name in two regions ("Yangiobod tumani"): use the region named in the question, or ask
    out: list[str] = []
    for alias in dict.fromkeys(a for _, a in found.values()):
        codes = [c for c, (_, a) in found.items() if a == alias]
        if len(codes) > 1:
            named = {code for _, code in _alias_hits(q, REGION_ALIASES)}
            narrowed = [c for c in codes if by_code[c]["parent"] in named]
            if len(narrowed) == 1:
                codes = narrowed
            else:
                opts = "; ".join(f"{by_code[c]['name']} ({by_code[c]['parent_name']})" for c in codes)
                raise NeedClarification(f"Bu nomli tuman/shahar bir nechta: {opts}. Qaysi viloyatdagisi? "
                                        "Viloyat nomini ham yozing.")
        out += [c for c in codes if c not in out]
    return out


def explicit_regions(q: str, conn=None) -> bool:
    """Does the question name a region or district itself (not the default "whole republic")?"""
    if find_districts(q, conn) or _alias_hits(q, REGION_ALIASES):
        return True
    return bool(re.search(r"toshkent|ташкент", q))


def find_regions(q: str, conn=None) -> list[str]:
    districts = find_districts(q, conn)
    parents = {c[:4] for c in districts}
    codes = []
    if ("toshkent" in q or "ташкент" in q) and not any(p in ("1726", "1727") for p in parents):
        city = re.search(r"toshkent (shahr|sh\b)|tashkent city|город ташкент|г\. ?ташкент|ташкент город", q)
        oblast = re.search(r"toshkent vil|tashkent region|ташкентск", q)
        if city:
            codes.append("1726")
        if oblast:
            codes.append("1727")
        if not city and not oblast:
            raise NeedClarification("Toshkent shahri (1726) yoki Toshkent viloyati (1727) nazarda tutilganmi? "
                                    "Iltimos, aniqlashtiring.")
    for _, code in sorted(_alias_hits(q, {k: v for k, v in REGION_ALIASES.items()
                                          if k not in ("1726", "1727")}), reverse=True):
        if code not in codes:
            codes.append(code)
    # "Samarqand viloyati tumanlari", "по районам": every district of the region(s)
    if re.search(r"tumanlar|tuman va shaharlar|по районам|районы|районов|районах", q):
        regs = [c for c in codes if c != "1700"]
        if not regs:
            raise NeedClarification("Qaysi viloyat tumanlari kerak? Viloyat nomini yozing "
                                    "(masalan: Samarqand viloyati tumanlari).")
        return [d["code"] for d in district_list(conn) if d["parent"] in regs] or regs
    if districts:
        # "Samarqand shahri" is a district-level city: the region with the same name is not asked
        return districts + [c for c in codes if c not in parents and c != "1700"]
    if "barcha viloyat" in q or "viloyatlar kesimida" in q or "hududlar kesimida" in q or "по областям" in q:
        return ["*"]
    return codes or ["1700"]


def find_years(q: str) -> tuple[int | None, int | None]:
    school = school_years(q)
    if school:  # "2016/2017" is one school year, stored as 2016
        return min(school), max(school)
    ys = [int(y) for y in re.findall(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)", q)]
    if not ys:
        return None, None
    return min(ys), max(ys)


def _alt(h) -> dict:
    return {"indicator_id": h.id, "dataset_code": h.code, "name": h.name, "unit": h.unit}


def prefer_twin(t: TimeAsk, chosen: list[int], hits) -> list[int]:
    """"yanvar–sentabr sanoat": the synonym points to the yearly series; use its quarterly / monthly
    twin when the catalog has one that fits the question as well."""
    if not (t.month or t.freq in ("Q", "M")) or not hits or not chosen:
        return chosen
    main = next((h for h in hits if h.id == chosen[0]), None)
    if main and main.freqs and not set(main.freqs) & {"Q", "M"}:
        twin = next((h for h in hits if set(h.freqs) & {"Q", "M"} and h.confidence >= max(MIN_CONFIDENCE,
                     main.confidence) and h.score >= main.score - 2.5), None)
        if twin:
            return [twin.id]
    return chosen


def choose_indicators(question: str, conn) -> tuple[list[int], list[dict]]:
    """Synonym dictionary first (tested MVP wording), then the catalog-wide search.

    A specific catalog match beats a generic synonym: "qishloq aholisi soni" is rural population,
    not total population, although "aholi soni" is a synonym of the latter."""
    q = norm(question)
    syn = find_indicators(q)
    regions = find_regions(q, conn)
    t = parse_time(question)
    hits = get_index(conn).search(question, top=8, regions=regions, freq=t.freq, sub_year=bool(t.month))
    top = hits[0] if hits else None
    syn_best = max((h.score for h in hits if h.id in syn), default=0.0)
    if top and top.id not in syn and top.confidence >= MIN_CONFIDENCE and (not syn or top.score > syn_best + 1.0):
        chosen = [top.id]
    elif syn:
        chosen = syn
    elif top and top.confidence >= MIN_CONFIDENCE:
        chosen = [top.id]
    elif hits:
        opts = "\n".join(f"{i}) {h.name} (kod {h.code})" for i, h in enumerate(hits[:5], 1))
        raise NeedClarification("Savoldagi ko‘rsatkichni aniq topa olmadim. Quyidagilardan birini nazarda "
                                "tutdingizmi? Nomini savolga qo‘shib qayta yozing:\n" + opts)
    else:
        raise NeedClarification("SIAT’ning hududiy ma’lumotlarida bunday ko‘rsatkich topilmadi. "
                                "Ko‘rsatkich nomini boshqacha yozib ko‘ring (masalan: aholi soni, ish haqi, "
                                "sanoat mahsulotlari, eksport).")
    chosen = prefer_twin(t, chosen, hits)
    best = top.score if top else 0
    alts = [_alt(h) for h in hits if h.id not in chosen and h.score >= 0.6 * best][:3]
    return chosen, alts


FREQ_ORDER = ("A", "S", "Q", "M")


def resolve_time(conn, inds: list[int], t: TimeAsk, y1: int | None, y2: int | None):
    """Which series to show: frequency, part of the year and years.

    Annual data is the default. Quarterly / monthly data is used when the question asks for it
    ("yanvar–sentabr", "III chorak", "oylar bo‘yicha") or when the indicator has nothing else
    (meat production is only quarterly). Without a part of the year, the latest published one is
    shown for every year, so years are compared like with like (January–September with
    January–September). Returns (indicator ids that have that frequency, freq, sub, y1, y2)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT indicator_id, freq, max(year) FROM v_obs WHERE indicator_id = ANY(%s)
                       GROUP BY 1, 2""", (inds,))
        have: dict[int, dict[str, int]] = {}
        for i, f, y in cur.fetchall():
            have.setdefault(i, {})[f] = y
    if not have:
        return inds, "A", None, y1, y2
    main = next(i for i in inds if i in have)
    fr = have[main]
    if t.freq and t.freq in fr:
        freq = t.freq
    elif t.month and ("M" in fr or "Q" in fr):
        freq = "M" if "M" in fr and (t.month % 3 or "Q" not in fr) else "Q"
    else:
        freq = next(f for f in FREQ_ORDER if f in fr)
    kept = [i for i in inds if freq in have.get(i, {})]
    sub = None
    if freq in ("Q", "M"):
        if t.every:
            if not y1:  # every quarter / month of the latest year
                y1 = y2 = fr[freq]
        elif t.month:
            sub = t.month if freq == "M" else max(1, t.month // 3)
        else:
            with conn.cursor() as cur:
                cur.execute("""SELECT coalesce(quarter, month) FROM v_obs
                               WHERE indicator_id = %s AND freq = %s AND value IS NOT NULL
                                 AND (%s::int IS NULL OR year <= %s::int)
                               ORDER BY year DESC, coalesce(quarter, month) DESC LIMIT 1""",
                            (main, freq, y2, y2))
                r = cur.fetchone()
            sub = r[0] if r else None
    return kept, freq, sub, y1, y2


def time_note(t: TimeAsk, freq: str) -> str:
    if (t.month or t.freq in ("Q", "M")) and freq in ("A", "S"):
        asked = ("chorak / oy" if not t.month else
                 period_label("M", 2000, t.month, True).split(" ", 1)[1])
        return (f"Bu ko‘rsatkich SIAT’da faqat yillik e’lon qilinadi: so‘ralgan davr ({asked}) bo‘yicha "
                "ma’lumot yo‘q, shuning uchun yillik qiymatlar ko‘rsatildi.")
    if t.freq == "S" and freq != "S":
        return "Bu ko‘rsatkich o‘quv yillari bo‘yicha emas, kalendar yillari bo‘yicha e’lon qilinadi."
    return ""


def template_sql(inds: list[int], regions: list[str], y1: int | None, y2: int | None,
                 freq: str = "A", sub: int | None = None) -> tuple[str, dict]:
    sql = ["SELECT indicator_id, dataset_code, indicator, unit, region_code, region, year, value,",
           "       freq, quarter, month, period, cumulative",
           "FROM v_obs WHERE freq = %(freq)s AND indicator_id = ANY(%(inds)s)"]
    params: dict = {"inds": inds, "freq": freq}
    if regions and regions != ["*"]:
        sql.append("AND region_code = ANY(%(regions)s)")
        params["regions"] = regions
    elif regions == ["*"]:
        sql.append("AND region_level <> 'district'")
    if sub:
        sql.append("AND coalesce(quarter, month) = %(sub)s")
        params["sub"] = sub
    if y1:
        sql.append("AND year BETWEEN %(y1)s AND %(y2)s")
        params.update(y1=y1, y2=y2 or y1)
    sql.append("ORDER BY indicator_id, region_code, year, quarter, month")
    return "\n".join(sql), params


def plan_rules(question: str, conn) -> Plan:
    q = norm(question)
    inds, alts = choose_indicators(question, conn)
    regions = find_regions(q, conn)
    y1, y2 = find_years(q)
    t = parse_time(question)
    inds, freq, sub, y1, y2 = resolve_time(conn, inds, t, y1, y2)
    sql, params = template_sql(inds, regions, y1, y2, freq, sub)
    return Plan(sql=sql, params=params, mode="rules", indicator_ids=inds,
                region_codes=regions, year_from=y1, year_to=y2, alternatives=alts, freq=freq, sub=sub,
                note=time_note(t, freq))


# ----------------------------------------------------------------------------- LLM planning
SYSTEM_PROMPT = """Sen O‘zbekiston rasmiy statistikasi (SIAT) bo‘yicha SQL yozuvchi yordamchisan.
Foydalanuvchi savoliga javob berish uchun PostgreSQL'da BITTA SELECT so‘rovini yoz.
Faqat `v_obs` view'idan foydalan. Ustunlar:
  indicator_id int, dataset_code text, indicator text, unit text,
  region_code text, region text, region_level text ('country' / 'region' / 'district'),
  freq char ('A' yillik, 'Q' choraklik, 'M' oylik, 'S' o‘quv yili), year int, quarter int, month int,
  value numeric, period text, cumulative bool, parent_code text
Qoidalar:
- Har doim indicator_id, dataset_code, indicator, unit, region_code, region, year, value ustunlarini qaytar
  (agar agregat kerak bo‘lmasa). ORDER BY indicator_id, region_code, year.
- Odatda yillik ma'lumotni ol: freq = 'A'. Chorak, oy, "yanvar–sentabr" kabi davrlar va o‘quv yillarini
  Python o‘zi hal qiladi, sen faqat to‘g‘ri ko‘rsatkich, hudud va yillarni tanla. Savolda chorak, oy yoki
  o‘quv yili borligi sababli HECH QACHON clarify qilma: bazada choraklik, oylik va o‘quv yili ma'lumotlari bor.
- Tuman yoki shahar kodlari (7 xonali) faqat "Savoldagi tumanlar" ro‘yxatida berilgan bo‘lsa ishlatiladi.
- Toshkent viloyati = '1727', Toshkent shahri = '1726', O‘zbekiston Respublikasi = '1700'.
  Agar savolda faqat "Toshkent" deyilgan bo‘lsa va qaysi biri aniq bo‘lmasa, clarify maydonini to‘ldir.
- Hudud ko‘rsatilmasa, '1700' (respublika) ni ol.
- Savolda bitta ko‘rsatkich so‘ralgan bo‘lsa, FAQAT BITTA, eng to‘g‘ridan-to‘g‘ri mos ko‘rsatkichni tanla
  (masalan "kartoshka yetishtirish" = yetishtirilgan kartoshka hajmi, ekilgan maydon emas; "ishsizlik" =
  ishsizlik darajasi, ishsizlar soni emas). Bir nechta ko‘rsatkichni faqat savolda bir nechta narsa
  aniq so‘ralgan bo‘lsa tanla. Savoldagi tushuncha (masalan "hosildorlik") nomzod nomida bo‘lmasa,
  boshqa ko‘rsatkich bilan almashtirma va hisoblab chiqarma: clarify maydonini to‘ldir.
- Ko‘rsatkichni faqat berilgan nomzodlar ro‘yxatidan tanla (ro‘yxat qidiruv bo‘yicha saralangan,
  birinchilari savolga eng yaqin). Hosila ko‘rsatkichlarni (o‘sish sur’ati, jon boshiga, ulushi, indeks)
  faqat savolda so‘ralgan bo‘lsa tanla. Mos ko‘rsatkich bo‘lmasa, clarify maydoniga 2-3 ta yaqin
  nomzodni yozib, qaysi biri kerakligini so‘ra. Savolda viloyat so‘ralgan bo‘lsa, "faqat respublika"
  ko‘rsatkichini emas, hududlar bo‘yicha ma’lumoti bor ko‘rsatkichni tanla.
- Raqamlarni o‘zing to‘qima, faqat SQL yoz.
Javobni faqat JSON ko‘rinishida ber:
{"sql": "...", "indicator_ids": [..], "region_codes": [..], "year_from": int|null, "year_to": int|null,
 "clarify": null | "foydalanuvchiga savol"}"""


def catalog_context(conn, question: str) -> tuple[str, list]:
    """Top candidates from the catalog search plus the synonym matches, not all 2 000+ indicators."""
    try:
        regions = find_regions(norm(question), conn)
    except NeedClarification:
        regions = None
    t = parse_time(question)
    hits = get_index(conn).search(question, top=LLM_CANDIDATES, regions=regions, freq=t.freq,
                                  sub_year=bool(t.month))
    n_regions = {h.id: h.regions for h in hits}
    ids = [h.id for h in hits]
    ids += [i for i in find_indicators(norm(question)) if i not in ids]
    with conn.cursor() as cur:
        cur.execute("""SELECT i.id, i.code, i.name_uz, i.unit_uz, i.synonyms, d.path_uz
                       FROM indicators i LEFT JOIN datasets d ON d.id = i.id WHERE i.id = ANY(%s)""", (ids,))
        info = {r[0]: r for r in cur.fetchall()}
        cur.execute("SELECT code, name_uz FROM regions WHERE level <> 'district' ORDER BY code")
        regs = cur.fetchall()
        dist = [r for r in (regions or []) if len(r) == 7]
        cur.execute("SELECT code, name_uz FROM regions WHERE code = ANY(%s) ORDER BY code", (dist,))
        dist_rows = cur.fetchall()
    lines = ["Nomzod ko‘rsatkichlar (indicator_id | kod | nomi | birlik | bo‘lim | hududlar | sinonimlar):"]
    for i in ids:
        if i in info:
            _, c, n, u, syn, path = info[i]
            nr = n_regions.get(i)
            cover = "faqat respublika" if nr == 1 else (f"{nr} hudud" if nr else "")
            lines.append(f"{i} | {c} | {n} | {u} | {path or ''} | {cover} | {', '.join(syn or [])}")
    if len(lines) == 1:
        lines.append("(qidiruv hech narsa topmadi)")
    lines.append("Hududlar (region_code | nomi):")
    lines += [f"{c} | {n}" for c, n in regs]
    if dist_rows:
        lines.append("Savoldagi tumanlar / shaharlar (region_code | nomi):")
        lines += [f"{c} | {n}" for c, n in dist_rows]
    return "\n".join(lines), hits


def plan_llm(question: str, conn) -> Plan:
    ctx, hits = catalog_context(conn, question)
    r = requests.post(
        LLM_URL,
        headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"},
        json={"model": LLM_MODEL, "temperature": 0,
              "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": ctx + "\n\nSavol: " + question}]},
        timeout=60)
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", content, re.S)
    if not m:
        raise ValueError(f"LLM did not return JSON: {content[:200]}")
    d = json.loads(m.group(0))
    if d.get("clarify"):
        if parse_time(question).any:
            # the model tends to refuse "yanvar–sentabr" / "o'quv yili" as "only yearly data";
            # the rule planner knows the quarterly / monthly / school-year series
            try:
                plan = plan_rules(question, conn)
                plan.mode = f"llm:{AI_PROVIDER}:{LLM_MODEL}+rules"
                return plan
            except NeedClarification:
                pass
        raise NeedClarification(d["clarify"])
    chosen = [int(x) for x in d.get("indicator_ids") or [] if str(x).isdigit()]
    regions = [str(x) for x in d.get("region_codes") or []]
    y1 = d.get("year_from") if isinstance(d.get("year_from"), int) else None
    y2 = d.get("year_to") if isinstance(d.get("year_to"), int) else None
    final = check_llm_choice(question, conn, chosen, hits)
    best = hits[0].score if hits else 0
    alts = [_alt(h) for h in hits if h.id not in final and h.score >= 0.6 * best][:3]
    mode = f"llm:{AI_PROVIDER}:{LLM_MODEL}"
    q = norm(question)
    parsed = find_regions(q, conn)
    named = explicit_regions(q, conn)
    if named and parsed != ["*"] and set(parsed) != set(regions):
        # the question names its regions ("O'zbekistonda", "Chilonzor tumanida"): the parser's codes
        # are exact, the model sometimes widens them to every region
        regions = parsed
    if school_years(q):
        y1, y2 = find_years(q)
    t = parse_time(question)
    final = prefer_twin(t, final, hits)
    kept, freq, sub, y1t, y2t = resolve_time(conn, final, t, y1, y2)
    if kept:
        final = kept
    if (set(final) != set(chosen) or freq != "A" or t.any or any(len(c) == 7 for c in regions)
            or (named and parsed != ["*"])):
        # The guard trimmed the model's pick, or the answer needs quarters / months / school years /
        # districts: run the standard query for the chosen indicator(s), regions and years.
        sql, params = template_sql(final, regions or ["1700"], y1t, y2t, freq, sub)
        return Plan(sql=sql, params=params, mode=mode + "+template", indicator_ids=final, region_codes=regions,
                    year_from=y1t, year_to=y2t, alternatives=alts, freq=freq, sub=sub, note=time_note(t, freq))
    return Plan(sql=d["sql"], mode=mode, indicator_ids=final, region_codes=regions, year_from=y1, year_to=y2,
                alternatives=alts)


def check_llm_choice(question: str, conn, chosen: list[int], hits) -> list[int]:
    """Guard against the model's two typical mistakes, using the same catalog search as rule mode.

    1. Several related indicators for a one-indicator question ("kartoshka" -> harvest AND sown area,
       "ishsizlik" -> rate AND number of unemployed). Keep the ones the rule chooser also picks; if it
       picks none of them, keep the model's best-ranked one.
    2. An indicator that does not cover the question's meaning ("paxta hosildorligi" -> cotton harvest,
       because there is no cotton yield indicator). Every kept indicator must be a synonym match or
       explain at least MIN_CONFIDENCE of the question's words; otherwise ask the user."""
    conf = {h.id: h.confidence for h in hits}
    rank = {h.id: i for i, h in enumerate(hits)}
    syn = set(find_indicators(norm(question)))
    valid = [i for i in chosen if i in syn or conf.get(i, 0.0) >= MIN_CONFIDENCE]
    if not valid:
        opts = "\n".join(f"{i}) {h.name} (kod {h.code})" for i, h in enumerate(hits[:5], 1))
        raise NeedClarification("Savoldagi ko‘rsatkichni aniq topa olmadim. Quyidagilardan birini nazarda "
                                "tutdingizmi? Nomini savolga qo‘shib qayta yozing:\n" + opts if opts else
                                "SIAT’ning yillik hududiy ma’lumotlarida bunday ko‘rsatkich topilmadi.")
    try:
        rule_pick, _ = choose_indicators(question, conn)
    except NeedClarification:
        rule_pick = []
    if len(valid) == 1:
        # "o'lim koeffitsienti" -> the model took the urban rate (shahar), the search the total one:
        # when the search clearly prefers a plain indicator the question fully explains, follow it
        score = {h.id: h.score for h in hits}
        r = rule_pick[0] if len(rule_pick) == 1 else None
        if (r and r != valid[0] and conf.get(r, 0.0) >= MIN_CONFIDENCE
                and score.get(r, -99) >= score.get(valid[0], -99) + 0.75):
            return [r]
        return valid
    both = [i for i in valid if i in rule_pick]
    if both:
        return both
    return [min(valid, key=lambda i: rank.get(i, 999))]


# ----------------------------------------------------------------------------- SQL safety
FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|grant|revoke|truncate|copy|call|do|"
                       r"execute|prepare|listen|notify|vacuum|analyze|set|reset|lock|pg_\w+|dblink\w*|lo_\w+)\b",
                       re.I)
ALLOWED_RELATIONS = {"v_obs", "siat.v_obs"}


def check_sql(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if ";" in s or "--" in s or "/*" in s:
        raise ValueError("SQL must be a single statement without comments")
    if not re.match(r"^(select|with)\b", s, re.I):
        raise ValueError("Only SELECT queries are allowed")
    if FORBIDDEN.search(s):
        raise ValueError(f"Forbidden keyword in SQL: {FORBIDDEN.search(s).group(0)}")
    cte = {m.lower() for m in re.findall(r"(?:with|,)\s*([a-z_][a-z0-9_]*)\s+as\s*\(", s, re.I)}
    for rel in re.findall(r"\b(?:from|join)\s+([a-z_][a-z0-9_.]*)", s, re.I):
        if rel.lower() not in ALLOWED_RELATIONS | cte:
            raise ValueError(f"Relation not allowed: {rel}")
    return s


PLAN_TABLES = {"siat.observations", "siat.indicators", "siat.regions", "siat.periods"}


def _relations(node) -> set[str]:
    out: set[str] = set()
    if isinstance(node, dict):
        if "Relation Name" in node:
            out.add(f"{node.get('Schema', '?')}.{node['Relation Name']}")
        for v in node.values():
            out |= _relations(v)
    elif isinstance(node, list):
        for v in node:
            out |= _relations(v)
    return out


def run_sql(conn, plan: Plan) -> list[dict]:
    sql = check_sql(plan.sql)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '5s'")
            cur.execute("SET LOCAL search_path = siat")
            wrapped = f"SELECT * FROM ({sql}) q LIMIT {MAX_ROWS}"
            # Second check by PostgreSQL itself: the plan may touch only the tables behind v_obs.
            cur.execute("EXPLAIN (FORMAT JSON, VERBOSE) " + wrapped, plan.params or None)
            used = _relations(cur.fetchone()[0])
            bad = {r for r in used if r not in PLAN_TABLES}
            if bad:
                raise ValueError(f"Query touches tables outside v_obs: {sorted(bad)}")
            cur.execute(wrapped, plan.params or None)
            cols = [c.name for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]


# ----------------------------------------------------------------------------- answer building
FREQ_NAME = {"A": "yillik", "Q": "choraklik", "M": "oylik", "S": "o‘quv yili bo‘yicha"}


def _tkey(r: dict) -> tuple[int, int]:
    return int(r["year"]), int(r.get("quarter") or r.get("month") or 0)


def _label(r: dict, short: bool = False) -> str:
    return period_label(r.get("freq") or "A", int(r["year"]), r.get("quarter") or r.get("month"),
                        bool(r.get("cumulative")), short=short)


def available_text(conn, inds: list[int]) -> str:
    """What the database has for these indicators, so the user can re-ask."""
    out = ""
    with conn.cursor() as cur:
        cur.execute("""SELECT indicator, dataset_code, freq, min(year), max(year), bool_or(cumulative),
                              mod(max(year * 100 + coalesce(quarter, month, 0)), 100),
                              string_agg(DISTINCT region, ', ' ORDER BY region)
                                  FILTER (WHERE region_level <> 'district'),
                              count(DISTINCT region_code) FILTER (WHERE region_level = 'district')
                       FROM v_obs WHERE indicator_id = ANY(%s) AND value IS NOT NULL
                       GROUP BY indicator, dataset_code, freq ORDER BY dataset_code, freq""", (inds,))
        for name, code, freq, y1, y2, cum, last_sub, regs, n_dist in cur.fetchall():
            regs = regs if regs and len(regs) < 300 else "barcha hududlar"
            span = f"{y1}–{y2}" if y1 != y2 else f"{y1}"
            if freq == "S":
                span = f"{y1}/{y1 + 1}–{y2}/{y2 + 1} o‘quv yillari"
            elif freq in ("Q", "M"):
                last = {"freq": freq, "year": y2, "quarter": last_sub if freq == "Q" else None,
                        "month": last_sub if freq == "M" else None, "cumulative": cum}
                span += f" ({FREQ_NAME[freq]}, oxirgisi: {_label(last)})"
            else:
                span += "-yillar"
            dist = f"; {n_dist} ta tuman/shahar" if n_dist else ""
            out += f"\n\n**{name}** ({code}) bo‘yicha bor ma’lumot: {span}; {regs}{dist}."
    return out


def build_answer(conn, plan: Plan, rows: list[dict]) -> Answer:
    if not rows:
        text = ("Bazada bu savol uchun ma’lumot topilmadi (ko‘rsatkich, hudud yoki davr bo‘yicha). "
                "Raqamni taxmin qilmayman.")
        if plan.indicator_ids:
            text += available_text(conn, plan.indicator_ids)
        return Answer(ok=False, sql=plan.sql, mode=plan.mode, text=text, alternatives=plan.alternatives)
    needed = {"indicator_id", "region", "year", "value"}
    if not needed <= rows[0].keys():
        # Aggregated / custom query: show the table as is.
        cols = list(rows[0].keys())
        md = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n" + "\n".join(
            "| " + " | ".join(fmt_num(float(r[c])) if isinstance(r[c], (int, float)) or hasattr(r[c], "as_tuple")
                              else str(r[c]) for c in cols) + " |" for r in rows)
        return Answer(ok=True, text="So‘rov natijasi (SIAT ma’lumotlari asosida):", table_md=md, rows=rows,
                      sql=plan.sql, mode=plan.mode)

    with conn.cursor() as cur:
        cur.execute("SELECT id, code, name_uz, unit_uz, periodicity_uz, updated_at, cumulative FROM siat.indicators "
                    "WHERE id = ANY(%s)", (sorted({r["indicator_id"] for r in rows}),))
        meta = {i: dict(code=c, name=n, unit=u, periodicity=p, updated=up, cumulative=cu)
                for i, c, n, u, p, up, cu in cur.fetchall()}

    texts, tables, sources = [], [], []
    by_ind: dict[int, dict[str, list[dict]]] = {}
    for r in rows:
        r.setdefault("freq", "A")
        if "cumulative" not in r:
            r["cumulative"] = meta.get(r["indicator_id"], {}).get("cumulative")
        by_ind.setdefault(r["indicator_id"], {}).setdefault(r["region"], []).append(r)

    for ind_id, regions in by_ind.items():
        m = meta[ind_id]
        unit = m["unit"] or ""
        is_rate = "foiz" in unit.lower() or unit.strip() == "%"
        u_ = f" ({unit})" if unit else ""  # some SIAT indicators have no unit in their passport
        allr = [r for rs in regions.values() for r in rs]
        keys = sorted({_tkey(r) for r in allr})
        sample = {_tkey(r): r for r in allr}
        freq = allr[0]["freq"]
        annual = freq == "A"
        dg = decimals_of(r["value"] for r in allr)
        n = lambda v: fmt_num(v, dg)  # noqa: E731
        L = lambda k, short=False: _label(sample[k], short)  # noqa: E731
        if annual:
            period = f"{keys[0][0]}–{keys[-1][0]}" if len(keys) > 1 else f"{keys[0][0]}"
        else:
            period = L(keys[0]) + (f" — {L(keys[-1])}" if len(keys) > 1 else "")
        lines = []
        for region, rs in regions.items():
            vals = {_tkey(r): float(r["value"]) if r["value"] is not None else None for r in rs}
            lk = next((k for k in reversed(keys) if vals.get(k) is not None), None)
            # compare with the same part of the year (January–September with January–September)
            fk = next((k for k in keys if vals.get(k) is not None and k[1] == (lk or (0, 0))[1]), None)
            if lk is None:
                lines.append(f"{region}: ma’lumot yo‘q.")
                continue
            last, first = vals[lk], vals[fk]
            if fk == lk:
                lines.append(f"{region}da {lk[0]}-yilda: {n(last)}{u_}." if annual
                             else f"{region}da {L(lk)}: {n(last)}{u_}.")
                continue
            verb = "oshgan" if last > first else ("kamaygan" if last < first else "o‘zgarmagan")
            fy, ly = (fk[0], lk[0]) if annual else (L(fk), L(lk))
            fs, ls = (f"{fy}-yildagi", f"{ly}-yilda") if annual else (f"{fy}dagi", f"{ly}da")
            if is_rate:
                diff = last - first
                lines.append(f"{region}da {fs} {n(first)}% dan {ls} {n(last)}% ga {verb} "
                             f"({'+' if diff > 0 else ''}{fmt_num(diff, max(dg, 1))} foiz punkt).")
            else:
                pct = (last - first) / first * 100 if first else None
                pct_s = f", ya’ni {fmt_pct(pct)}" if pct is not None else ""
                lines.append(f"{region}da {fs} {n(first)} dan {ls} {n(last)} ga {verb}{pct_s}{u_}.")
        note = ""
        if freq in ("Q", "M") and allr[0].get("cumulative"):
            note = "\n_Qiymatlar yil boshidan o‘sib boruvchi yakunda (masalan, yanvar–sentabr jami)._"
        elif freq in ("Q", "M"):
            note = f"\n_{FREQ_NAME[freq].capitalize()} ma’lumot._"
        texts.append(f"**{m['name']}** ({unit + ', ' if unit else ''}{period}):\n" + "\n".join("- " + l for l in lines) + note)

        reg_names = list(regions.keys())
        head_t = {"A": "Yil", "S": "O‘quv yili"}.get(freq, "Davr")
        head = f"| {head_t} | " + " | ".join(reg_names) + " |\n|---|" + "---:|" * len(reg_names)
        body = []
        for k in keys:
            cells = []
            for rn in reg_names:
                v = next((r["value"] for r in regions[rn] if _tkey(r) == k), None)
                cells.append(n(float(v)) if v is not None else "—")
            body.append(f"| {L(k, short=True)} | " + " | ".join(cells) + " |")
        tables.append(f"{m['name']}{', ' + unit if unit else ''}\n\n" + head + "\n" + "\n".join(body))
        sources.append({"indicator_id": ind_id, "dataset_code": m["code"], "name": m["name"], "unit": unit,
                        "periodicity": m["periodicity"], "siat_updated": str(m["updated"]),
                        "url": f"https://siat.stat.uz/api/sdmx/{ind_id}/table/"})

    src = "\n".join(f"Manba: SIAT (Milliy statistika qo‘mitasi), dataset kodi {s['dataset_code']} "
                    f"(SIAT id {s['indicator_id']}), SIAT’da yangilangan: {s['siat_updated'][:10]}" for s in sources)
    alts = [x for x in plan.alternatives if x["indicator_id"] not in by_ind]
    if alts:
        src += "\n\nO‘xshash ko‘rsatkichlar (kerak bo‘lsa, nomini yozib so‘rang): " + "; ".join(
            f"{x['name']} ({x['dataset_code']})" for x in alts) + "."
    if plan.note:
        texts.insert(0, "ℹ️ " + plan.note)
    return Answer(ok=True, text="\n\n".join(texts) + "\n\n" + src, table_md="\n\n".join(tables),
                  alternatives=alts, rows=[{k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in r.items()} for r in rows],
                  sql=plan.sql, mode=plan.mode, sources=sources)


def ask(question: str, force_rules: bool = False) -> Answer:
    with psycopg.connect(AGENT_DB_URL, autocommit=True) as conn:
        conn.execute("SET search_path = siat")
        try:
            if LLM_KEY and not force_rules:
                try:
                    plan = plan_llm(question, conn)
                    rows = run_sql(conn, plan)
                    if plan.indicator_ids and rows and "indicator_id" in rows[0]:
                        rows = [r for r in rows if r["indicator_id"] in plan.indicator_ids]
                except NeedClarification:
                    raise
                except Exception as e:
                    log.warning("LLM path failed (%s), falling back to rules", e)
                    plan = plan_rules(question, conn)
                    rows = run_sql(conn, plan)
            else:
                plan = plan_rules(question, conn)
                rows = run_sql(conn, plan)
        except NeedClarification as c:
            return Answer(ok=False, text=str(c), mode="clarify")
        return build_answer(conn, plan, rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rules", action="store_true", help="do not call the LLM")
    ap.add_argument("--sql", action="store_true", help="also print the SQL")
    a = ap.parse_args()

    def show(q: str):
        ans = ask(q, force_rules=a.rules)
        if a.json:
            print(json.dumps(asdict(ans), ensure_ascii=False, indent=1, default=str))
            return
        print(ans.text)
        if ans.table_md:
            print("\n" + ans.table_md)
        if a.sql and ans.sql:
            print(f"\n-- {ans.mode}\n{ans.sql}")

    if a.question:
        show(" ".join(a.question))
        return
    print("SIAT agent. Savolingizni yozing (chiqish: Ctrl+D).")
    for line in sys.stdin:
        if line.strip():
            show(line.strip())
            print("\n" + "-" * 60)


if __name__ == "__main__":
    main()
