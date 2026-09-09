#!/usr/bin/env python3
"""The buyer side of the tape: who actually pays this market.

Every surface on this site measures supply. Sellers, prices, delivery, accuracy.
The question an agent developer actually has is whether anyone like them is
buying, and nothing here could answer it, because the settlement archive keys on
the seller and throws the payer addresses away (`decode()` in sweep.py: "The
addresses themselves are not stored, only the shape"). That was a deliberate,
privacy-conscious choice and it is why 36 days of tape cannot be mined for this.
Buyer identity has to be retained from here forward.

WHY THE PAYER IN A TRANSFER LOG IS THE REAL BUYER (verified on chain 2026-09-09).
This is the fact the whole dataset rests on, and it was not safe to assume. x402
settles through EIP-3009 `transferWithAuthorization` (selector 0xe3ee160e): the
buyer signs an authorization off-chain and a facilitator submits the transaction
and pays the gas. Decoding a live settlement confirms the binding:

    calldata word0 == log.topics[1] (payer)     word2 == log.data (value)
    calldata word1 == log.topics[2] (seller)

and `tx.from` (the submitter) is a DIFFERENT address every time. Across 25
settlements into one seller there was 1 distinct payer and 13 distinct relayers.
So `log.from` is the buyer, not a facilitator's pooled treasury, and buyers never
submit a transaction themselves (our own wallet is the same shape: nonce 0, zero
ETH). Had this gone the other way, a "buyer census" would have been a census of
infrastructure.

THE MIRROR OF THE $138M BUG. On 8/18 a seller advertised `0x0` as its payTo, so
every USDC burn on chain mapped to it and the headline read $138M. The same trap
exists in reverse on this side: the zero address as SENDER is a USDC *mint*, not
a wallet buying anything. Left in, the largest buyer in this market would be the
mint. `_MINTS` is filtered here, and there is a test.

WHAT THIS UNIVERSE IS. These logs are already filtered to the payTo addresses the
registry advertises, so a "buyer" here means a wallet that paid a listed x402
seller. It is not every USDC wallet on Base, and the saved file says so.

FRAMING. Sellers advertised their payTo. Buyers advertised nothing. This module
records shapes, never verdicts: how many sellers a wallet pays, how concentrated
its spending is, how long it has been active. Calling a wallet a keepalive bot is
an inference, and the retired `circular` wash flag is the standing lesson in what
happens when an inference gets published as a fact.
"""
import argparse
import json
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
HIST = os.path.join(DATA, "history")

# The zero address as a SENDER is a mint. 0x...dead is a burn convention. Neither
# is a wallet that bought anything. See the module docstring.
_MINTS = {"0x0000000000000000000000000000000000000000",
          "0x000000000000000000000000000000000000dead"}

METHOD = ("USDC Transfer logs on Base whose recipient is a payTo address advertised in the "
          "CDP registry, grouped by payer. x402 settles via EIP-3009, so the log sender is the "
          "buyer that signed the authorization, not the facilitator that submitted it. Mints "
          "(sender 0x0) are excluded. A 'buyer' is a wallet that paid a listed x402 seller.")


def decode_buyers(logs, seller_addrs=frozenset()):
    """Group raw Transfer logs by PAYER, keeping the seller edge list.

    The edge list is the point. A payer count cannot distinguish one wallet
    paying a thousand times from a thousand wallets paying once, and a total
    cannot distinguish a buyer that uses three services it needs from one that
    touches four hundred it does not. Keeping (buyer -> seller) pairs is what
    makes cohort and co-movement analysis possible later without re-sweeping.

    `seller_addrs` is the set of advertised payTo addresses. A wallet in it that
    also pays other sellers is a reseller passing through cost of goods, not a
    pure buyer, and conflating the two is exactly the error that produced the
    retired wash flag (clusterprotocol read as a washer when it was a reseller).
    It is flagged, never filtered: dropping resellers would hide real demand.
    """
    by = defaultdict(lambda: {"settlements": 0, "usdc": 0.0,
                              "sellers": defaultdict(lambda: {"n": 0, "usdc": 0.0}),
                              "first_block": None, "last_block": None})
    for lg in logs:
        try:
            frm = ("0x" + lg["topics"][1][-40:]).lower()
            to = ("0x" + lg["topics"][2][-40:]).lower()
            val = int(lg["data"], 16) / 1e6
            blk = int(lg["blockNumber"], 16)
        except Exception:
            continue
        if frm in _MINTS:
            continue  # a mint is not a buyer; see the $138M mirror in the docstring
        if to in _MINTS:
            continue  # burn/redemption, not a payment to any seller
        r = by[frm]
        r["settlements"] += 1
        r["usdc"] += val
        sr = r["sellers"][to]
        sr["n"] += 1
        sr["usdc"] += val
        r["first_block"] = blk if r["first_block"] is None else min(r["first_block"], blk)
        r["last_block"] = blk if r["last_block"] is None else max(r["last_block"], blk)

    sellers_lc = {a.lower() for a in seller_addrs}
    out = {}
    for k, v in by.items():
        amounts = sorted((s["usdc"] for s in v["sellers"].values()), reverse=True)
        total = sum(amounts) or 1.0
        shares = [a / total for a in amounts]
        out[k] = {
            "settlements": v["settlements"],
            "usdc": round(v["usdc"], 6),
            "sellers": len(v["sellers"]),
            # the buyer-side mirror of top_payer_share: how much of this wallet's
            # money went to the single seller it spends most with
            "top_seller_share": round(shares[0], 4) if shares else None,
            "top5_seller_share": round(sum(shares[:5]), 4) if shares else None,
            "seller_hhi": round(sum(x * x for x in shares), 4) if shares else None,
            "repeat_sellers": sum(1 for s in v["sellers"].values() if s["n"] > 1),
            "first_block": v["first_block"],
            "last_block": v["last_block"],
            "span_blocks": (v["last_block"] - v["first_block"])
                           if v["first_block"] is not None else None,
            # a wallet that is itself an advertised seller is a reseller, not a
            # pure buyer. Flagged, never filtered.
            "is_seller": k in sellers_lc,
            "sellers_paid": {s: {"n": d["n"], "usdc": round(d["usdc"], 6)}
                             for s, d in sorted(v["sellers"].items(),
                                                key=lambda kv: -kv[1]["usdc"])},
        }
    return out


def summarize(rows):
    """Headline shape of a buyer day. Facts only, no classification."""
    if not rows:
        return {}
    spend = sorted((v["usdc"] for v in rows.values()), reverse=True)
    total = sum(spend) or 1.0
    n = len(rows)
    return {
        "buyers": n,
        "usdc": round(sum(spend), 6),
        "settlements": sum(v["settlements"] for v in rows.values()),
        "resellers": sum(1 for v in rows.values() if v["is_seller"]),
        "one_seller_buyers": sum(1 for v in rows.values() if v["sellers"] == 1),
        "multi_seller_buyers": sum(1 for v in rows.values() if v["sellers"] >= 5),
        "top_buyer_share": round(spend[0] / total, 4),
        "top10_buyer_share": round(sum(spend[:10]) / total, 4),
        "buyer_hhi": round(sum((a / total) ** 2 for a in spend), 4),
        "median_spend": round(spend[n // 2], 6),
    }


def load(date):
    p = os.path.join(HIST, f"buyers_{date}.json")
    return json.load(open(p)) if os.path.exists(p) else None


def main():
    ap = argparse.ArgumentParser(description="Inspect a saved buyer day. Collection happens in sweep.py.")
    ap.add_argument("--date", help="YYYY-MM-DD (default: the most recent saved day)")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--wallet", help="everything one wallet paid that day")
    a = ap.parse_args()

    if not os.path.isdir(HIST):
        raise SystemExit("no history directory yet")
    days = sorted(f[len("buyers_"):-len(".json")] for f in os.listdir(HIST)
                  if f.startswith("buyers_") and f.endswith(".json"))
    if not days:
        raise SystemExit("no buyer days saved yet. Run sweep.py to collect one.")
    date = a.date or days[-1]
    doc = load(date)
    if not doc:
        raise SystemExit(f"no buyer file for {date}. Have: {', '.join(days)}")
    rows = doc.get("by_buyer") or {}

    if a.wallet:
        w = a.wallet.lower()
        r = rows.get(w)
        if not r:
            print(f"{w} paid no listed seller on {date}")
            return 0
        print(f"{w}  ({'reseller, also a listed seller' if r['is_seller'] else 'buyer'})")
        print(f"  {r['settlements']:,} payments  ${r['usdc']:,.4f}  {r['sellers']} distinct sellers")
        print(f"  top-seller share {r['top_seller_share']:.1%}  active across {r['span_blocks']:,} blocks\n")
        for s, d in list(r["sellers_paid"].items())[:40]:
            print(f"    {d['n']:>6} x  ${d['usdc']:>11,.4f}  {s}")
        return 0

    s = doc.get("summary") or summarize(rows)
    print(f"buyer tape {date}   ({doc.get('hours')}h window)\n")
    print(f"  {s['buyers']:,} distinct buyers  ${s['usdc']:,.2f}  {s['settlements']:,} payments")
    print(f"  {s['one_seller_buyers']:,} paid exactly one seller | {s['multi_seller_buyers']:,} paid 5+")
    print(f"  {s['resellers']:,} are themselves listed sellers (resellers, flagged not filtered)")
    print(f"  top buyer {s['top_buyer_share']:.1%} of dollars | top 10 {s['top10_buyer_share']:.1%}"
          f" | HHI {s['buyer_hhi']:.4f}  | median spend ${s['median_spend']:,.4f}\n")
    top = sorted(rows.items(), key=lambda kv: -kv[1]["usdc"])[:a.top]
    print(f'{"wallet":<44}{"sellers":>8}{"pays":>8}{"USDC":>13}{"top-sellr":>11}  flag')
    print("-" * 92)
    for w, r in top:
        print(f'{w:<44}{r["sellers"]:>8}{r["settlements"]:>8}{r["usdc"]:>13,.4f}'
              f'{(r["top_seller_share"] or 0):>10.1%}  {"reseller" if r["is_seller"] else ""}')
    print(f"\n{len(rows):,} buyers total. --wallet <addr> for one wallet's full edge list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
