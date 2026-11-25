"""Shared settings and HTTP helper for the SIAT AI Agent ETL."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent


def load_env(path: Path = ROOT / ".env") -> None:
    """Minimal .env reader (no extra dependency)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

SIAT_API = os.environ.get("SIAT_API", "https://siat.stat.uz/api").rstrip("/")
DATA_DIR = Path(os.environ.get("SIAT_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
CATALOG_DIR = DATA_DIR / "catalog"
NORM_DIR = DATA_DIR / "normalized"
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://siat:siat@localhost:5432/siat")

# MVP: 10 annual indicators with a breakdown by region (classifier 1 = Hududlar).
MVP_INDICATORS = [
    246,   # Doimiy aholi soni (jami)
    223,   # Tug'ilganlar soni (jami)
    1299,  # Yalpi ichki (hududiy) mahsulot hajmi (yillik)
    660,   # Aholi jon boshiga YaHM (yillik)
    552,   # Sanoat mahsulotlari ishlab chiqarish hajmi (yillik)
    1326,  # Asosiy kapitalga investitsiyalar hajmi (yillik)
    2704,  # Chakana savdo hajmi (yillik)
    500,   # O'rtacha hisoblangan oylik ish haqi (yillik)
    1875,  # Ishsizlik darajasi
    2360,  # Eksport hajmi (hududlar kesimida, yillik)
]

log = logging.getLogger("siat")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(message)s")

_session = requests.Session()
_session.headers.update({"User-Agent": "siat-ai-agent/0.1 (open-data ETL)",
                         "Accept": "application/json"})


def get_json(path_or_url: str, params: dict | None = None, retries: int = 5,
             timeout: int = 60) -> tuple[object, bytes]:
    """GET JSON with retries. SIAT sometimes resets connections, so back off and retry.

    Returns (parsed_json, raw_bytes)."""
    url = path_or_url if path_or_url.startswith("http") else f"{SIAT_API}/{path_or_url.lstrip('/')}"
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code == 404:
                raise FileNotFoundError(url)
            r.raise_for_status()
            return r.json(), r.content
        except FileNotFoundError:
            raise
        except Exception as e:  # network errors, 5xx, bad JSON
            last = e
            wait = min(2 ** attempt, 30)
            log.warning("GET %s failed (%s), retry %d/%d in %ss", url, e, attempt, retries, wait)
            time.sleep(wait)
    raise RuntimeError(f"GET {url} failed after {retries} attempts: {last}")


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def append_jsonl(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
