#!/usr/bin/env python3
"""Daily settlement sweep: exact on-chain demand for every graded service.

Reads USDC Transfer logs on Base addressed to the seller wallets we collect from
each service's own payment challenge, and appends a dated record. Run daily; the
history accumulates into a settlement tape that cannot be backfilled by anyone who
starts later.

Providers are pluggable on purpose. Depending on a third party is fine as long as
the source is named, swappable, and checkable against another:

  rpc         direct Base JSON-RPC. Free, no account, exact. Range-capped, so it
              is right for a rolling recent window, wrong for month-long backfill.
  blockscout  free public indexer. Per-address, no key. Good for snapshots.
  cdp         Coinbase CDP SQL API. Whole-market SQL over base.events. Needs
              CDP_API_KEY. This is what x402scan uses.
  bitquery    streaming GraphQL. Needs BITQUERY_API_KEY. Also used by x402scan.

Usage:
  python3 sweep.py                 # last 24h via rpc
  python3 sweep.py --hours 6
  python3 sweep.py --provider blockscout
"""
import argparse, json, os, time, urllib.error, urllib.request
from collections import defaultdict

import buyers

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
HIST = os.path.join(DATA, "history")

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
# Transfers to the zero/dead address are USDC burns/redemptions, not payments.
# Grouping them as settlement once produced a $138M phantom day, so drop them here.
BURN_ADDRS = {"0x0000000000000000000000000000000000000000",
              "0x000000000000000000000000000000000000dead"}
UA = {"Content-Type": "application/json",
      "User-Agent": "touchstone-probe/0.1 (+https://touchstone.neilkpatel.com)"}

# Ordered by preference. Public endpoints differ in whether they allow eth_getLogs
# at all, so we fail over rather than trusting any single one. But public Base RPC
# is broadly unreliable for this workload (result caps, rate limits, 5xx) and is
# the root cause of the gap/partial days. Set BASE_RPC_URL to a keyed endpoint
# (Alchemy/Infura free tier handles this volume fine) and it is used FIRST:
#   export BASE_RPC_URL="https://base-mainnet.g.alchemy.com/v2/<your-key>"
RPCS = ([os.environ["BASE_RPC_URL"]] if os.environ.get("BASE_RPC_URL") else []) + [
    "https://base.drpc.org", "https://base-rpc.publicnode.com",
    "https://mainnet.base.org", "https://base.llamarpc.com"]

BLOCK_SECONDS = 2
WINDOW_BLOCKS = 2000     # public-node log range cap
ADDR_BATCH = 150         # seller addresses per query
MAX_RETRIES = 3


def jrpc(url, method, params, timeout=45):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    if "error" in d:
        raise RuntimeError(str(d["error"])[:160])
    return d["result"]


# QUOTA_MARKERS is checked against the lowercased error text. A quota or auth
# refusal is a property of the ENDPOINT, not of the range, so retrying the same
# endpoint cannot succeed and every retry is pure latency. 2026-09-09: three of
# four public endpoints failed this way at once (drpc "-32001 usage limit",
# publicnode HTTP 403, llamarpc HTTP 525) and the sweep spent 3h42m grinding
# 3 doomed retries plus backoff over ~400 ranges before failing over.
QUOTA_MARKERS = ("usage limit", "rate limit", "too many requests", "429",
                 "403", "forbidden", "-32001", "quota", "exceeded", "525",
                 "unauthorized", "payment required")


def is_endpoint_refusal(err):
    """True if this error means the ENDPOINT is refusing us, not that the range
    is bad. Those must fail over immediately instead of burning MAX_RETRIES."""
    return any(m in str(err).lower() for m in QUOTA_MARKERS)


def pick_rpc(sample_topics=None):
    """Return the first endpoint that actually SERVES the query this sweep makes.

    publicnode and mainnet.base.org answer eth_blockNumber and a tiny 2-block
    getLogs but return empty (or reject) on realistic ranges. The old test used a
    2-block window, so those endpoints passed and the full sweep then came back
    empty, silently recording $0 days (8/15 and 8/17 were lost this way). USDC
    transfers on Base happen many times per block, so a ~50-block window must come
    back non-empty; if it does not, the endpoint cannot serve this query.

    A 50-block probe is still not the workload. A rate-limited endpoint answers
    small queries and refuses the 2000-block, 150-address ones this sweep is made
    of, so it PASSED the old check and was then chosen as primary. On 2026-09-09
    base.drpc.org did exactly that: fine on the probe, "-32001 usage limit" on
    every real range. When `sample_topics` is given, the check now runs one
    genuinely representative query, so the endpoint is tested on what it will be
    asked to do.
    """
    for url in RPCS:
        try:
            tip = int(jrpc(url, "eth_blockNumber", [], timeout=20), 16)
            logs = jrpc(url, "eth_getLogs", [{"address": USDC_BASE, "topics": [TRANSFER],
                        "fromBlock": hex(tip - 50), "toBlock": hex(tip)}], timeout=30)
            if not logs:
                print(f"  rpc returns empty on a real range, skipping: {url}")
                continue
            if sample_topics:
                # the real thing: full window, full address batch
                jrpc(url, "eth_getLogs", [{
                    "address": USDC_BASE, "topics": [TRANSFER, None, sample_topics],
                    "fromBlock": hex(tip - WINDOW_BLOCKS), "toBlock": hex(tip - 1)}], timeout=60)
            return url, tip
        except Exception as e:
            note = " (endpoint refusing us, not a bad range)" if is_endpoint_refusal(e) else ""
            print(f"  rpc unusable: {url} ({type(e).__name__}: {str(e)[:70]}){note}")
    raise SystemExit("no usable public Base RPC for eth_getLogs")


def topic_for(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


def sweep_rpc(addresses, hours):
    topics = [topic_for(a) for a in addresses]
    batches = [topics[i:i + ADDR_BATCH] for i in range(0, len(topics), ADDR_BATCH)]
    # Test each candidate on the query this sweep is actually made of, not on a
    # toy range. A quota-limited endpoint passes a 50-block probe and refuses
    # every real one, which is how a doomed endpoint got chosen as primary.
    url, tip = pick_rpc(sample_topics=batches[0] if batches else None)
    span = int(hours * 3600 / BLOCK_SECONDS)
    start = tip - span
    print(f"  provider rpc={url} tip={tip:,} scanning {span:,} blocks (~{hours}h)")

    logs, out_logs = [], []
    windows = list(range(start, tip, WINDOW_BLOCKS))
    # two passes: money in (topic 2 = recipient) and money out (topic 1 = sender).
    # Many services forward most of what they take, so inflow alone is turnover.
    total = len(windows) * len(batches) * 2
    done, failures = 0, []

    def get_range(topics, lo, hi, endpoint=None, retries=None):
        ep = endpoint or url
        for attempt in range(retries or MAX_RETRIES):
            try:
                return jrpc(ep, "eth_getLogs", [{
                    "address": USDC_BASE, "topics": topics,
                    "fromBlock": hex(lo), "toBlock": hex(hi)}]), True
            except Exception as e:
                # A quota/auth refusal is about the ENDPOINT, not this range, so
                # retrying the same endpoint is guaranteed to fail and only buys
                # latency. Give up on it immediately and let the caller fail over.
                # On 2026-09-09 three endpoints refused at once and the sweep
                # spent 3h42m paying 3 doomed attempts plus backoff per range.
                if is_endpoint_refusal(e):
                    return None, False
                if attempt < (retries or MAX_RETRIES) - 1:
                    time.sleep(1.2 * (attempt + 1))
        return None, False

    for lo in windows:
        hi = min(lo + WINDOW_BLOCKS - 1, tip)
        for batch in batches:
            for topics, sink in (([TRANSFER, None, batch], logs), ([TRANSFER, batch], out_logs)):
                res, ok = get_range(topics, lo, hi)
                if ok:
                    sink.extend(res)
                else:
                    failures.append((topics, lo, hi, sink))
                done += 1
            if done % 60 == 0:
                print(f"    {done}/{total} queries, {len(logs):,} in / {len(out_logs):,} out")

    # A failed range silently undercounts the day (8/18 lost ~14% this way). Most
    # failures are transient RPC hiccups, so retry the failed ranges once more after
    # a real pause, and only count what STILL fails.
    # Retry against the SAME endpoint first, then FAIL OVER to the other providers.
    # Sticking with one endpoint for the whole sweep was the actual cause of the
    # 2026-08-19..21 gap: base.drpc.org degraded mid-run, 10/176 ranges failed, the
    # retry hit the same degraded node, and the day was refused. A range that one
    # public node drops is usually served fine by the next one.
    if failures:
        print(f"  {len(failures)}/{total} ranges failed the first pass; retrying after a pause...")
        time.sleep(6)
        still = []
        for topics, lo, hi, sink in failures:
            res, ok = get_range(topics, lo, hi)
            if ok:
                sink.extend(res)
            else:
                still.append((topics, lo, hi, sink))
        failures = still

        if failures:
            alts = [u for u in RPCS if u != url]
            for alt in alts:
                if not failures:
                    break
                print(f"  {len(failures)} still failing; failing over to {alt}")
                nxt = []
                for topics, lo, hi, sink in failures:
                    res, ok = get_range(topics, lo, hi, endpoint=alt)
                    if ok:
                        sink.extend(res)
                    else:
                        nxt.append((topics, lo, hi, sink))
                failures = nxt

        # A range that fails on EVERY provider is not a flaky node, it is a response
        # too large to serve: one spam-volume seller (blockrun.ai did 205k txs in a
        # day) can push a 2000-block window past the node's result cap. Halving the
        # window until it fits recovers the range instead of writing it off as a
        # floor. Depth-limited so a genuinely dead range cannot loop.
        if failures:
            print(f"  {len(failures)} failed on all providers; splitting oversized ranges")
            def fetch_split(topics, lo, hi, sink, depth=0):
                res, ok = get_range(topics, lo, hi, retries=1)
                if ok:
                    sink.extend(res); return True
                if depth >= 6 or hi - lo < 8:
                    return False
                mid = (lo + hi) // 2
                a = fetch_split(topics, lo, mid, sink, depth + 1)
                b = fetch_split(topics, mid + 1, hi, sink, depth + 1)
                return a and b
            nxt = []
            for topics, lo, hi, sink in failures:
                if not fetch_split(topics, lo, hi, sink):
                    nxt.append((topics, lo, hi, sink))
            recovered = len(failures) - len(nxt)
            if recovered:
                print(f"  recovered {recovered} range(s) by splitting")
            failures = nxt
        failures = [(t, lo, hi) for t, lo, hi, _ in failures]
    failed = len(failures)
    if failed:
        print(f"  WARNING: {failed}/{total} queries failed after retry; counts are a floor")
    return logs, out_logs, {"provider": "rpc", "endpoint": url, "from_block": start,
                            "to_block": tip, "failed_queries": failed, "total_queries": total}


def decode_out(logs):
    """Group raw Transfer logs by sender: money leaving the seller.

    The zero address as SENDER is a mint, not a seller paying anyone. Without
    this filter it entered the tape as a seller with $248M of outflow and a
    quarter-billion-dollar negative net, which is the same shape of bug that
    put a $138M phantom on the site once already. The inflow side has always
    excluded burns; this is the matching guard on the outflow side.
    """
    by = defaultdict(lambda: {"settlements": 0, "usdc": 0.0})
    for lg in logs:
        try:
            frm = "0x" + lg["topics"][1][-40:]
            val = int(lg["data"], 16) / 1e6
        except Exception:
            continue
        if frm.lower() in BURN_ADDRS:
            continue
        r = by[frm.lower()]
        r["settlements"] += 1
        r["usdc"] += val
    return {k: {"settlements": v["settlements"], "usdc": round(v["usdc"], 6)} for k, v in by.items()}


def decode(logs):
    """Group raw Transfer logs by recipient, keeping the shape of who paid.

    A payer COUNT cannot tell you whether demand is real. One wallet sending a
    thousand payments and a thousand wallets sending one each look identical in a
    total, and a July 2026 study found much x402 settlement is internal to linked
    clusters. So we keep the distribution: how concentrated the money is, and how
    many buyers came back. The addresses themselves are not stored, only the shape.
    """
    by = defaultdict(lambda: {"settlements": 0, "usdc": 0.0,
                              "payers": defaultdict(lambda: {"n": 0, "usdc": 0.0}),
                              "first_block": None, "last_block": None})
    for lg in logs:
        try:
            to = "0x" + lg["topics"][2][-40:]
            frm = "0x" + lg["topics"][1][-40:]
            val = int(lg["data"], 16) / 1e6
            blk = int(lg["blockNumber"], 16)
        except Exception:
            continue
        if to.lower() in BURN_ADDRS:
            continue  # burn/redemption, not a payment to any seller
        r = by[to.lower()]
        r["settlements"] += 1
        r["usdc"] += val
        pr = r["payers"][frm.lower()]
        pr["n"] += 1
        pr["usdc"] += val
        r["first_block"] = blk if r["first_block"] is None else min(r["first_block"], blk)
        r["last_block"] = blk if r["last_block"] is None else max(r["last_block"], blk)

    out = {}
    for k, v in by.items():
        amounts = sorted((p["usdc"] for p in v["payers"].values()), reverse=True)
        total = sum(amounts) or 1.0
        shares = [a / total for a in amounts]
        out[k] = {
            "settlements": v["settlements"], "usdc": round(v["usdc"], 6),
            "unique_payers": len(v["payers"]),
            # how much of the money came from the single biggest buyer, and the top five
            "top_payer_share": round(shares[0], 4) if shares else None,
            "top5_payer_share": round(sum(shares[:5]), 4) if shares else None,
            # a buyer who came back is worth more evidence than a buyer who did not
            "repeat_payers": sum(1 for p in v["payers"].values() if p["n"] > 1),
            # Herfindahl on dollar share: 1.0 is one buyer, near 0 is many equal buyers
            "payer_hhi": round(sum(x * x for x in shares), 4) if shares else None,
            "first_block": v["first_block"], "last_block": v["last_block"],
        }
    return out


def overwrite_refusal(prior, new_per_addr, hours, new_failed, rows_key="by_address"):
    """Should this run refuse to overwrite an existing day file? Pure, so the
    guard on the ONE unrecoverable pipeline is testable: a missed or corrupted
    settlement day cannot be re-swept later (the window is rolling), and this
    function is the last thing standing between a lossy re-run and the tape.

    Returns a refusal reason, or None to allow the write. `force` is handled by
    the caller. Three refusals:
      1. a SHORTER window must never replace a longer one (a test sweep vs a day)
      2. a run with failed ranges must never replace a CLEAN same-window snapshot
      3. a same-window run holding <90% of the prior settlements is a lossier
         sweep, not new truth. Failure COUNT alone is a badly biased proxy: on
         2026-08-21 a 4% query-failure rate cost half the transaction count,
         because the ranges that fail are precisely the high-volume ones.
    Everything is defensive about shape: a malformed prior file must never crash
    the sweep, it should simply not block the write.
    """
    prior_hours = prior.get("hours", 0) or 0
    if prior_hours > hours:
        return (f"refusing to overwrite: that day already covers {prior_hours}h and this "
                f"run is only {hours}h; a short test sweep must not replace a full day.")
    if prior_hours != hours:
        return None
    prior_failed = (prior.get("meta") or {}).get("failed_queries", 0) or 0
    if prior_failed == 0 and (new_failed or 0) > 0:
        return (f"refusing to overwrite a CLEAN day with a partial one: the saved sweep had 0 "
                f"failed queries and this run failed {new_failed}; keep the clean snapshot.")
    def _n(rows):
        return sum(int((v or {}).get("settlements") or 0) for v in (rows or {}).values())
    prior_n, new_n = _n(prior.get(rows_key)), _n(new_per_addr)
    if prior_n and new_n < prior_n * 0.9:
        return (f"refusing to overwrite: the existing day holds {prior_n:,} settlements and this "
                f"run found only {new_n:,} ({new_n * 100 // max(prior_n, 1)}%); a smaller "
                f"same-window result is a lossier sweep, not new truth.")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--provider", default="rpc", choices=["rpc", "blockscout"])
    ap.add_argument("--force", action="store_true", help="allow a shorter window to replace today's file")
    args = ap.parse_args()

    latest = json.load(open(os.path.join(DATA, "latest.json")))
    origins = latest["origins"]

    addr_to_services = defaultdict(list)
    for r in origins:
        seen = r.get("payto_addresses")
        if seen is None:
            seen = []
            for c in r["checked"]:
                a = c.get("live_payto") or c.get("adv_payto") or ""
                if a.startswith("0x") and a.lower() not in [s.lower() for s in seen]:
                    seen.append(a)
            r["payto_addresses"] = seen
        for a in seen:
            addr_to_services[a.lower()].append(r["origin"])

    addresses = sorted(addr_to_services)
    print(f"sweeping {len(addresses)} seller addresses over the last {args.hours}h")
    if not addresses:
        raise SystemExit("no seller addresses; run probe.py first")

    per_buyer = None
    if args.provider == "rpc":
        logs, out_logs, meta = sweep_rpc(addresses, args.hours)
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
        # The buyer side, off the SAME logs: no extra RPC, no extra money. The
        # seller rollup above keeps only the shape of who paid, so without this
        # the payer addresses are discarded and that day is unrecoverable (the
        # window is rolling). See buyers.py for why log.from is the real buyer.
        per_buyer = buyers.decode_buyers(logs, seller_addrs=set(addresses))
    else:
        raise SystemExit("blockscout sweep lives in onchain.py; use --provider rpc here")

    # roll addresses up to the services that asked us to pay them
    live = 0
    for r in origins:
        recs = [per_addr.get(a.lower()) for a in r.get("payto_addresses", [])]
        recs = [x for x in recs if x]
        if not recs:
            r["settled"] = None
            continue
        r["settled"] = {
            "hours": args.hours,
            "settlements": sum(x["settlements"] for x in recs),
            "usdc": round(sum(x["usdc"] for x in recs), 6),
            "usdc_out": round(sum(x.get("usdc_out", 0.0) for x in recs), 6),
            "usdc_net": round(sum(x.get("usdc_net", x["usdc"]) for x in recs), 6),
            "unique_payers": sum(x["unique_payers"] for x in recs),
        }
        live += 1

    # A full 24h sweep of hundreds of seller addresses on Base ALWAYS finds
    # settlement. Zero means the endpoint failed, not that nothing settled, so
    # refuse to record a phantom-empty day (which corrupts the tape and averages).
    # Better a visible gap the daily job can retry than a silent $0. --force to override.
    if not per_addr and not args.force:
        raise SystemExit(
            f"refusing to record an empty day: 0 settlements from {meta['endpoint']} "
            f"({meta.get('failed_queries', 0)}/{meta.get('total_queries', 0)} queries failed). "
            f"This is an RPC failure, not a real $0. Retry with a working endpoint.")

    # A partial sweep silently undercounts settlement and corrupts the trend. Better
    # a known gap the daily job retries than a full day that is quietly 14% short.
    _fq, _tq = meta.get("failed_queries", 0), meta.get("total_queries", 0) or 1
    if _fq / _tq > 0.05 and not args.force:
        raise SystemExit(
            f"refusing to record a partial day: {_fq}/{_tq} log-range queries failed "
            f"({_fq / _tq * 100:.0f}%) from {meta['endpoint']}, which undercounts settlement. "
            f"Retry (transient RPC), or --force to record it anyway.")

    stamp = time.strftime("%Y-%m-%d")
    os.makedirs(HIST, exist_ok=True)
    existing = os.path.join(HIST, f"settlements_{stamp}.json")
    if os.path.exists(existing):
        try:
            _prior = json.load(open(existing))
        except Exception:
            _prior = {}
        _refuse = overwrite_refusal(_prior, per_addr, args.hours, _fq)
        if _refuse and not args.force:
            raise SystemExit(f"{_refuse} ({os.path.basename(existing)}) Use --force to override.")
    json.dump({"date": stamp, "generated": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
               "hours": args.hours, "meta": meta, "by_address": per_addr},
              open(os.path.join(HIST, f"settlements_{stamp}.json"), "w"), indent=1)

    # Buyer tape, written beside the seller tape as its own file so the existing
    # format is untouched and nothing downstream can break. Every reader of
    # data/history/ filters on the "settlements_" prefix, so this file cannot
    # reach public/ without a deliberate change: buyer addresses stay internal
    # until there is a decision to publish them.
    if per_buyer is not None:
        _bpath = os.path.join(HIST, f"buyers_{stamp}.json")
        _brefuse = None
        if os.path.exists(_bpath):
            try:
                _bprior = json.load(open(_bpath))
            except Exception:
                _bprior = {}
            _brefuse = overwrite_refusal(_bprior, per_buyer, args.hours, _fq, rows_key="by_buyer")
        if _brefuse and not args.force:
            # The seller day is already saved; refuse only this file rather than
            # killing a run that has otherwise succeeded.
            print(f"  buyer tape NOT written: {_brefuse}")
        else:
            json.dump({"date": stamp, "generated": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
                       "hours": args.hours, "meta": meta, "method": buyers.METHOD,
                       "summary": buyers.summarize(per_buyer), "by_buyer": per_buyer},
                      open(_bpath, "w"), indent=1)
            _bs = buyers.summarize(per_buyer)
            print(f"  buyer tape: {_bs['buyers']:,} distinct buyers, "
                  f"{_bs['resellers']:,} of them listed sellers, "
                  f"top buyer {_bs['top_buyer_share']:.1%} of dollars")

    latest["settlement_source"] =f"Base {meta['provider']} ({meta['endpoint']}), USDC transfers to seller addresses"
    latest["settlement_window_hours"] = args.hours
    latest["settlement_generated"] = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    json.dump(latest, open(os.path.join(DATA, "latest.json"), "w"), indent=1)

    tot_s = sum(v["settlements"] for v in per_addr.values())
    tot_u = sum(v["usdc"] for v in per_addr.values())
    print(f"\n{tot_s:,} settlements, ${tot_u:,.2f} USDC to {len(per_addr)} seller addresses in {args.hours}h")
    print(f"services with settlement in window: {live}/{len(origins)}")
    top = sorted((r for r in origins if r.get("settled")),
                 key=lambda r: -r["settled"]["settlements"])[:10]
    for r in top:
        s = r["settled"]
        print(f"  {s['settlements']:>8,} txs  ${s['usdc']:>10,.2f}  {s['unique_payers']:>4} payers  {r['origin'][:38]}")


if __name__ == "__main__":
    main()
