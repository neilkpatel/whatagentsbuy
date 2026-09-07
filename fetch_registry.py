#!/usr/bin/env python3
"""Refresh the public x402 registry snapshot that probe.py grades against.

Source: Coinbase CDP discovery API (public, unauthenticated). Every listed
resource, with its advertised price, declared call shape, and the registry's own
usage counters.

SNAPSHOT SAFETY (added 2026-08-04 after this bit us): the previous version broke
out of pagination on ANY network error and then saved whatever it had as the
day's snapshot. Two pulls the same day produced 3,100 and 14,618 resources — a
transient blip silently became a 4.7x understatement, and the truncated figures
were quoted as fact for hours afterward. Now: each page retries, a partial walk
is recorded as partial, and a snapshot materially smaller than the previous one
is refused rather than written. Losing a day of freshness beats publishing a
silently truncated universe.
"""
import json, os, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
UA = {"User-Agent": "touchstone-probe/0.1 (+https://touchstone.neilkpatel.com)"}
API = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"

PAGE = 100
MAX_OFFSET = 60000
TRIES = 3
# A real registry does not shrink by a quarter overnight. Anything below this
# ratio is treated as a broken fetch, not as churn.
FLOOR_RATIO = 0.75


def get_page(offset):
    last = None
    for attempt in range(TRIES):
        try:
            url = f"{API}?limit={PAGE}&offset={offset}"
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r).get("items", [])
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"offset {offset} failed after {TRIES} tries: {last}")


def previous_count():
    p = os.path.join(DATA, "cdp_resources_raw.json")
    if not os.path.exists(p):
        return 0
    try:
        return len(json.load(open(p)))
    except Exception:
        return 0


def main():
    items, offset, partial, why = [], 0, False, ""
    while offset <= MAX_OFFSET:
        try:
            got = get_page(offset)
        except Exception as e:
            partial, why = True, str(e)
            print(f"! pagination interrupted at offset {offset}: {e}")
            break
        items += got
        if len(got) < PAGE:
            break
        offset += PAGE

    if not items:
        raise SystemExit("registry returned nothing; keeping the previous snapshot")

    prev = previous_count()
    if prev and len(items) < prev * FLOOR_RATIO:
        raise SystemExit(
            f"REFUSING to overwrite snapshot: got {len(items):,} resources vs {prev:,} previously "
            f"({len(items)/prev:.0%} of prior, floor is {FLOOR_RATIO:.0%})."
            + (f" Walk was partial: {why}" if partial else " Walk completed but the registry shrank sharply.")
            + " Keeping the previous snapshot; investigate before forcing."
        )

    out = os.path.join(DATA, "cdp_resources_raw.json")
    json.dump(items, open(out, "w"))
    # keep a dated copy so registry churn itself becomes a time series
    stamp = time.strftime("%Y-%m-%d")
    json.dump({"date": stamp, "count": len(items), "partial": partial,
               "prev_count": prev, "error": why or None},
              open(os.path.join(DATA, f"registry_{stamp}.json"), "w"))
    origins = len({i["resource"].split("/")[2] for i in items
                   if isinstance(i.get("resource"), str) and i["resource"].startswith("http")})
    flag = "  (PARTIAL WALK — accepted because it cleared the floor)" if partial else ""
    print(f"registry snapshot: {len(items):,} resources / {origins:,} origins "
          f"(previous {prev:,}){flag}")


if __name__ == "__main__":
    main()
