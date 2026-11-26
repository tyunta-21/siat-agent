"""Data-quality checks after loading: is what is in PostgreSQL complete and sane?

Checks (FAIL stops a release, WARN is for a human to look at):
  completeness   every downloaded indicator is in the DB; DB values = values in the raw SIAT files
  regions        only the 15 known regions (republic, Qoraqalpog‘iston, 12 viloyat, Toshkent shahri),
                 districts have 7-digit codes inside a known region
  Toshkent       1726 (shahar) and 1727 (viloyat) both present and never mixed up in names
  years          annual years between 1990 and next year
  units          every indicator has a unit
  freshness      every indicator has the SIAT update date
  numbers        share of non-numeric values ("-", "...") per indicator
  sum check      for counts and volumes, republic total ≈ sum of regions (info: shows the data is
                 consistent; rates, averages and indices are skipped)

Writes data/quality_report.md and exits with code 1 if any FAIL.

Usage:  python check_data.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import psycopg

from siat_common import DATA_DIR, DATABASE_URL, RAW_DIR, log
from siat_normalizer import district_cells, parse_period, table_cells

KNOWN_REGIONS = {"1700", "1735", "1703", "1706", "1708", "1710", "1712", "1714", "1718", "1722", "1724",
                 "1727", "1730", "1733", "1726"}
# Units where the republic total should equal the sum of regions
ADDITIVE_UNIT = ("ming kishi", "kishi", "nafar", "mlrd", "mln", "ming so‘m", "dona", "birlik", "tonna",
                 "ming tonna", "gektar", "ming gektar", "bosh", "ming bosh", "km", "ming dollar", "mln dollar")
NON_ADDITIVE = ("foiz", "%", "o‘rtacha", "koeffitsient", "indeks", "jon boshiga", "1000", "10000", "100 000",
                "nisbat", "daraja", "ulush", "sur’at", "o‘sish", "zichlik")


def main() -> int:
    out: list[str] = []
    fails = warns = 0

    def res(level: str, name: str, msg: str):
        nonlocal fails, warns
        fails += level == "FAIL"
        warns += level == "WARN"
        out.append(f"| {level} | {name} | {msg} |")

    raw, expected, skipped_cols = {}, {}, {}
    for d in RAW_DIR.iterdir():
        m = d / "manifest.json"
        if d.is_dir() and m.exists():
            i = int(d.name)
            raw[i] = json.loads(m.read_text(encoding="utf-8"))
            table = json.loads((d / "table.json").read_text(encoding="utf-8"))
            keys = [k for r in table for c in (r.get("data") or []) for k in c]
            # SIAT sometimes repeats a year ("2010" and "2010\n"): the database keeps one
            cells = {(str(r.get("code")), p["period"]) for r, p, _ in table_cells(table)}
            cells |= {(code, p["period"]) for code, p, _ in district_cells(d)[1]}
            expected[i] = len(cells)
            bad = {k.strip() for k in keys if not parse_period(k)}
            if bad:
                skipped_cols[i] = sorted(bad)

    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("SET search_path = siat")
        cur.execute("SELECT indicator_id, count(*), count(value) FROM observations GROUP BY 1")
        db = {i: (n, nv) for i, n, nv in cur.fetchall()}
        cur.execute("SELECT count(*) FROM indicators")
        n_ind = cur.fetchone()[0]

        # completeness
        missing = [i for i in raw if expected[i] and i not in db]
        empty = [i for i, m in raw.items() if not m.get("values")]
        no_annual = [i for i in raw if raw[i].get("values") and not expected[i]]  # no usable period
        mismatch = [(i, expected[i], db.get(i, (0,))[0]) for i in raw if db.get(i, (0,))[0] != expected[i]]
        res("FAIL" if missing else "OK", "Indikatorlar bazada",
            f"{len(db)} ta indikatorda qiymat bor (yuklangan: {len(raw)}, bazadagi ro‘yxat: {n_ind})"
            + (f"; bazada yo‘q: {missing[:10]}" if missing else ""))
        res("WARN" if empty else "OK", "SIAT’da bo‘sh jadval",
            f"{len(empty)} ta indikatorda SIAT jadvali bo‘sh" + (f": {empty[:15]}" if empty else ""))
        res("FAIL" if mismatch else "OK", "Qiymatlar soni = SIAT fayllari",
            "hammasi mos" if not mismatch else f"{len(mismatch)} ta mos emas, masalan {mismatch[:5]}")
        res("WARN" if no_annual else "OK", "Davr formati noma’lum",
            f"{len(no_annual)} ta indikatorda davrlarni o‘qib bo‘lmadi: {no_annual[:10]}" if no_annual else "yo‘q")
        cur.execute("SELECT freq, count(DISTINCT indicator_id), count(*) FROM v_obs GROUP BY 1 ORDER BY 1")
        names = {"A": "yillik", "Q": "choraklik", "M": "oylik", "S": "o‘quv yili", "D": "davr (mavsum)"}
        res("OK", "Davriylik", "; ".join(f"{names.get(f, f)}: {n} ta indikator, {v:,} qiymat"
                                         for f, n, v in cur.fetchall()))
        other = {i: c for i, c in skipped_cols.items() if i not in no_annual}
        res("OK", "Tashlab ketilgan ustunlar",
            f"{len(other)} ta indikatorda yillik bo‘lmagan ustunlar (Excel qoldig‘i “Unnamed”, “2026 yanvar-iyun”) "
            "olinmadi" + (f", masalan {list(other.items())[:2]}" if other else ""))

        # regions
        cur.execute("SELECT DISTINCT region_code FROM observations")
        codes = {r[0] for r in cur.fetchall()}
        districts = {c for c in codes if len(c) == 7 and c[:4] in KNOWN_REGIONS}
        extra = sorted(codes - KNOWN_REGIONS - districts)
        res("WARN" if extra else "OK", "Hududlar", f"{len(codes & KNOWN_REGIONS)} ta ma’lum hudud"
            + (f"; noma’lum kodlar: {extra}" if extra else ""))
        cur.execute("SELECT count(DISTINCT indicator_id) FROM v_obs WHERE region_level = 'district'")
        res("OK", "Tumanlar va shaharlar", f"{len(districts)} ta tuman/shahar, "
            f"{cur.fetchone()[0]} ta indikatorda tuman ma’lumoti bor")
        cur.execute("SELECT code, name_uz FROM regions WHERE code IN ('1726','1727') ORDER BY code")
        tosh = dict(cur.fetchall())
        ok = "shahr" in (tosh.get("1726") or "").lower() and "viloyat" in (tosh.get("1727") or "").lower()
        res("OK" if ok else "FAIL", "Toshkent shahri / viloyati", f"1726 = {tosh.get('1726')}, 1727 = {tosh.get('1727')}")

        # years
        cur.execute("SELECT min(year), max(year) FROM v_obs WHERE freq = 'A'")
        y1, y2 = cur.fetchone()
        bad = y1 is None or y1 < 1990 or y2 > dt.date.today().year + 1
        res("FAIL" if bad else "OK", "Yillar", f"{y1}–{y2}")

        # units, freshness
        cur.execute("SELECT id FROM indicators WHERE coalesce(unit_uz, '') = '' AND id = ANY(%s)", (list(db),))
        no_unit = [r[0] for r in cur.fetchall()]
        res("WARN" if no_unit else "OK", "O‘lchov birligi",
            f"{len(db) - len(no_unit)}/{len(db)} ta indikatorda bor" + (f"; yo‘q: {no_unit[:15]}" if no_unit else ""))
        cur.execute("SELECT count(*) FROM indicators WHERE updated_at IS NULL AND id = ANY(%s)", (list(db),))
        no_upd = cur.fetchone()[0]
        res("WARN" if no_upd else "OK", "SIAT yangilangan sana", f"{no_upd} ta indikatorda sana yo‘q")

        # non-numeric values
        cur.execute("SELECT count(*), count(*) FILTER (WHERE value IS NULL) FROM observations")
        total, nulls = cur.fetchone()
        share = nulls / total * 100 if total else 0
        res("WARN" if share > 5 else "OK", "Raqam bo‘lmagan qiymatlar",
            f"{nulls:,} / {total:,} ({share:.1f}%) — bular javobda “ma’lumot yo‘q” deb ko‘rsatiladi")

        # republic = sum of regions for additive indicators (latest common year)
        cur.execute("""
            WITH r AS (
              SELECT o.indicator_id, i.unit_uz || ' ' || i.name_uz AS unit_uz, o.year,
                     max(o.value) FILTER (WHERE o.region_code = '1700') AS rep,
                     sum(o.value) FILTER (WHERE o.region_code <> '1700' AND o.region_level = 'region') AS reg,
                     count(o.value) FILTER (WHERE o.region_code <> '1700' AND o.region_level = 'region') AS n
              FROM v_obs o JOIN indicators i ON i.id = o.indicator_id
              WHERE o.freq = 'A' GROUP BY 1, 2, 3)
            SELECT DISTINCT ON (indicator_id) indicator_id, unit_uz, year, rep, reg
            FROM r WHERE n = 14 AND rep IS NOT NULL AND rep <> 0
            ORDER BY indicator_id, year DESC""")
        tested = good = 0
        worst = []
        for i, unit, year, rep, reg in cur.fetchall():
            u = (unit or "").lower()
            if not any(a in u for a in ADDITIVE_UNIT) or any(x in u for x in NON_ADDITIVE):
                continue
            tested += 1
            dev = abs(float(reg) - float(rep)) / abs(float(rep)) * 100
            if dev <= 2:
                good += 1
            else:
                worst.append((round(dev, 1), i, year))
        worst.sort(reverse=True)
        res("OK" if tested and good / tested >= 0.8 else "WARN", "Respublika = hududlar yig‘indisi",
            f"{good}/{tested} ta hajm ko‘rsatkichida farq ≤ 2%"
            + (f"; eng katta farqlar (%, id, yil): {worst[:5]}" if worst else "")
            + " (farq bo‘lishi mumkin: taqsimlanmagan qism, yaxlitlash)")

    report = ["# SIAT ma’lumotlari sifati", "", f"Tekshirildi: {dt.datetime.now():%Y-%m-%d %H:%M}", "",
              "| Natija | Tekshiruv | Tafsilot |", "|---|---|---|", *out, "",
              f"FAIL: {fails}, WARN: {warns}"]
    (DATA_DIR / "quality_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    log.info("quality report: %s", DATA_DIR / "quality_report.md")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
