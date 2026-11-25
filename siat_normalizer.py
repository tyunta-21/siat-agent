"""Turn raw SIAT files (data/raw/<id>/) into clean, long-format tables.

Output in data/normalized/:
  indicators.jsonl    one line per indicator: names (uz/ru/en/uzc), code, unit, periodicity, methodology...
  metadata.jsonl      every metadata field of every indicator (contact persons are left out)
  regions.jsonl       regions (SOATO code + names); only for classifier 1 = Hududlar
  observations.csv    indicator_id, region_code, period, year, quarter, month, value

Period keys in SIAT tables:  "2020" (annual), "2020-Q3" (quarterly), "2020-M09" (monthly).

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


def clean_unit(u: str | None) -> str | None:
    if not u:
        return None
    u = u.strip().replace("`", "‘").replace("'", "‘")
    u = re.sub(r"\s+", " ", u)
    return u[0].lower() + u[1:] if u[:1].isupper() and not u[:2].isupper() else u


def parse_period(key: str) -> dict | None:
    m = PERIOD_RE.match(key.strip())
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


def meta_key(m: dict) -> str:
    if m.get("key_name"):
        return m["key_name"]
    base = (m.get("name_en") or m.get("name_uz") or "field").lower()
    return re.sub(r"[^a-z0-9]+", "_", base).strip("_")


def main() -> None:
    NORM_DIR.mkdir(parents=True, exist_ok=True)
    indicators, metadata, regions, obs = [], [], {}, []
    problems = 0
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
        indicators.append(ind)

        if classifier.get("id") != 1:
            log.warning("%s: classifier %s is not regions, skipped in MVP", ind_id, classifier.get("name_uz"))
            continue
        for r in table:
            code = str(r.get("code"))
            if code not in regions:
                regions[code] = {"code": code, "siat_id": r.get("id"), "is_root": r.get("is_root"),
                                 "map_code": r.get("map_code"),
                                 "level": "country" if r.get("is_root") else "region",
                                 **{f"name_{lg}": re.sub(r"\s+", " ", r.get(f"name_{lg}") or "").strip() for lg in LANGS}}
            for cell in r.get("data") or []:
                for k, v in cell.items():
                    p = parse_period(k)
                    if not p:
                        # Excel leftovers ("Unnamed: 31"), part-year columns in annual tables
                        # ("2026 yanvar-iyun"), duplicated columns ("2020.1"): not annual values
                        unknown.setdefault(ind_id, set()).add(k)
                        continue
                    val, flag = to_number(v)
                    obs.append({"indicator_id": ind_id, "region_code": code, **p,
                                "value": val, "value_raw": v if flag else "", "flag": flag or ""})

    for ind_id, keys in unknown.items():
        log.info("%s: skipped non-annual/unknown columns: %s", ind_id, ", ".join(sorted(keys)[:5]))
    # the same region/period twice in one SIAT table would break the database key: keep the last one
    seen: dict[tuple, int] = {}
    for n, o in enumerate(obs):
        seen[(o["indicator_id"], o["region_code"], o["period"])] = n
    if len(seen) != len(obs):
        log.warning("%d duplicate region/period cells dropped", len(obs) - len(seen))
        problems += len(obs) - len(seen)
        keep = set(seen.values())
        obs = [o for n, o in enumerate(obs) if n in keep]

    def dump(name, rows):
        with (NORM_DIR / name).open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    dump("indicators.jsonl", indicators)
    dump("metadata.jsonl", metadata)
    dump("regions.jsonl", regions.values())
    cols = ["indicator_id", "region_code", "period", "freq", "year", "quarter", "month",
            "value", "value_raw", "flag"]
    with (NORM_DIR / "observations.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(obs)
    log.info("normalized: %d indicators, %d regions, %d observations, %d metadata fields, %d problems",
             len(indicators), len(regions), len(obs), len(metadata), problems)


if __name__ == "__main__":
    main()
