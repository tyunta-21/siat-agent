"""Load normalized SIAT data into PostgreSQL (idempotent upserts).

Steps: apply schema.sql -> raw_json -> datasets (catalog) -> indicators -> regions -> periods
       -> observations -> metadata, with a row in load_log for each step.

Usage:  python load_data.py            (uses DATABASE_URL from .env)
"""
from __future__ import annotations

import csv
import json
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from siat_common import CATALOG_DIR, DATABASE_URL, NORM_DIR, RAW_DIR, ROOT, log, sha256
from siat_dictionary import INDICATOR_SYNONYMS, REGION_ALIASES, district_aliases

LANGS = ("uz", "ru", "en", "uzc")


def jsonl(name: str) -> list[dict]:
    p = NORM_DIR / name
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def step(cur, name: str, fn) -> None:
    cur.execute("INSERT INTO load_log (step, status) VALUES (%s, 'running') RETURNING id", (name,))
    log_id = cur.fetchone()[0]
    try:
        n = fn(cur)
        cur.execute("UPDATE load_log SET finished_at=now(), rows=%s, status='ok' WHERE id=%s", (n, log_id))
        log.info("%-13s %6d rows", name, n)
    except Exception as e:
        raise RuntimeError(f"step {name} failed: {e}") from e


def load_raw(cur) -> int:
    n = 0
    for mf in RAW_DIR.glob("*/manifest.json"):
        m = json.loads(mf.read_text(encoding="utf-8"))
        for name, f in m["files"].items():
            body = (mf.parent / f"{name}.json").read_bytes()
            cur.execute(
                """INSERT INTO raw_json (indicator_id, endpoint, sha256, fetched_at, body)
                   VALUES (%s,%s,%s,%s,%s) ON CONFLICT (endpoint, sha256) DO NOTHING""",
                (m["id"], f["endpoint"], sha256(body), m["fetched_at"], Jsonb(json.loads(body))))
            n += cur.rowcount
    return n


def load_datasets(cur) -> int:
    p = CATALOG_DIR / "catalog.json"
    if not p.exists():
        return 0
    items = json.loads(p.read_text(encoding="utf-8"))["items"]
    rows = [(r["id"], (r["path_ids"] or [None])[-1], r["code"], r["period"], r["classifier_id"], True,
             r["name_uz"], r["name_ru"], r["name_en"], r["name_uzc"], r["path_uz"], r["updated_xlsx"])
            for r in items]
    cur.executemany(
        """INSERT INTO datasets (id, parent_id, code, period, classifier_id, is_leaf,
               name_uz, name_ru, name_en, name_uzc, path_uz, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (id) DO UPDATE SET code=EXCLUDED.code, period=EXCLUDED.period,
               classifier_id=EXCLUDED.classifier_id, name_uz=EXCLUDED.name_uz, name_ru=EXCLUDED.name_ru,
               name_en=EXCLUDED.name_en, name_uzc=EXCLUDED.name_uzc, path_uz=EXCLUDED.path_uz,
               updated_at=EXCLUDED.updated_at""", rows)
    return len(rows)


def load_indicators(cur) -> int:
    rows = jsonl("indicators.jsonl")
    cols = (["id", "code", "parent_id", "classifier_id", "period"]
            + [f"{f}_{lg}" for f in ("name", "unit", "methodology", "note") for lg in LANGS]
            + ["periodicity_uz", "source_url", "updated_at", "fetched_at", "cumulative"])
    sql = (f"INSERT INTO indicators ({', '.join(cols)}, synonyms) VALUES ({', '.join(['%s'] * (len(cols) + 1))}) "
           "ON CONFLICT (id) DO UPDATE SET " + ", ".join(f"{c}=EXCLUDED.{c}" for c in cols[1:] + ["synonyms"]))
    rows = [{**r, "cumulative": bool(r.get("cumulative"))} for r in rows]
    cur.executemany(sql, [[r.get(c) for c in cols] + [INDICATOR_SYNONYMS.get(r["id"], [])] for r in rows])
    return len(rows)


def load_regions(cur) -> int:
    rows = jsonl("regions.jsonl")
    cur.executemany(
        """INSERT INTO regions (code, siat_id, level, parent_code, map_code, name_uz, name_ru, name_en, name_uzc,
                               aliases)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (code) DO UPDATE SET siat_id=EXCLUDED.siat_id, level=EXCLUDED.level,
               parent_code=EXCLUDED.parent_code,
               map_code=EXCLUDED.map_code, name_uz=EXCLUDED.name_uz, name_ru=EXCLUDED.name_ru,
               name_en=EXCLUDED.name_en, name_uzc=EXCLUDED.name_uzc, aliases=EXCLUDED.aliases""",
        [(r["code"], r["siat_id"], r["level"], r.get("parent_code"), r["map_code"], r["name_uz"], r["name_ru"],
          r["name_en"], r["name_uzc"],
          REGION_ALIASES.get(r["code"]) or (district_aliases(r) if r["level"] == "district" else []))
         for r in rows])
    return len(rows)


def read_obs():
    """Stream observations.csv (over a million rows with districts: not kept in memory)."""
    with (NORM_DIR / "observations.csv").open(encoding="utf-8") as f:
        yield from csv.DictReader(f)


def load_periods(cur) -> int:
    seen = {}
    for o in read_obs():
        seen[o["period"]] = (o["period"], o["freq"], int(o["year"]),
                             int(o["quarter"]) if o["quarter"] else None, int(o["month"]) if o["month"] else None)
    cur.executemany("INSERT INTO periods VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", list(seen.values()))
    return len(seen)


def load_observations(cur) -> int:
    n = 0
    cur.execute("CREATE TEMP TABLE obs_in (LIKE observations INCLUDING DEFAULTS) ON COMMIT DROP")
    with cur.copy("COPY obs_in (indicator_id, region_code, period, value, value_raw, flag) FROM STDIN") as cp:
        for o in read_obs():
            n += 1
            cp.write_row((int(o["indicator_id"]), o["region_code"], o["period"],
                          o["value"] or None, o["value_raw"] or None, o["flag"] or None))
    cur.execute("ALTER TABLE obs_in ADD PRIMARY KEY (indicator_id, region_code, period)")
    cur.execute("""INSERT INTO observations (indicator_id, region_code, period, value, value_raw, flag)
                   SELECT indicator_id, region_code, period, value, value_raw, flag FROM obs_in
                   ON CONFLICT (indicator_id, region_code, period) DO UPDATE
                   SET value=EXCLUDED.value, value_raw=EXCLUDED.value_raw, flag=EXCLUDED.flag, loaded_at=now()""")
    # cells SIAT no longer has (or that the normalizer now skips, e.g. all-zero columns) are removed
    cur.execute("""DELETE FROM observations o
                   WHERE o.indicator_id IN (SELECT DISTINCT indicator_id FROM obs_in)
                     AND NOT EXISTS (SELECT 1 FROM obs_in n WHERE n.indicator_id = o.indicator_id
                                     AND n.region_code = o.region_code AND n.period = o.period)""")
    if cur.rowcount:
        log.info("observations: %d old cells removed", cur.rowcount)
    return n


def load_metadata(cur) -> int:
    rows = jsonl("metadata.jsonl")
    cols = ["indicator_id", "key", "position"] + [f"{f}_{lg}" for f in ("label", "value") for lg in LANGS]
    cur.executemany(
        f"INSERT INTO metadata ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
        "ON CONFLICT (indicator_id, key) DO UPDATE SET " + ", ".join(f"{c}=EXCLUDED.{c}" for c in cols[2:]),
        [[r.get(c) for c in cols] for r in rows])
    return len(rows)


def main() -> None:
    t0 = datetime.now()
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))
        cur.execute("SET search_path = siat, public")
        for name, fn in [("raw_json", load_raw), ("datasets", load_datasets), ("indicators", load_indicators),
                         ("regions", load_regions), ("periods", load_periods),
                         ("observations", load_observations), ("metadata", load_metadata)]:
            step(cur, name, fn)
    log.info("loaded in %.1fs", (datetime.now() - t0).total_seconds())


if __name__ == "__main__":
    main()
