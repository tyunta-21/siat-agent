"""Download raw SIAT data for a set of indicators, unchanged, with a load log.

For each indicator id it saves into data/raw/<id>/:
  info.json      /sdmx/<id>/            (name, period, parent, classifier, updated date)
  table.json     /sdmx/<id>/table/      (values by region/classifier item and period)
  metadata.json  /sdmx/<id>/metadata/   (unit, methodology, source, notes)
  download.json  /sdmx/<id>/table/download/ (links to the official XML / JSON v2 files)
and appends one line per indicator to data/raw/download_log.jsonl.

Re-runs skip indicators whose SIAT `updated_xlsx` has not changed (use --force to refetch).

Usage:
  python siat_downloader.py                 # the 10 MVP indicators
  python siat_downloader.py 246 552         # specific ids
  python siat_downloader.py --all-annual-regional --workers 4   # every annual indicator by region (needs catalog)
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from siat_common import (CATALOG_DIR, MVP_INDICATORS, RAW_DIR, append_jsonl, get_json, log,
                         now_iso, sha256)

ENDPOINTS = {
    "info": "/sdmx/{id}/",
    "table": "/sdmx/{id}/table/",
    "metadata": "/sdmx/{id}/metadata/",
    "download": "/sdmx/{id}/table/download/",
}


def download_one(ind_id: int, force: bool = False) -> dict:
    out_dir = RAW_DIR / str(ind_id)
    manifest_path = out_dir / "manifest.json"
    t0 = time.time()
    info, info_raw = get_json(ENDPOINTS["info"].format(id=ind_id))
    if manifest_path.exists() and not force:
        old = json.loads(manifest_path.read_text(encoding="utf-8"))
        if old.get("updated_xlsx") == info.get("updated_xlsx"):
            log.info("%s: unchanged since %s, skip", ind_id, info.get("updated_xlsx"))
            return {"id": ind_id, "status": "skipped_unchanged", "at": now_iso()}

    out_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, tpl in ENDPOINTS.items():
        if name == "info":
            raw = info_raw
        else:
            try:
                # download.json only holds links to the official files and SIAT rate-limits that
                # endpoint (429), so it gets fewer retries and may be skipped
                _, raw = get_json(tpl.format(id=ind_id), retries=5 if name != "download" else 2)
            except FileNotFoundError:
                log.warning("%s: %s not found", ind_id, name)
                continue
            except Exception as e:
                if name != "download":
                    raise
                log.warning("%s: download links skipped (%s)", ind_id, str(e)[:80])
                continue
        (out_dir / f"{name}.json").write_bytes(raw)
        files[name] = {"endpoint": tpl.format(id=ind_id), "sha256": sha256(raw), "bytes": len(raw)}

    table = json.loads((out_dir / "table.json").read_text(encoding="utf-8"))
    n_rows = len(table) if isinstance(table, list) else 0
    n_values = sum(len(r.get("data") or []) for r in table) if isinstance(table, list) else 0
    manifest = {
        "id": ind_id, "code": info.get("code"), "name_uz": info.get("name_uz"),
        "period": info.get("period"), "updated_xlsx": info.get("updated_xlsx"),
        "fetched_at": now_iso(), "files": files, "rows": n_rows, "values": n_values,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    entry = {"id": ind_id, "status": "ok", "at": manifest["fetched_at"], "rows": n_rows,
             "values": n_values, "seconds": round(time.time() - t0, 1)}
    log.info("%s %s: %d rows, %d values", ind_id, info.get("code"), n_rows, n_values)
    return entry


def all_annual_regional() -> list[int]:
    cat = json.loads((CATALOG_DIR / "catalog.json").read_text(encoding="utf-8"))
    return [r["id"] for r in cat["items"]
            if r["classifier_id"] == 1 and r["period"] == "Yillik" and r.get("is_active", True)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--all-annual-regional", action="store_true")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds each worker waits between indicators")
    ap.add_argument("--workers", type=int, default=1, help="parallel downloads (4 is polite and fast)")
    a = ap.parse_args()
    ids = a.ids or (all_annual_regional() if a.all_annual_regional else MVP_INDICATORS)
    log.info("downloading %d indicators with %d worker(s)", len(ids), a.workers)
    lock = threading.Lock()
    counts = {"ok": 0, "skipped_unchanged": 0, "error": 0}

    def job(i: int) -> dict:
        try:
            entry = download_one(i, force=a.force)
        except Exception as e:  # keep going, log the failure
            entry = {"id": i, "status": "error", "at": now_iso(), "error": str(e)[:500]}
            log.error("%s failed: %s", i, e)
        time.sleep(a.pause)  # be polite to the SIAT server
        return entry

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        futures = [pool.submit(job, i) for i in ids]
        for n, f in enumerate(as_completed(futures), 1):
            entry = f.result()
            with lock:
                counts[entry["status"]] = counts.get(entry["status"], 0) + 1
                append_jsonl(RAW_DIR / "download_log.jsonl", entry)
            if n % 50 == 0 or n == len(ids):
                log.info("progress %d/%d: %s", n, len(ids), counts)
    log.info("done: %s", counts)
    if counts["error"]:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
