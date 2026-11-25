"""Download the full SIAT indicator catalog and save it as JSON + CSV.

Sources:
  /sdmx/json/           whole category tree (period, code, last update)
  /sdmx/last-layer/     flat list of leaf indicators with classifier id

Output: data/catalog/catalog.json, data/catalog/catalog.csv, raw copies in data/raw/catalog/.

Usage:  python siat_catalog.py
"""
from __future__ import annotations

import csv
import json

from siat_common import CATALOG_DIR, RAW_DIR, get_json, log, now_iso, sha256, write_json

LANGS = ("uz", "ru", "en", "uzc")


def fetch_tree() -> list[dict]:
    tree, raw = get_json("/sdmx/json/")
    (RAW_DIR / "catalog").mkdir(parents=True, exist_ok=True)
    (RAW_DIR / "catalog" / "sdmx_json.json").write_bytes(raw)
    log.info("catalog tree: %d top sections, sha256 %s", len(tree), sha256(raw)[:12])
    return tree


def fetch_last_layer() -> dict[int, dict]:
    out: dict[int, dict] = {}
    page = 1
    while True:
        d, _ = get_json("/sdmx/last-layer/", params={"page": page})
        for it in d["results"]:
            out[it["id"]] = it
        if not d.get("next"):
            break
        page += 1
    write_json(RAW_DIR / "catalog" / "last_layer.json", list(out.values()))
    log.info("last-layer: %d indicators over %d pages", len(out), page)
    return out


def flatten(tree: list[dict], leaves_info: dict[int, dict]) -> list[dict]:
    rows: list[dict] = []

    def walk(node: dict, path: list[dict]):
        children = node.get("children") or []
        if children:
            for c in children:
                walk(c, path + [node])
            return
        info = leaves_info.get(node["id"], {})
        row = {
            "id": node["id"],
            "code": node.get("code"),
            "period": node.get("period"),
            "classifier_id": info.get("classifier"),
            "is_active": node.get("is_active"),
            "updated_xlsx": node.get("updated_xlsx"),
            "status": node.get("status"),
            "section_code": path[0].get("code") if path else None,
            "path_uz": " / ".join(p.get("name_uz") or "" for p in path),
            "path_ids": [p["id"] for p in path],
        }
        for lg in LANGS:
            row[f"name_{lg}"] = node.get(f"name_{lg}")
        rows.append(row)

    for top in tree:
        walk(top, [])
    return rows


def main() -> None:
    tree = fetch_tree()
    leaves = fetch_last_layer()
    rows = flatten(tree, leaves)
    write_json(CATALOG_DIR / "catalog.json", {"fetched_at": now_iso(), "count": len(rows), "items": rows})
    cols = ["id", "code", "period", "classifier_id", "is_active", "updated_xlsx", "status",
            "section_code", "path_uz"] + [f"name_{lg}" for lg in LANGS]
    with (CATALOG_DIR / "catalog.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    by_period: dict[str, int] = {}
    for r in rows:
        by_period[r["period"]] = by_period.get(r["period"], 0) + 1
    regional_annual = sum(1 for r in rows if r["classifier_id"] == 1 and r["period"] == "Yillik")
    log.info("catalog: %d indicators %s; annual by region: %d",
             len(rows), json.dumps(by_period, ensure_ascii=False), regional_annual)


if __name__ == "__main__":
    main()
