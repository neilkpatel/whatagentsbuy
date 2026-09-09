#!/usr/bin/env python3
"""A short-window look at the buyer side, without touching the settlement tape.

sweep.py now retains buyer identity, but only for the windows it runs, so the
first real buyer day arrives with tomorrow's 06:40 job. This exists to answer the
question that decides what gets built: is the buyer side 200 wallets or 20,000,
is there a long tail, and how much of the money is a handful of addresses.

Deliberately NOT the tape. Output goes to data/probes/, never data/history/, so a
short exploratory window can never be mistaken for a settlement day or replace
one. The tape's rolling window is unrecoverable; probes are free to re-run.

Inflow pass only. sweep_rpc() also scans money leaving sellers, which the buyer
side does not need, and skipping it halves the RPC load. That matters while the
daily sweep is running against the same public endpoints.

  python3 buyer_probe.py --hours 6
  python3 buyer_probe.py --hours 6 --top 30
"""
import argparse
import json
import os
import time

import buyers
import sweep

HERE = os.path.dirname(os.path.abspath(__file__))
PROBES = os.path.join(HERE, "data", "probes")


def seller_addresses(latest):
    """The seller payTo set, derived the way sweep.py derives it.

    `payto_addresses` is a CACHE that sweep.py writes back into latest.json at the
    end of a run; it is absent on a fresh probe file. Reading only the cached key
    silently yields ZERO addresses, and a scan of zero addresses returns zero
    buyers while looking exactly like a completed run. Fall back to the same
    live_payto/adv_payto walk sweep.py uses.
    """
    out = set()
    for r in latest.get("origins") or []:
        cached = r.get("payto_addresses")
        if cached:
            out.update(a.lower() for a in cached if a.startswith("0x"))
            continue
        for c in r.get("checked") or []:
            a = c.get("live_payto") or c.get("adv_payto") or ""
            if a.startswith("0x"):
                out.add(a.lower())
    return out


def scan_in(addresses, hours, url, tip):
    """USDC transfers INTO the seller set over the last `hours`.

    Same hardening as the tape sweep, because the failure modes are the same:
    a range that fails silently undercounts, and a range that fails on every
    provider is usually a response too large to serve rather than a dead node,
    so it is halved rather than written off (one spam-volume seller can push a
    2000-block window past a public node's result cap).
    """
    span = int(hours * 3600 / sweep.BLOCK_SECONDS)
    start = tip - span
    topics = [sweep.topic_for(a) for a in addresses]
    batches = [topics[i:i + sweep.ADDR_BATCH] for i in range(0, len(topics), sweep.ADDR_BATCH)]
    windows = list(range(start, tip, sweep.WINDOW_BLOCKS))
    total, done, failed = len(windows) * len(batches), 0, 0
    logs = []

    def get(t, lo, hi, endpoint=None, retries=2):
        for attempt in range(retries):
            try:
                return sweep.jrpc(endpoint or url, "eth_getLogs", [{
                    "address": sweep.USDC_BASE, "topics": t,
                    "fromBlock": hex(lo), "toBlock": hex(hi)}]), True
            except Exception:
                if attempt < retries - 1:
                    time.sleep(1.0 * (attempt + 1))
        return None, False

    def fetch(t, lo, hi, depth=0):
        res, ok = get(t, lo, hi)
        if ok:
            logs.extend(res)
            return True
        for alt in [u for u in sweep.RPCS if u != url]:
            res, ok = get(t, lo, hi, endpoint=alt, retries=1)
            if ok:
                logs.extend(res)
                return True
        if depth >= 6 or hi - lo < 8:
            return False
        mid = (lo + hi) // 2
        a = fetch(t, lo, mid, depth + 1)
        b = fetch(t, mid + 1, hi, depth + 1)
        return a and b

    for lo in windows:
        hi = min(lo + sweep.WINDOW_BLOCKS - 1, tip)
        for batch in batches:
            if not fetch([sweep.TRANSFER, None, batch], lo, hi):
                failed += 1
            done += 1
            if done % 25 == 0:
                print(f"    {done}/{total} queries, {len(logs):,} transfers")
    return logs, {"from_block": start, "to_block": tip, "endpoint": url,
                  "failed_queries": failed, "total_queries": total}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()

    latest = json.load(open(os.path.join(HERE, "data", "latest.json")))
    addrs = sorted(seller_addresses(latest))
    if not addrs:
        raise SystemExit("no seller addresses in latest.json; run probe.py first")

    url, tip = sweep.pick_rpc()
    print(f"probing {a.hours}h of buyer activity across {len(addrs):,} seller addresses")
    logs, meta = scan_in(addrs, a.hours, url, tip)
    print(f"\n{len(logs):,} transfers | {meta['failed_queries']}/{meta['total_queries']} ranges failed")
    if meta["failed_queries"]:
        print("  WARNING: incomplete, treat every count below as a floor")

    rows = buyers.decode_buyers(logs, seller_addrs=set(addrs))
    s = buyers.summarize(rows)
    os.makedirs(PROBES, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H%M%S")
    path = os.path.join(PROBES, f"buyers_probe_{stamp}.json")
    json.dump({"kind": "probe", "not_the_tape": True, "generated": stamp,
               "hours": a.hours, "meta": meta, "method": buyers.METHOD,
               "summary": s, "by_buyer": rows}, open(path, "w"), indent=1)

    print(f"\n{s['buyers']:,} distinct buyers  ${s['usdc']:,.2f}  {s['settlements']:,} payments")
    print(f"  {s['one_seller_buyers']:,} paid exactly one seller | {s['multi_seller_buyers']:,} paid 5+")
    print(f"  {s['resellers']:,} are themselves listed sellers (reseller, flagged not filtered)")
    print(f"  top buyer {s['top_buyer_share']:.1%} of dollars | top 10 {s['top10_buyer_share']:.1%}"
          f" | HHI {s['buyer_hhi']:.4f} | median spend ${s['median_spend']:,.4f}\n")
    top = sorted(rows.items(), key=lambda kv: -kv[1]["usdc"])[:a.top]
    print(f'{"wallet":<44}{"sellers":>8}{"pays":>8}{"USDC":>13}{"top-sellr":>11}  flag')
    print("-" * 92)
    for w, r in top:
        print(f'{w:<44}{r["sellers"]:>8}{r["settlements"]:>8}{r["usdc"]:>13,.4f}'
              f'{(r["top_seller_share"] or 0):>10.1%}  {"reseller" if r["is_seller"] else ""}')
    print(f"\nsaved {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
