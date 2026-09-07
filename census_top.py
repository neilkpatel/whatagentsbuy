#!/usr/bin/env python3
"""Who funds the top services? A tractable buyer-side census.

The full-market census (funders.py over all ~1,418 advertised payTo addresses)
rate-limits the free Base RPCs and stalls. This scopes the same on-chain scan to
the payTo wallets of the top-revenue services, which is one RPC group and the
more interesting slice anyway: are the biggest services in agentic commerce paid
by a broad crowd of real buyers, or by a handful of wallets?

Usage: python3 census_top.py [--days 7] [--top 60]
"""
import argparse
import collections
import json
import os

import funders

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--top", type=int, default=60)
    a = ap.parse_args()

    lb = json.load(open(os.path.join(HERE, "data", "leaderboard.json")))
    rows = lb["windows"]["30d"]["rows"][:a.top]
    addr_to_service = {r["address"].lower(): r.get("service") or r.get("host") or r["address"]
                       for r in rows if r.get("address")}
    addrs = list(addr_to_service.keys())
    print(f"scanning {len(addrs)} top-service payTo wallets over {a.days:g} days of Base USDC transfers\n")

    senders, tip, start = funders.scan(addrs, a.days)

    # aggregate the buyer side. s["to"] is a Counter of the recipient service
    # addresses this wallet paid, read straight from the on-chain transfers, so
    # the top recipient is verified fact, not inference.
    buyers = []
    for frm, s in senders.items():
        top_to, top_to_ct = (s["to"].most_common(1)[0] if s["to"] else (None, 0))
        buyers.append({"wallet": frm, "usdc": round(s["usdc"], 4), "payments": s["n"],
                       "distinct_services": len(s["to"]),
                       "top_recipient": top_to,
                       "top_recipient_service": addr_to_service.get(top_to),
                       "top_recipient_payments": top_to_ct})
    total_usdc = sum(b["usdc"] for b in buyers)
    total_pmts = sum(b["payments"] for b in buyers)

    print(f"buyers found: {len(buyers)} unique wallets")
    print(f"total into the top {len(addrs)} services: ${total_usdc:,.2f} across {total_pmts:,} payments\n")

    # concentration
    by_usd = sorted(buyers, key=lambda b: -b["usdc"])
    top5 = sum(b["usdc"] for b in by_usd[:5])
    top1 = by_usd[0]["usdc"] if by_usd else 0
    print("concentration:")
    print(f"  top buyer   : ${top1:,.2f} ({100*top1/total_usdc:.1f}% of all buyer dollars)" if total_usdc else "  n/a")
    print(f"  top 5 buyers: ${top5:,.2f} ({100*top5/total_usdc:.1f}%)\n" if total_usdc else "")

    print("biggest buyers by dollars:")
    for b in by_usd[:12]:
        print(f"  {b['wallet']}  ${b['usdc']:>11,.2f}  {b['payments']:>5} pmts  {b['distinct_services']:>3} services")

    print("\nbroadest buyers (most distinct services, the keepalive/crawler signature):")
    for b in sorted(buyers, key=lambda b: -b["distinct_services"])[:12]:
        print(f"  {b['wallet']}  {b['distinct_services']:>3} services  {b['payments']:>5} pmts  ${b['usdc']:>10,.4f}")

    out = {"days": a.days, "top_services": len(addrs), "buyers": len(buyers),
           "total_usdc": round(total_usdc, 2), "total_payments": total_pmts,
           "top_buyer_share": round(top1 / total_usdc, 4) if total_usdc else None,
           "top5_share": round(top5 / total_usdc, 4) if total_usdc else None,
           "buyers_detail": sorted(buyers, key=lambda b: -b["usdc"])}
    json.dump(out, open(os.path.join(HERE, "data", "census_top.json"), "w"), indent=1)
    print("\nwrote data/census_top.json")


if __name__ == "__main__":
    main()
