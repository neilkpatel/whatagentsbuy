#!/usr/bin/env python3
"""Re-grade saved conformance rows OFFLINE with the current judge(), no network.

A verdict is derived from the response; when a parsing bug is fixed, every row
that kept a real response shape can be re-judged for free instead of re-paid.
`judge()` decides delivered/short on field NAMES, and observed_schema preserves
the names, so re-grading the saved shape reproduces the true verdict.

Rows whose capture failed (observed_schema is null/missing, because the old code
discarded the response before saving it) cannot be recovered here; they are
written to a recall queue for a fresh paid call. That gap is exactly why
conform.py now archives the raw response BEFORE parsing: after this, no future
bug is unrecoverable.

Usage:
  python3 regrade.py                 # re-grade data/conformance_2026-08-12.json
  python3 regrade.py <file> [<file>] # re-grade specific files
"""
import glob
import json
import os
import sys

import conform

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def regrade_rows(rows):
    """Return (regraded_clean_rows, recall_queue, flips). Pure, no I/O."""
    clean, recall, flips = [], [], []
    for r in rows:
        schema = r.get("observed_schema")
        if not isinstance(schema, dict):
            recall.append({"url": r.get("url"), "host": r.get("host"),
                           "quoted": r.get("quoted"),
                           "reason": "no response shape saved (capture bug lost it)"})
            continue
        old_conforms = bool(r.get("conforms"))
        v = conform.judge(schema, r.get("promised") or [])
        row = dict(r)
        row.update(v)                       # overwrite ONLY the verdict fields
        row["regraded"] = True
        clean.append(row)
        if v["conforms"] != old_conforms:
            flips.append({"url": r["url"], "host": r.get("host"),
                          "was": "delivered" if old_conforms else "failed",
                          "now": v["status"]})
    return clean, recall, flips


def main():
    files = sys.argv[1:] or [os.path.join(DATA, "conformance_2026-08-12.json")]
    rows = []
    seen = set()
    for f in files:
        for r in json.load(open(f)).get("rows", []):
            if r.get("url") and r["url"] not in seen:   # dedupe, keep first
                seen.add(r["url"])
                rows.append(r)

    clean, recall, flips = regrade_rows(rows)

    from collections import Counter
    split = Counter(r["status"] for r in clean)
    print(f"read {len(rows)} unique rows from {len(files)} file(s)")
    print(f"\nre-graded offline (free): {len(clean)}")
    for k in ("delivered", "short", "inconclusive"):
        print(f"    {k:<13} {split.get(k, 0)}")
    print(f"\nverdict flips vs the buggy run: {len(flips)}")
    for fl in flips[:12]:
        print(f"    {fl['was']:>9} -> {fl['now']:<12} {fl['host']}")
    if len(flips) > 12:
        print(f"    ... and {len(flips) - 12} more")
    print(f"\nno usable shape saved, must re-call (paid): {len(recall)}"
          f"  ({len(set(x['host'] for x in recall))} hosts)")

    out = os.path.join(DATA, "conformance_regraded.json")
    json.dump({"generated": "offline re-grade", "source": files,
               "note": "verdicts re-derived from saved response shapes; no network",
               "rows": clean}, open(out, "w"), indent=1)
    q = os.path.join(DATA, "conformance_recall_queue.json")
    json.dump({"note": "rows whose capture was lost; need a fresh paid call",
               "count": len(recall), "rows": recall}, open(q, "w"), indent=1)
    print(f"\nwrote {out} ({len(clean)} clean rows)")
    print(f"wrote {q} ({len(recall)} to re-call)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
