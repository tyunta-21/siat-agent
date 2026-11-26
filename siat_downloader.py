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
  python siat_downloader.py --all-regional --workers 4   # every annual, quarterly and monthly indicator by region
  python siat_downloader.py --districts --max-minutes 120  # district values from the JSON v2 files

Districts: /table/ has only the 14 regions. The official JSON v2 file (link in download.json,
field file_2, with a random suffix) also has districts and district-level cities (7-digit SOATO
codes) when SIAT publishes them. Only those rows are kept, in data/raw/<id>/districts.json.
The pass is resumable: an indicator is fetched again only after its SIAT update date changed,
and the pass stops after --max-minutes (the nightly update continues the next night).
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


def all_regional(periods=("Yillik", "Choraklik", "Oylik")) -> list[int]:
    cat = json.loads((CATALOG_DIR / "catalog.json").read_text(encoding="utf-8"))
    order = {p: n for n, p in enumerate(periods)}
    rows = [r for r in cat["items"]
            if r["classifier_id"] == 1 and r["period"] in periods and r.get("is_active", True)]
    return [r["id"] for r in sorted(rows, key=lambda r: (order[r["period"]], r["id"]))]


def districts_one(ind_id: int) -> str:
    """Fetch the JSON v2 file of one indicator and keep its district rows. Returns a status."""
    d = RAW_DIR / str(ind_id)
    mf = d / "manifest.json"
    if not mf.exists():
        return "no_manifest"
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    out = d / "districts.json"
    if out.exists():
        old = json.loads(out.read_text(encoding="utf-8"))
        if old.get("updated_xlsx") == manifest.get("updated_xlsx"):
            return "skipped_unchanged"
    link = None
    if (d / "download.json").exists():
        link = (json.loads((d / "download.json").read_text(encoding="utf-8")) or {}).get("file_2")
    body = None
    for attempt in range(2):
        if not link or attempt == 1:  # the stored link may be stale: ask SIAT for the current one
            dl, raw = get_json(ENDPOINTS["download"].format(id=ind_id), retries=4)
            (d / "download.json").write_bytes(raw)
            link = (dl or {}).get("file_2")
            if not link:
                break
        try:
            body, _ = get_json(link, retries=3, timeout=120)
            break
        except FileNotFoundError:
            link = None
    rows = [{"klass_id": str(r.get("klass_id")), "klassifikator": r.get("klassifikator"),
             "values": r.get("values") or []}
            for r in ((body or {}).get("data") or []) if len(str(r.get("klass_id") or "").strip()) == 7]
    out.write_text(json.dumps({"id": ind_id, "updated_xlsx": manifest.get("updated_xlsx"), "fetched_at": now_iso(),
                               "source": link, "rows": rows}, ensure_ascii=False), encoding="utf-8")
    return "ok_districts" if rows else "ok_no_districts"


def districts_pass(ids: list[int], max_minutes: float, pause: float, workers: int) -> dict:
    t_end = time.time() + max_minutes * 60
    counts: dict[str, int] = {}
    todo = [i for i in ids if (RAW_DIR / str(i) / "manifest.json").exists()]
    log.info("districts: checking %d indicators, time budget %.0f min", len(todo), max_minutes)

    def job(i: int) -> str:
        if time.time() > t_end:
            return "postponed"
        try:
            st = districts_one(i)
        except Exception as e:
            log.warning("%s: districts failed: %s", i, str(e)[:120])
            st = "error"
        if st not in ("skipped_unchanged", "no_manifest"):
            time.sleep(pause)
        return st

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for n, st in enumerate(pool.map(job, todo), 1):
            counts[st] = counts.get(st, 0) + 1
            if n % 100 == 0 or n == len(todo):
                log.info("districts progress %d/%d: %s", n, len(todo), counts)
    if counts.get("postponed"):
        log.info("districts: %d indicators left for the next run (time budget)", counts["postponed"])
    log.info("districts done: %s", counts)
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--all-annual-regional", action="store_true", help="only annual indicators by region")
    ap.add_argument("--all-regional", action="store_true", help="annual, quarterly and monthly by region")
    ap.add_argument("--districts", action="store_true", help="district values from the JSON v2 files")
    ap.add_argument("--max-minutes", type=float, default=120, help="time budget of the --districts pass")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds each worker waits between indicators")
    ap.add_argument("--workers", type=int, default=1, help="parallel downloads (4 is polite and fast)")
    a = ap.parse_args()
    if a.districts:
        ids = a.ids or all_regional()
        counts = districts_pass(ids, a.max_minutes, a.pause, a.workers)
        if counts.get("error", 0) > max(10, len(ids) // 10):
            raise SystemExit(1)
        return
    if a.all_regional:
        ids = all_regional()
    elif a.all_annual_regional:
        ids = all_regional(("Yillik",))
    else:
        ids = a.ids or MVP_INDICATORS
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
