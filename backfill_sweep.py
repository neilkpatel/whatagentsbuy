#!/usr/bin/env python3
"""Re-sweep historical UTC days from the chain, to repair the settlement tape.

Why this exists: every day in data/history/ except 2026-08-21 was written by the
pre-8/21 sweep, which held one RPC endpoint for a whole run and wrote off any
range a node refused. Their meta carries non-zero failed_queries, and a failed
range is never random: the ranges that fail are the HIGH-VOLUME ones, because
volume is what makes a getLogs response too large to serve. That is why 8/21
measured a 3.98% query-failure rate and still lost HALF the transaction count.
So `failed_queries/total_queries` cannot be used to decide a day is fine.

The daily job has to tolerate some failure, because it gets one shot at a rolling
window before the blocks age out of what public nodes will serve. A backfill has
no such constraint: it can retry a range forever. So this script holds a stricter
standard than sweep.py.

  sweep.py     records a day if under 5% of ranges failed  (a floor, and biased)
  backfill.py  records a day only if ZERO ranges failed    (a count, not a floor)

Any day that cannot be made clean is refused and named, never written short.

Day basis: UTC calendar days, resolved by binary search on block timestamps. The
old tape stamped a rolling 24h window ending at the ~06:40 run time with that
day's date, so its "days" were neither calendar days nor aligned with each other.
Bars you can compare have to sit on the same grid, and UTC is the grid the rest of
the stack already uses.

Usage:
  python3 backfill_sweep.py --from 2026-08-04 --to 2026-08-20
  python3 backfill_sweep.py --date 2026-08-15
  python3 backfill_sweep.py --from 2026-08-04 --to 2026-08-20 --dry-run
"""
import argparse, json, os, sys, time, urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sweep import (USDC_BASE, TRANSFER, UA, RPCS, WINDOW_BLOCKS, ADDR_BATCH,
                   jrpc, topic_for, decode, decode_out, HIST, DATA)

# Endpoints proven to serve ARCHIVE getLogs (publicnode 403s on old ranges,
# llamarpc 521s). Probed before use anyway; this is just the preference order.
PACE = 0.15   # seconds between served queries; public Base RPCs 429 without it
ARCHIVE_RPCS = [u for u in RPCS if "publicnode" not in u and "llamarpc" not in u]


def usable_endpoints():
    """Endpoints that answer a real archive log query, in preference order."""
    ok = []
    for url in ARCHIVE_RPCS:
        try:
            tip = int(jrpc(url, "eth_blockNumber", [], timeout=20), 16)
            probe = tip - 700_000  # ~16 days back, inside our backfill span
            logs = jrpc(url, "eth_getLogs", [{
                "address": USDC_BASE, "topics": [TRANSFER],
                "fromBlock": hex(probe), "toBlock": hex(probe + 50)}], timeout=40)
            if logs:
                ok.append(url)
                print(f"  archive OK: {url} (tip {tip:,})")
            else:
                print(f"  archive empty, skipping: {url}")
        except Exception as e:
            print(f"  archive unusable: {url} ({str(e)[:70]})")
    if not ok:
        raise SystemExit("no archive-capable Base RPC; cannot backfill")
    return ok


_ts_cache = {}
BLOCKS_PER_DAY = 43_200  # Base: 2s blocks, 86400/2. Verified exact against chain.


def block_ts(endpoints, n):
    """Timestamp of block n, rotating endpoints and backing off on 429.

    Public Base RPCs rate-limit aggressively, and a naive per-day binary search
    fires hundreds of these. Everything that can be derived arithmetically is,
    so this stays a handful of calls used only to ANCHOR and VERIFY.
    """
    if n in _ts_cache:
        return _ts_cache[n]
    last = None
    for attempt in range(6):
        for url in endpoints:
            try:
                b = jrpc(url, "eth_getBlockByNumber", [hex(n), False], timeout=30)
                ts = int(b["timestamp"], 16)
                _ts_cache[n] = ts
                return ts
            except Exception as e:
                last = e
        time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"cannot read block {n}: {str(last)[:120]}")


def block_at(endpoints, target_ts, tip):
    """First block at or after target_ts, by estimate then short bisect.

    Used ONCE to anchor. Base's cadence is exact enough that later day
    boundaries are derived as anchor + 43200*n and merely spot-checked, which
    is what keeps this inside the rate limits.
    """
    est = tip - int((block_ts(endpoints, tip) - target_ts) / 2)
    lo, hi = max(1, est - 10_000), min(tip, est + 10_000)
    if block_ts(endpoints, lo) > target_ts:
        lo = max(1, lo - 200_000)
    if block_ts(endpoints, hi) < target_ts:
        hi = min(tip, hi + 200_000)
    while lo < hi:
        mid = (lo + hi) // 2
        if block_ts(endpoints, mid) < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def day_bounds(endpoints, date_str, anchor, tip):
    """[start, end] blocks for a UTC day, derived from the anchor and verified.

    Derivation is arithmetic (exact 2s cadence); the verification is one call.
    If the chain has drifted from the ideal cadence, correct by the observed
    offset rather than trusting the arithmetic. An off-by-1000 boundary would
    silently move ~30 minutes of settlement between two days.
    """
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    t0, t1 = int(day.timestamp()), int((day + timedelta(days=1)).timestamp())
    a_block, a_ts = anchor
    start = a_block + (t0 - a_ts) // 2
    ts = block_ts(endpoints, start)
    if ts != t0:                       # correct any cadence drift, then re-check
        start += (t0 - ts) // 2
        ts = block_ts(endpoints, start)
        while ts < t0:
            start += 1
            ts = block_ts(endpoints, start)
        while ts > t0 and block_ts(endpoints, start - 1) >= t0:
            start -= 1
            ts = block_ts(endpoints, start)
    end = start + (t1 - t0) // 2 - 1
    end = min(end, tip)
    return start, end


def sweep_range(endpoints, addresses, start, end, label):
    """Sweep [start, end] for these addresses. Returns (logs, out_logs, stats).

    Every range is pursued until it is served: retry on the same endpoint, fail
    over across endpoints, then halve oversized windows. Unlike the daily job,
    this reports what is STILL unserved so the caller can refuse the day.
    """
    topics_all = [topic_for(a) for a in addresses]
    batches = [topics_all[i:i + ADDR_BATCH] for i in range(0, len(topics_all), ADDR_BATCH)]
    windows = list(range(start, end + 1, WINDOW_BLOCKS))
    logs, out_logs = [], []
    total = len(windows) * len(batches) * 2
    done = 0
    unserved = []
    splits = 0
    rr = 0

    def get(topics, lo, hi, tries=3):
        """Try every endpoint, with backoff, before giving up on this range.

        Endpoints are rotated per call so a single node is not the one absorbing
        every query, which is what turns into 429s and then into a short day.
        """
        nonlocal rr
        order = endpoints[rr % len(endpoints):] + endpoints[:rr % len(endpoints)]
        rr += 1
        for ep in order:
            for attempt in range(tries):
                try:
                    r = jrpc(ep, "eth_getLogs", [{
                        "address": USDC_BASE, "topics": topics,
                        "fromBlock": hex(lo), "toBlock": hex(hi)}], timeout=60)
                    time.sleep(PACE)
                    return r, True
                except Exception as e:
                    # 429 means slow down, not that the range is unserveable.
                    time.sleep((3.0 if "429" in str(e) else 0.8) * (attempt + 1))
        return None, False

    def fetch(topics, lo, hi, sink, depth=0):
        nonlocal splits
        res, ok = get(topics, lo, hi, tries=(3 if depth == 0 else 1))
        if ok:
            sink.extend(res)
            return True
        # A range that no endpoint will serve is almost always TOO LARGE, not
        # dead: one spam-volume seller can push a 2000-block window past a
        # node's result cap. Halve until it fits. This is the step whose
        # absence cost half the transaction count on 8/21.
        if depth >= 8 or hi - lo < 4:
            return False
        mid = (lo + hi) // 2
        splits += 1
        a = fetch(topics, lo, mid, sink, depth + 1)
        b = fetch(topics, mid + 1, hi, sink, depth + 1)
        return a and b

    for lo in windows:
        hi = min(lo + WINDOW_BLOCKS - 1, end)
        for batch in batches:
            for topics, sink in (([TRANSFER, None, batch], logs),
                                 ([TRANSFER, batch], out_logs)):
                if not fetch(topics, lo, hi, sink):
                    unserved.append((lo, hi))
                done += 1
        pct = 100 * done / total
        print(f"    {label}: {done}/{total} ({pct:.0f}%) "
              f"{len(logs):,} in / {len(out_logs):,} out, {splits} splits", flush=True)

    return logs, out_logs, {"total_queries": total, "unserved": unserved, "splits": splits}


def build_day(endpoints, addresses, date_str, anchor, tip, dry_run=False):
    start, end = day_bounds(endpoints, date_str, anchor, tip)
    print(f"\n=== {date_str} UTC  blocks {start:,}..{end:,} ({end - start + 1:,}) ===", flush=True)
    if dry_run:
        return {"date": date_str, "from_block": start, "to_block": end,
                "blocks": end - start + 1, "queries": None}

    logs, out_logs, st = sweep_range(endpoints, addresses, start, end, date_str)

    if st["unserved"]:
        print(f"  REFUSED {date_str}: {len(st['unserved'])} range(s) still unserved "
              f"after retry, failover and splitting. Not writing a short day.")
        return None

    per_addr = decode(logs)
    out_addr = decode_out(out_logs)
    for a, v in out_addr.items():
        rec = per_addr.setdefault(a, {"settlements": 0, "usdc": 0.0, "unique_payers": 0,
                                      "first_block": None, "last_block": None})
        rec["usdc_out"] = v["usdc"]
        rec["transfers_out"] = v["settlements"]
    for a, rec in per_addr.items():
        rec.setdefault("usdc_out", 0.0)
        rec.setdefault("transfers_out", 0)
        rec["usdc_net"] = round(rec["usdc"] - rec["usdc_out"], 6)

    if not per_addr:
        print(f"  REFUSED {date_str}: zero settlements. On Base that is always an "
              f"RPC failure, never a real day.")
        return None

    tot_s = sum(v["settlements"] for v in per_addr.values())
    tot_u = sum(v["usdc"] for v in per_addr.values())
    rec = {
        "date": date_str,
        "generated": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "hours": 24,
        "meta": {
            "provider": "rpc", "endpoint": ",".join(endpoints),
            "from_block": start, "to_block": end,
            "failed_queries": 0, "total_queries": st["total_queries"],
            "range_splits": st["splits"],
            # Stamped so a reader can tell a repaired UTC day from an old
            # rolling-window day without diffing block numbers.
            "basis": "utc_day", "backfilled": True,
            "backfill_note": "re-swept from chain with failover + range splitting; "
                             "written only because zero ranges were left unserved",
        },
        "by_address": per_addr,
    }
    print(f"  {date_str}: {tot_s:,} settlements, ${tot_u:,.2f} across "
          f"{len(per_addr)} addresses, {st['splits']} splits, 0 unserved", flush=True)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to", dest="to")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=HIST)
    args = ap.parse_args()

    if args.date:
        dates = [args.date]
    elif args.frm and args.to:
        a = datetime.strptime(args.frm, "%Y-%m-%d")
        b = datetime.strptime(args.to, "%Y-%m-%d")
        dates = [(a + timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range((b - a).days + 1)]
    else:
        raise SystemExit("need --date or --from/--to")

    latest = json.load(open(os.path.join(DATA, "latest.json")))
    addrs = set()
    for r in latest["origins"]:
        for a in (r.get("payto_addresses") or []):
            if a.startswith("0x"):
                addrs.add(a.lower())
        for c in r.get("checked", []):
            a = (c.get("live_payto") or c.get("adv_payto") or "")
            if a.startswith("0x"):
                addrs.add(a.lower())
    addresses = sorted(addrs)
    print(f"backfilling {len(dates)} day(s) over {len(addresses)} seller addresses")

    print("probing archive endpoints...")
    endpoints = usable_endpoints()
    tip = int(jrpc(endpoints[0], "eth_blockNumber", []), 16)

    # Anchor once on the first day's UTC midnight; every other boundary is
    # derived from it arithmetically. Binary-searching each day separately is
    # what tripped the public-RPC rate limiter.
    t0 = int(datetime.strptime(dates[0], "%Y-%m-%d")
             .replace(tzinfo=timezone.utc).timestamp())
    a_block = block_at(endpoints, t0, tip)
    anchor = (a_block, block_ts(endpoints, a_block))
    print(f"  anchor: block {a_block:,} @ {anchor[1]} ({dates[0]} 00:00 UTC)")

    os.makedirs(args.out, exist_ok=True)
    written, refused = [], []
    for d in dates:
        try:
            rec = build_day(endpoints, addresses, d, anchor, tip, dry_run=args.dry_run)
        except Exception as e:
            print(f"  ERROR {d}: {type(e).__name__}: {str(e)[:160]}")
            refused.append(d)
            continue
        if rec is None:
            refused.append(d)
            continue
        if args.dry_run:
            print(f"  dry-run {d}: {rec}")
            continue
        path = os.path.join(args.out, f"settlements_{d}.json")
        json.dump(rec, open(path, "w"), indent=1)
        written.append(d)
        print(f"  wrote {os.path.basename(path)}")

    print(f"\ndone. written={len(written)} refused={len(refused)}")
    if refused:
        print(f"refused days (left as-is, NOT overwritten): {', '.join(refused)}")


if __name__ == "__main__":
    main()
