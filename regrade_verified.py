#!/usr/bin/env python3
"""Re-grade conformance_verified.json offline after a judge() improvement.

The saved observed_schema preserves the response's field names and structure
(including lists of records), so rebuilding it into a probe payload and running
the current judge() re-derives every verdict for free, no network, no re-pay.

The improved judge only ever finds MORE promised fields (nested objects and
lists of records), so this can only turn an overstated short into a smaller
short, or a false short into a delivery. It can never create a new shortfall,
so no seller can be newly accused by running it. git preserves the prior file.

Run: python3 regrade_verified.py
"""
import json
import os
from collections import Counter

import conform
import receipts

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "data", "conformance_verified.json")


def main():
    doc = json.load(open(SRC))
    rows = doc.get("rows", [])
    shorts_before = {r["url"] for r in rows if r.get("status") == "short"}
    changed = []
    for r in rows:
        # ONLY ever correct a row that is already a short, and ONLY downward. A
        # delivered or inconclusive verdict is never touched: an inconclusive is a
        # deliberate non-claim (often a two-call reconcile), and turning it into a
        # short from a single stored shape would manufacture a false accusation,
        # which is precisely what must never happen.
        if r.get("status") != "short":
            continue
        sch = r.get("observed_schema")
        if not isinstance(sch, dict):
            continue
        v = conform.judge(receipts.schema_probe(sch), r.get("promised") or [])
        old_missing = sorted(r.get("missing") or [])
        new_missing = sorted(v["missing"])
        if v["status"] not in ("delivered", "short") or len(new_missing) >= len(old_missing):
            continue                          # only accept a strict reduction in shortfall
        changed.append((r.get("host"), len(old_missing), v["status"], len(new_missing)))
        r["status"] = v["status"]
        r["missing"] = v["missing"]
        r["extra"] = v["extra"]
        r["conforms"] = (v["status"] == "delivered")
        if v["status"] == "delivered":
            r["why"] = ""
        elif "two calls" not in (r.get("why") or ""):
            r["why"] = f"missing {len(v['missing'])} promised field(s)"
        # a still-short row that was reverified keeps its 'confirmed on two calls' why

    # Safety net: the set of shorts may only shrink. If any URL that was not
    # short became short, this pass manufactured an accusation. Refuse to write.
    shorts_after = {r["url"] for r in rows if r.get("status") == "short"}
    assert shorts_after <= shorts_before, (
        "regrade manufactured a short: " + str(shorts_after - shorts_before))
    doc["counts"] = dict(Counter(r.get("status") for r in rows))
    _note = doc.get("note", "")
    _tag = "Re-graded 2026-08-15 with a list-of-records aware judge."
    if _tag not in _note:
        doc["note"] = (_note + " " + _tag).strip()
    json.dump(doc, open(SRC, "w"), indent=1)

    print(f"changed {len(changed)} rows (overstated shortfalls corrected):")
    for h, om, ns, nm in changed:
        print(f"  {h}: short missing {om} -> {ns} missing {nm}")
    print("new counts:", doc["counts"])


if __name__ == "__main__":
    main()
