"""Turn raw SIAT files (data/raw/<id>/) into clean, long-format tables.

Output in data/normalized/:
  indicators.jsonl    one line per indicator: names (uz/ru/en/uzc), code, unit, periodicity, methodology...
  metadata.jsonl      every metadata field of every indicator (contact persons are left out)
  regions.jsonl       regions and districts (SOATO code + names); only for classifier 1 = Hududlar
  observations.csv    indicator_id, region_code, period, year, quarter, month, value

Period keys in SIAT tables:  "2020" (annual), "2020-Q3" (quarterly), "2020-M09" (monthly),
"2016/2017" (school year, stored with year = 2016).
Quarterly and monthly volumes on SIAT are usually year-to-date totals (2025-Q3 = January–September);
such indicators get cumulative = true, so answers say "yanvar–sentabr", not "III chorak".
District values (7-digit SOATO codes) come from data/raw/<id>/districts.json, written by
`siat_downloader.py --districts` from the official JSON v2 file.

Usage:  python siat_normalizer.py
"""
from __future__ import annotations

import csv
import json
import math
import re

from siat_common import NORM_DIR, RAW_DIR, log

LANGS = ("uz", "ru", "en", "uzc")
# Contact details of individual staff are not needed by the agent; don't copy them.
PERSONAL_KEYS = re.compile(r"phone|email|pochta|fio|fish|fullname|hodim|xodim", re.I)
PERIOD_RE = re.compile(r"^(\d{4})(?:-(Q[1-4]|M\d{2}))?$")
SCHOOL_RE = re.compile(r"^(\d{4})\s*/\s*(\d{4})$")
CUMULATIVE_NOTE = re.compile(r"o.sib\s+boruvchi|нарастающ|ўсиб\s+борувчи|cumulative|year.to.date", re.I)
# Latin letters that look like Cyrillic ones: SIAT sometimes types "Aндижон" with a Latin A
_LAT2CYR = str.maketrans("AaBEeKMHOoPpCcTXxy", "АаВЕеКМНОоРрСсТХху")


def clean_unit(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip().replace("`", "‘").replace("'", "‘")
    u = re.sub(r"\s+", " ", u)
    return u[0].lower() + u[1:] if u[:1].isupper() and not u[:2].isupper() else u


def parse_period(key: str) -> dict | None:
    key = key.strip()
    m = SCHOOL_RE.match(key)
    if m and int(m.group(2)) == int(m.group(1)) + 1:
        y = int(m.group(1))
        return {"period": f"{y}/{y + 1}", "freq": "S", "year": y, "quarter": None, "month": None}
    m = PERIOD_RE.match(key)
    if not m:
        return None
    year, sub = int(m.group(1)), m.group(2)
    if sub is None:
        return {"period": str(year), "freq": "A", "year": year, "quarter": None, "month": None}
    if sub.startswith("Q"):
        return {"period": f"{year}-{sub}", "freq": "Q", "year": year, "quarter": int(sub[1]), "month": None}
    return {"period": f"{year}-{sub[1:]}", "freq": "M", "year": year, "quarter": None, "month": int(sub[1:])}


def to_number(v):
    """SIAT values are numbers today, but guard against text like '-', '…', '1 234,5'."""
    if v is None:
        return None, "missing"
    if isinstance(v, (int, float)):
        return (None, "nan") if isinstance(v, float) and math.isnan(v) else (float(v), None)
    s = str(v).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if s in ("", "-", "–", "—", "…", "...", "x", "X"):
        return None, f"special:{v}"
    try:
        return float(s), None
    except ValueError:
        return None, f"text:{v}"


def table_cells(table: list[dict]):
    """(row, parsed period, raw value) for every usable cell of a /table/ response.

    A period column that is 0.0 in every region (at least 3 of them) is an empty column SIAT filled
    with zeros (e.g. a school year not published yet), not real zeros: it is left out."""
    cols: dict[str, list] = {}
    cells = []
    for r in table:
        for cell in r.get("data") or []:
            for k, v in cell.items():
                p = parse_period(k)
                if p:
                    cells.append((r, p, v))
                    cols.setdefault(p["period"], []).append(v)
    empty = {per for per, vs in cols.items() if len(vs) >= 3 and all(v in (0, 0.0, "0") for v in vs)}
    return [c for c in cells if c[1]["period"] not in empty]


def fix_cyrillic(name: str) -> str:
    """Replace Latin look-alike letters inside Cyrillic words ("Aндижон" -> "Андижон")."""
    return " ".join(w.translate(_LAT2CYR) if re.search(r"[а-яёўқғҳ]", w, re.I) else w
                    for w in re.sub(r"\s+", " ", name or "").strip().split(" "))


def cyr_to_latin(name: str) -> str:
    """Uzbek Cyrillic district name -> Latin: "Нукус шаҳри" -> "Nukus shahri", "Янгиер" -> "Yangiyer"."""
    from siat_dictionary import _CYR2LAT
    t = fix_cyrillic(name).lower()
    t = re.sub(r"(^|[\s(\-аеёиоуэюяўъь])е", lambda m: m.group(1) + "ye", t)  # е = ye after a vowel
    t = t.translate(_CYR2LAT).replace("ʼ", "'").replace("’", "'")
    t = re.sub(r"(^|[\s(\-])([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
    return re.sub(r"\b(Tumani|Shahri|Tuman|Shahar)\b", lambda m: m.group(1).lower(), t)


def district_cells(d) -> tuple[list[dict], list[tuple]]:
    """Districts of one indicator from districts.json: (region rows, (code, period, value) cells)."""
    p = d / "districts.json"
    if not p.exists():
        return [], []
    rows = json.loads(p.read_text(encoding="utf-8")).get("rows") or []
    regs, cells, cols = [], [], {}
    for r in rows:
        code = str(r.get("klass_id") or "").strip()
        if not re.fullmatch(r"\d{7}", code):
            continue
        uzc = fix_cyrillic(r.get("klassifikator") or "")
        regs.append({"code": code, "siat_id": None, "is_root": False, "map_code": None, "level": "district",
                     "parent_code": code[:4], "name_uz": cyr_to_latin(uzc), "name_ru": uzc, "name_en": None,
                     "name_uzc": uzc})
        for v in r.get("values") or []:
            per = parse_period(str(v.get("year") or ""))
            if per:
                cells.append((code, per, v.get("value")))
                cols.setdefault(per["period"], []).append(v.get("value"))
    empty = {per for per, vs in cols.items() if len(vs) >= 3 and all(v in (0, 0.0, "0") for v in vs)}
    return regs, [c for c in cells if c[1]["period"] not in empty]


def is_cumulative(table: list[dict], notes: str) -> bool:
    """Year-to-date series: said so in the metadata, or the republic's values grow through every
    complete year and restart in January (2025-Q4 = whole year, 2026-Q1 small again)."""
    if CUMULATIVE_NOTE.search(notes or ""):
        return True
    root = next((r for r in table if r.get("is_root")), None) or (table[0] if table else None)
    if not root:
        return False
    series: dict[int, dict[int, float]] = {}
    for cell in root.get("data") or []:
        for k, v in cell.items():
            p = parse_period(k)
            if p and p["freq"] in "QM" and isinstance(v, (int, float)) and v > 0:
                series.setdefault(p["year"], {})[p["quarter"] or p["month"]] = float(v)
    n_sub = {4: 4, 12: 12}
    votes = []
    for vals in series.values():
        n = max(vals)
        if n not in n_sub or len(vals) != n:
            continue  # only complete years tell the pattern
        seq = [vals[i] for i in range(1, n + 1)]
        rising = all(b >= a for a, b in zip(seq, seq[1:]))
        votes.append(rising and seq[-1] > (2.5 if n == 4 else 6) * seq[0])
    return bool(votes) and sum(votes) > len(votes) / 2


def meta_key(m: dict) -> str:
    if m.get("key_name"):
        return m["key_name"]
    base = (m.get("name_en") or m.get("name_uz") or "field").lower()
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_")


def main() -> None:
    NORM_DIR.mkdir(parents=True, exist_ok=True)
    indicators, metadata, regions = [], [], {}
    problems = n_obs = 0
    cols = ["indicator_id", "region_code", "period", "freq", "year", "quarter", "month",
            "value", "value_raw", "flag"]
    # observations are written indicator by indicator: with districts there are over a million
    obs_file = (NORM_DIR / "observations.csv").open("w", encoding="utf-8", newline="")
    writer = csv.DictWriter(obs_file, fieldnames=cols)
    writer.writeheader()
    unknown: dict[int, set] = {}
    for d in sorted(p for p in RAW_DIR.iterdir() if p.is_dir() and p.name.isdigit()):
        ind_id = int(d.name)
        if not (d / "manifest.json").exists():  # download was interrupted: the downloader will redo it
            log.warning("%s: incomplete download (no manifest), skipped", ind_id)
            problems += 1
            continue
        info = json.loads((d / "info.json").read_text(encoding="utf-8"))
        table = json.loads((d / "table.json").read_text(encoding="utf-8"))
        md = json.loads((d / "metadata.json").read_text(encoding="utf-8")) if (d / "metadata.json").exists() else {}
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        meta = {}
        for m in md.get("meta_data", []):
            k = meta_key(m)
            if PERSONAL_KEYS.search(k) or PERSONAL_KEYS.search(m.get("name_en") or ""):
                continue
            row = {"indicator_id": ind_id, "key": k, "position": m.get("position")}
            for lg in LANGS:
                row[f"label_{lg}"] = m.get(f"name_{lg}")
                row[f"value_{lg}"] = m.get(f"value_{lg}")
            metadata.append(row)
            meta[k] = m
        # SIAT uses different keys for the same field across indicators
        for canon, alts in {"unit": ("unit_of_measurement",), "methodology": ("calculation_methodology",),
                            "periodicity": (), "note": ()}.items():
            if not meta.get(canon):
                meta[canon] = next((meta[a] for a in alts if a in meta), None) or next(
                    (m for kk, m in meta.items() if m and (canon in kk or canon in (m.get("name_en") or "").lower())), None)
        classifier = info.get("classifier") or {}
        ind = {
            "id": ind_id, "code": info.get("code") or md.get("code"),
            "parent_id": info.get("parent"), "classifier_id": classifier.get("id"),
            "period": info.get("period"), "updated_at": info.get("updated_xlsx"),
            "fetched_at": manifest.get("fetched_at"),
            "source_url": f"https://siat.stat.uz/api/sdmx/{ind_id}/table/",
        }
        for lg in LANGS:
            ind[f"name_{lg}"] = info.get(f"name_{lg}")
            ind[f"unit_{lg}"] = clean_unit((meta.get("unit") or {}).get(f"value_{lg}"))
            ind[f"methodology_{lg}"] = (meta.get("methodology") or {}).get(f"value_{lg}")
            ind[f"note_{lg}"] = (meta.get("note") or {}).get(f"value_{lg}")
        ind["periodicity_uz"] = (meta.get("periodicity") or {}).get("value_uz")
        notes = " ".join(str(m.get(f"value_{lg}") or "") for m in md.get("meta_data", []) for lg in LANGS)
        ind["cumulative"] = is_cumulative(table, notes) if info.get("period") in ("Choraklik", "Oylik") else False
        indicators.append(ind)

        obs: list[dict] = []
        if classifier.get("id") != 1:
            log.warning("%s: classifier %s is not regions, skipped in MVP", ind_id, classifier.get("name_uz"))
            continue
        for r in table:
            code = str(r.get("code"))
            if code not in regions:
                regions[code] = {"code": code, "siat_id": r.get("id"), "is_root": r.get("is_root"),
                                 "map_code": r.get("map_code"),
                                 "level": "country" if r.get("is_root") else "region",
                                 "parent_code": None if r.get("is_root") else "1700",
                                 **{f"name_{lg}": re.sub(r"\s+", " ", r.get(f"name_{lg}") or "").strip() for lg in LANGS}}
            for cell in r.get("data") or []:
                for k in cell:
                    if not parse_period(k):
                        # Excel leftovers ("Unnamed: 31"), part-year columns in annual tables
                        # ("2026 yanvar-iyun"), duplicated columns ("2020.1")
                        unknown.setdefault(ind_id, set()).add(k)
        for r, p, v in table_cells(table):
            val, flag = to_number(v)
            obs.append({"indicator_id": ind_id, "region_code": str(r.get("code")), **p,
                        "value": val, "value_raw": v if flag else "", "flag": flag or ""})
        dregs, dcells = district_cells(d)
        for reg in dregs:
            regions.setdefault(reg["code"], reg)
        for code, p, v in dcells:
            val, flag = to_number(v)
            obs.append({"indicator_id": ind_id, "region_code": code, **p,
                        "value": val, "value_raw": v if flag else "", "flag": flag or ""})
        # the same region/period twice in one SIAT table would break the database key: keep the last one
        seen = {(o["region_code"], o["period"]): o for o in obs}
        if len(seen) != len(obs):
            log.warning("%s: %d duplicate region/period cells dropped", ind_id, len(obs) - len(seen))
            problems += len(obs) - len(seen)
        writer.writerows(seen.values())
        n_obs += len(seen)

    obs_file.close()
    for ind_id, keys in unknown.items():
        log.info("%s: skipped unknown columns: %s", ind_id, ", ".join(sorted(keys)[:5]))

    def dump(name, rows):
        with (NORM_DIR / name).open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    dump("indicators.jsonl", indicators)
    dump("metadata.jsonl", metadata)
    dump("regions.jsonl", regions.values())
    log.info("normalized: %d indicators (%d year-to-date), %d regions and districts, %d observations, "
             "%d metadata fields, %d problems", len(indicators), sum(i["cumulative"] for i in indicators),
             len(regions), n_obs, len(metadata), problems)


if __name__ == "__main__":
    main()
