"""Check the agent against expected answers AND against the live SIAT table.

For each question: right indicator, right regions, right years, and every value in the answer
must equal the value on siat.stat.uz/api/sdmx/<id>/table/ today. Questions marked "clarify"
must not get numbers.

Usage:  python eval/run_eval.py            (rule mode; add --llm to test the LLM (OpenAI) path)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from query_agent import ask  # noqa: E402
from siat_common import get_json  # noqa: E402
from siat_normalizer import parse_period  # noqa: E402

use_llm = "--llm" in sys.argv
cases = json.loads((Path(__file__).parent / "questions.json").read_text(encoding="utf-8"))
siat_cache: dict[int, dict] = {}


def siat_values(ind: int, districts: bool = False) -> dict:
    """(region code, period) -> value, straight from siat.stat.uz (districts: from the JSON v2 file)."""
    key = (ind, districts)
    if key not in siat_cache:
        out = {}
        if districts:
            dl, _ = get_json(f"/sdmx/{ind}/table/download/")
            v2, _ = get_json(dl["file_2"], timeout=120)
            for r in v2.get("data") or []:
                for c in r.get("values") or []:
                    p = parse_period(str(c.get("year") or ""))
                    if p:
                        out[(str(r["klass_id"]).strip(), p["period"])] = c.get("value")
        else:
            table, _ = get_json(f"/sdmx/{ind}/table/")
            for r in table:
                for d in r["data"]:
                    for k, v in d.items():
                        p = parse_period(k)
                        if p:
                            out[(str(r["code"]), p["period"])] = v
        siat_cache[key] = out
    return siat_cache[key]


passed = 0
only_new = "--new" in sys.argv  # only the quarterly / monthly / district / school-year questions
if only_new:
    cases = [c for c in cases if c.get("v2")]
for c in cases:
    a = ask(c["q"], force_rules=not use_llm)
    errs = []
    if c.get("clarify"):
        if a.ok:
            errs.append("expected a clarifying question, got numbers")
    else:
        if not a.ok:
            errs.append(f"no answer: {a.text[:80]}")
        else:
            inds = {r["indicator_id"] for r in a.rows}
            regs = sorted({r["region_code"] for r in a.rows})
            yrs = [min(r["year"] for r in a.rows), max(r["year"] for r in a.rows)]
            if inds != {c["indicator"]}:
                errs.append(f"indicator {inds} != {c['indicator']}")
            if regs != sorted(c["regions"]):
                errs.append(f"regions {regs} != {c['regions']}")
            if c.get("years") and yrs != c["years"]:
                errs.append(f"years {yrs} != {c['years']}")
            if c.get("freq") and {r.get("freq", "A") for r in a.rows} != {c["freq"]}:
                errs.append(f"freq {sorted({r.get('freq', 'A') for r in a.rows})} != {c['freq']}")
            if c.get("period") and sorted({r.get("period") for r in a.rows}) != sorted(c["period"]):
                errs.append(f"period {sorted({str(r.get('period')) for r in a.rows})} != {c['period']}")
            for r in a.rows:
                per = str(r.get("period") or r["year"])
                live = siat_values(c["indicator"], districts=len(r["region_code"]) == 7)
                v = live.get((r["region_code"], per))
                if v is None or r["value"] is None or abs(float(r["value"]) - float(v)) > 1e-6:
                    errs.append(f"value {r['region_code']}/{per}: db {r['value']} vs SIAT {v}")
            for k in ("dataset kodi", "Manba: SIAT"):
                if k not in a.text:
                    errs.append(f"answer text lacks '{k}'")
    passed += not errs
    print(("PASS " if not errs else "FAIL ") + c["q"][:70] + ("" if not errs else "\n     " + "; ".join(errs[:3])))
print(f"\n{passed}/{len(cases)} passed")
sys.exit(0 if passed == len(cases) else 1)
