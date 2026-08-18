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
# at all, so we fail over rather than trusting any single one.
RPCS = ["https://base.drpc.org", "https://base-rpc.publicnode.com",
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


def pick_rpc():
    """Return the first endpoint that actually SERVES real log queries.

    publicnode and mainnet.base.org answer eth_blockNumber and a tiny 2-block
    getLogs but return empty (or reject) on realistic ranges. The old test used a
    2-block window, so those endpoints passed and the full sweep then came back
    empty, silently recording $0 days (8/15 and 8/17 were lost this way). USDC
    transfers on Base happen many times per block, so a ~50-block window must come
    back non-empty; if it does not, the endpoint cannot serve this query."""
    for url in RPCS:
        try:
            tip = int(jrpc(url, "eth_blockNumber", [], timeout=20), 16)
            logs = jrpc(url, "eth_getLogs", [{"address": USDC_BASE, "topics": [TRANSFER],
                        "fromBlock": hex(tip - 50), "toBlock": hex(tip)}], timeout=30)
            if not logs:
                print(f"  rpc returns empty on a real range, skipping: {url}")
                continue
            return url, tip
        except Exception as e:
            print(f"  rpc unusable: {url} ({type(e).__name__})")
    raise SystemExit("no usable public Base RPC for eth_getLogs")


def topic_for(addr):
    return "0x" + "0" * 24 + addr[2:].lower()


def sweep_rpc(addresses, hours):
    url, tip = pick_rpc()
    span = int(hours * 3600 / BLOCK_SECONDS)
    start = tip - span
    print(f"  provider rpc={url} tip={tip:,} scanning {span:,} blocks (~{hours}h)")

    topics = [topic_for(a) for a in addresses]
    batches = [topics[i:i + ADDR_BATCH] for i in range(0, len(topics), ADDR_BATCH)]
    logs, out_logs = [], []
    windows = list(range(start, tip, WINDOW_BLOCKS))
    # two passes: money in (topic 2 = recipient) and money out (topic 1 = sender).
    # Many services forward most of what they take, so inflow alone is turnover.
    total = len(windows) * len(batches) * 2
    done, failed = 0, 0

    for lo in windows:
        hi = min(lo + WINDOW_BLOCKS - 1, tip)
        for batch in batches:
            for attempt in range(MAX_RETRIES):
                try:
                    res = jrpc(url, "eth_getLogs", [{
                        "address": USDC_BASE, "topics": [TRANSFER, None, batch],
                        "fromBlock": hex(lo), "toBlock": hex(hi)}])
                    logs.extend(res)
                    break
                except Exception as e:
                    if attempt == MAX_RETRIES - 1:
                        failed += 1
                    else:
                        time.sleep(1.2 * (attempt + 1))
            done += 1
            for attempt in range(MAX_RETRIES):
                try:
                    res = jrpc(url, "eth_getLogs", [{
                        "address": USDC_BASE, "topics": [TRANSFER, batch],
                        "fromBlock": hex(lo), "toBlock": hex(hi)}])
                    out_logs.extend(res)
                    break
                except Exception:
                    if attempt == MAX_RETRIES - 1:
                        failed += 1
                    else:
                        time.sleep(1.2 * (attempt + 1))
            done += 1
            if done % 60 == 0:
                print(f"    {done}/{total} queries, {len(logs):,} in / {len(out_logs):,} out")
    if failed:
        print(f"  WARNING: {failed}/{total} queries failed; counts are a floor")
    return logs, out_logs, {"provider": "rpc", "endpoint": url, "from_block": start,
                            "to_block": tip, "failed_queries": failed, "total_queries": total}


def decode_out(logs):
    """Group raw Transfer logs by sender: money leaving the seller."""
    by = defaultdict(lambda: {"settlements": 0, "usdc": 0.0})
    for lg in logs:
        try:
            frm = "0x" + lg["topics"][1][-40:]
            val = int(lg["data"], 16) / 1e6
        except Exception:
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

    stamp = time.strftime("%Y-%m-%d")
    os.makedirs(HIST, exist_ok=True)
    existing = os.path.join(HIST, f"settlements_{stamp}.json")
    if os.path.exists(existing):
        try:
            prior_hours = json.load(open(existing)).get("hours", 0)
        except Exception:
            prior_hours = 0
        if prior_hours > args.hours and not args.force:
            raise SystemExit(
                f"refusing to overwrite: {os.path.basename(existing)} already covers "
                f"{prior_hours}h and this run is only {args.hours}h. "
                f"A short test sweep must not replace a full day. Use --force to override.")
    json.dump({"date": stamp, "generated": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
               "hours": args.hours, "meta": meta, "by_address": per_addr},
              open(os.path.join(HIST, f"settlements_{stamp}.json"), "w"), indent=1)

    latest["settlement_source"] = f"Base {meta['provider']} ({meta['endpoint']}), USDC transfers to seller addresses"
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
