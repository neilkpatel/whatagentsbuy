#!/usr/bin/env python3
"""Promote clean daily grades into conformance_verified.json.

conform.py writes dated conformance_<date>.json files, but receipts.py (and thus
the verified/preflight tier and the MCP) mint ONLY from conformance_verified.json.
Without this step the dated grades never count -- exactly the freeze that stranded
two weeks of grading at 350 verified while we had paid far more sellers.

This merges the clean, gradeable rows (a real paid delivery outcome: delivered or
short, with an observed_schema) into the verified set, deduped by url with the
NEWEST grade winning, load-merge-write with a backup. conform.py calls
promote_verified() at the end of every run, so grades count immediately; this file
also backfills the whole history when run directly.

  python3 promote_verified.py     # backfill every dated conformance_*.json
"""
import glob, json, os, shutil, time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
VER = os.path.join(DATA, "conformance_verified.json")
BACKUPS = os.path.expanduser("~/Automation/whatagentsbuy-backups")


def is_gradeable(r):
    """A row that represents a real paid delivery outcome we can stand behind."""
    return bool(r.get("url") and r.get("observed_schema")
                and r.get("status") in ("delivered", "short"))


def _write(doc):
    doc["generated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = VER + ".tmp"                       # atomic: never leave a half-written file
    json.dump(doc, open(tmp, "w"), indent=1)
    os.replace(tmp, VER)


def _backup(tag):
    try:
        os.makedirs(BACKUPS, exist_ok=True)
        shutil.copy(VER, os.path.join(BACKUPS, f"conformance_verified_{tag}_{time.strftime('%Y%m%d-%H%M%S')}.json"))
    except Exception:
        pass


def promote_verified(rows, backup=True):
    """Merge gradeable rows into conformance_verified.json (newest grade per url
    wins). Returns (added, updated, total). Safe to call every run."""
    doc = json.load(open(VER))
    by_url = {r["url"]: r for r in doc.get("rows", [])}
    before = len(by_url)
    updated = 0
    for r in rows:
        if not is_gradeable(r):
            continue
        if r["url"] in by_url:
            updated += 1
        by_url[r["url"]] = r
    if backup:
        _backup("promote")
    doc["rows"] = list(by_url.values())
    _write(doc)
    return len(by_url) - before, updated, len(by_url)


def backfill_all():
    """One-time (or idempotent) sweep of every dated conformance file into the
    verified set. Idempotent: re-running only ever refreshes to the newest grade."""
    doc = json.load(open(VER))
    by_url = {r["url"]: r for r in doc.get("rows", [])}
    before = len(by_url)
    _backup("backfill")
    for f in sorted(glob.glob(os.path.join(DATA, "conformance_2026-*.json"))):
        n = 0
        for r in json.load(open(f)).get("rows", []):
            if is_gradeable(r):
                by_url[r["url"]] = r
                n += 1
        if n:
            print(f"  {os.path.basename(f)}: merged {n} gradeable rows")
    doc["rows"] = list(by_url.values())
    _write(doc)
    print(f"backfill: {before} -> {len(by_url)} verified rows (+{len(by_url) - before})")


if __name__ == "__main__":
    backfill_all()
