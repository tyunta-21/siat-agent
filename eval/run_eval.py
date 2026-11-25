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

use_llm = "--llm" in sys.argv
cases = json.loads((Path(__file__).parent / "questions.json").read_text(encoding="utf-8"))
siat_cache: dict[int, dict] = {}


def siat_values(ind: int) -> dict:
    if ind not in siat_cache:
        table, _ = get_json(f"/sdmx/{ind}/table/")
        siat_cache[ind] = {(str(r["code"]), int(k)): v for r in table for d in r["data"] for k, v in d.items()
                           if k.isdigit()}
    return siat_cache[ind]


passed = 0
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
            if yrs != c["years"]:
                errs.append(f"years {yrs} != {c['years']}")
            live = siat_values(c["indicator"])
            for r in a.rows:
                v = live.get((r["region_code"], r["year"]))
                if v is None or abs(float(r["value"]) - v) > 1e-6:
                    errs.append(f"value {r['region_code']}/{r['year']}: db {r['value']} vs SIAT {v}")
            for k in ("dataset kodi", "Manba: SIAT"):
                if k not in a.text:
                    errs.append(f"answer text lacks '{k}'")
    passed += not errs
    print(("PASS " if not errs else "FAIL ") + c["q"][:70] + ("" if not errs else "\n     " + "; ".join(errs[:3])))
print(f"\n{passed}/{len(cases)} passed")
sys.exit(0 if passed == len(cases) else 1)
