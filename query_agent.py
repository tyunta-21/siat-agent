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


def find_regions(q: str) -> list[str]:
    codes = []
    if "toshkent" in q or "ташкент" in q:
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
    if "barcha viloyat" in q or "viloyatlar kesimida" in q or "hududlar kesimida" in q or "по областям" in q:
        return ["*"]
    return codes or ["1700"]


def find_years(q: str) -> tuple[int | None, int | None]:
    ys = [int(y) for y in re.findall(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)", q)]
    if not ys:
        return None, None
    return min(ys), max(ys)


def _alt(h) -> dict:
    return {"indicator_id": h.id, "dataset_code": h.code, "name": h.name, "unit": h.unit}


def choose_indicators(question: str, conn) -> tuple[list[int], list[dict]]:
    """Synonym dictionary first (tested MVP wording), then the catalog-wide search.

    A specific catalog match beats a generic synonym: "qishloq aholisi soni" is rural population,
    not total population, although "aholi soni" is a synonym of the latter."""
    q = norm(question)
    syn = find_indicators(q)
    regions = find_regions(q)
    hits = get_index(conn).search(question, top=8, regions=regions)
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
        raise NeedClarification("SIAT’ning yillik hududiy ma’lumotlarida bunday ko‘rsatkich topilmadi. "
                                "Ko‘rsatkich nomini boshqacha yozib ko‘ring (masalan: aholi soni, ish haqi, "
                                "sanoat mahsulotlari, eksport).")
    best = top.score if top else 0
    alts = [_alt(h) for h in hits if h.id not in chosen and h.score >= 0.6 * best][:3]
    return chosen, alts


def template_sql(inds: list[int], regions: list[str], y1: int | None, y2: int | None) -> tuple[str, dict]:
    sql = ["SELECT indicator_id, dataset_code, indicator, unit, region_code, region, year, value",
           "FROM v_obs WHERE freq = 'A' AND indicator_id = ANY(%(inds)s)"]
    params: dict = {"inds": inds}
    if regions and regions != ["*"]:
        sql.append("AND region_code = ANY(%(regions)s)")
        params["regions"] = regions
    if y1:
        sql.append("AND year BETWEEN %(y1)s AND %(y2)s")
        params.update(y1=y1, y2=y2 or y1)
    sql.append("ORDER BY indicator_id, region_code, year")
    return "\n".join(sql), params


def plan_rules(question: str, conn) -> Plan:
    q = norm(question)
    inds, alts = choose_indicators(question, conn)
    regions = find_regions(q)
    y1, y2 = find_years(q)
    sql, params = template_sql(inds, regions, y1, y2)
    return Plan(sql=sql, params=params, mode="rules", indicator_ids=inds,
                region_codes=regions, year_from=y1, year_to=y2, alternatives=alts)


# ----------------------------------------------------------------------------- LLM planning
SYSTEM_PROMPT = """Sen O‘zbekiston rasmiy statistikasi (SIAT) bo‘yicha SQL yozuvchi yordamchisan.
Foydalanuvchi savoliga javob berish uchun PostgreSQL'da BITTA SELECT so‘rovini yoz.
Faqat `v_obs` view'idan foydalan. Ustunlar:
  indicator_id int, dataset_code text, indicator text, unit text,
  region_code text, region text, region_level text, freq char ('A' yillik),
  year int, quarter int, month int, value numeric
Qoidalar:
- Har doim indicator_id, dataset_code, indicator, unit, region_code, region, year, value ustunlarini qaytar
  (agar agregat kerak bo‘lmasa). ORDER BY indicator_id, region_code, year.
- Hozircha faqat yillik ma'lumot bor: freq = 'A'.
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
        regions = find_regions(norm(question))
    except NeedClarification:
        regions = None
    hits = get_index(conn).search(question, top=LLM_CANDIDATES, regions=regions)
    n_regions = {h.id: h.regions for h in hits}
    ids = [h.id for h in hits]
    ids += [i for i in find_indicators(norm(question)) if i not in ids]
    with conn.cursor() as cur:
        cur.execute("""SELECT i.id, i.code, i.name_uz, i.unit_uz, i.synonyms, d.path_uz
                       FROM indicators i LEFT JOIN datasets d ON d.id = i.id WHERE i.id = ANY(%s)""", (ids,))
        info = {r[0]: r for r in cur.fetchall()}
        cur.execute("SELECT code, name_uz FROM regions ORDER BY code")
        regs = cur.fetchall()
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
        raise NeedClarification(d["clarify"])
    chosen = [int(x) for x in d.get("indicator_ids") or [] if str(x).isdigit()]
    regions = [str(x) for x in d.get("region_codes") or []]
    y1 = d.get("year_from") if isinstance(d.get("year_from"), int) else None
    y2 = d.get("year_to") if isinstance(d.get("year_to"), int) else None
    final = check_llm_choice(question, conn, chosen, hits)
    best = hits[0].score if hits else 0
    alts = [_alt(h) for h in hits if h.id not in final and h.score >= 0.6 * best][:3]
    mode = f"llm:{AI_PROVIDER}:{LLM_MODEL}"
    if set(final) != set(chosen):
        # The model's pick was trimmed by the guard: run the standard query for the kept indicator(s).
        sql, params = template_sql(final, regions or ["1700"], y1, y2)
        return Plan(sql=sql, params=params, mode=mode + "+guard", indicator_ids=final, region_codes=regions,
                    year_from=y1, year_to=y2, alternatives=alts)
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
    if len(valid) == 1:
        return valid
    try:
        rule_pick, _ = choose_indicators(question, conn)
    except NeedClarification:
        rule_pick = []
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
def build_answer(conn, plan: Plan, rows: list[dict]) -> Answer:
    if not rows:
        text = ("Bazada bu savol uchun ma’lumot topilmadi (ko‘rsatkich, hudud yoki yillar bo‘yicha). "
                "Raqamni taxmin qilmayman.")
        if plan.indicator_ids:  # say what the indicator does have, so the user can re-ask
            with conn.cursor() as cur:
                cur.execute("""SELECT indicator, dataset_code, min(year), max(year),
                                      string_agg(DISTINCT region, ', ' ORDER BY region)
                               FROM v_obs WHERE indicator_id = ANY(%s) AND freq = 'A'
                               GROUP BY indicator, dataset_code""", (plan.indicator_ids,))
                for name, code, y1, y2, regs in cur.fetchall():
                    regs = regs if len(regs) < 300 else "barcha hududlar"
                    text += f"\n\n**{name}** ({code}) bo‘yicha bor ma’lumot: {y1}–{y2}-yillar; {regs}."
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
        cur.execute("SELECT id, code, name_uz, unit_uz, periodicity_uz, updated_at FROM siat.indicators "
                    "WHERE id = ANY(%s)", (sorted({r["indicator_id"] for r in rows}),))
        meta = {i: dict(code=c, name=n, unit=u, periodicity=p, updated=up) for i, c, n, u, p, up in cur.fetchall()}

    texts, tables, sources = [], [], []
    by_ind: dict[int, dict[str, list[dict]]] = {}
    for r in rows:
        by_ind.setdefault(r["indicator_id"], {}).setdefault(r["region"], []).append(r)

    for ind_id, regions in by_ind.items():
        m = meta[ind_id]
        unit = m["unit"] or ""
        is_rate = "foiz" in unit.lower() or unit.strip() == "%"
        years = sorted({r["year"] for rs in regions.values() for r in rs})
        y1, y2 = years[0], years[-1]
        dg = decimals_of(r["value"] for rs in regions.values() for r in rs)
        n = lambda v: fmt_num(v, dg)  # noqa: E731
        period = f"{y1}–{y2}" if y1 != y2 else f"{y1}"
        lines = []
        for region, rs in regions.items():
            vals = {r["year"]: float(r["value"]) if r["value"] is not None else None for r in rs}
            first = next((vals[y] for y in years if vals.get(y) is not None), None)
            last = next((vals[y] for y in reversed(years) if vals.get(y) is not None), None)
            fy = next((y for y in years if vals.get(y) is not None), None)
            ly = next((y for y in reversed(years) if vals.get(y) is not None), None)
            if first is None or last is None:
                lines.append(f"{region}: ma’lumot yo‘q.")
                continue
            if fy == ly:
                lines.append(f"{region}da {fy}-yilda: {n(last)} ({unit}).")
                continue
            verb = "oshgan" if last > first else ("kamaygan" if last < first else "o‘zgarmagan")
            if is_rate:
                diff = last - first
                lines.append(f"{region}da {fy}-yildagi {n(first)}% dan {ly}-yilda {n(last)}% ga {verb} "
                             f"({'+' if diff > 0 else ''}{fmt_num(diff, max(dg, 1))} foiz punkt).")
            else:
                pct = (last - first) / first * 100 if first else None
                pct_s = f", ya’ni {fmt_pct(pct)}" if pct is not None else ""
                lines.append(f"{region}da {fy}-yildagi {n(first)} dan {ly}-yilda {n(last)} ga {verb}{pct_s} "
                             f"({unit}).")
        texts.append(f"**{m['name']}** ({unit}, {period}):\n" + "\n".join("- " + l for l in lines))

        reg_names = list(regions.keys())
        head = "| Yil | " + " | ".join(reg_names) + " |\n|---|" + "---:|" * len(reg_names)
        body = []
        for y in years:
            cells = []
            for rn in reg_names:
                v = next((r["value"] for r in regions[rn] if r["year"] == y), None)
                cells.append(n(float(v)) if v is not None else "—")
            body.append(f"| {y} | " + " | ".join(cells) + " |")
        tables.append(f"{m['name']}, {unit}\n\n" + head + "\n" + "\n".join(body))
        sources.append({"indicator_id": ind_id, "dataset_code": m["code"], "name": m["name"], "unit": unit,
                        "periodicity": m["periodicity"], "siat_updated": str(m["updated"]),
                        "url": f"https://siat.stat.uz/api/sdmx/{ind_id}/table/"})

    src = "\n".join(f"Manba: SIAT (Milliy statistika qo‘mitasi), dataset kodi {s['dataset_code']} "
                    f"(SIAT id {s['indicator_id']}), SIAT’da yangilangan: {s['siat_updated'][:10]}" for s in sources)
    alts = [x for x in plan.alternatives if x["indicator_id"] not in by_ind]
    if alts:
        src += "\n\nO‘xshash ko‘rsatkichlar (kerak bo‘lsa, nomini yozib so‘rang): " + "; ".join(
            f"{x['name']} ({x['dataset_code']})" for x in alts) + "."
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
