#!/usr/bin/env python3
"""Market-wide daily series: what settled, and how much of it looks like demand.

The tape in data/history/ is per-seller-address. This rolls it up into one
series for the whole x402 market on Base, and splits each day's dollars into
the part that came from many independent buyers and the part that did not.

WHY THIS EXISTS
Everyone quoting "x402 volume" quotes a gross number. A gross number cannot
tell you whether anyone wanted the product: one wallet paying itself a million
times and a million wallets paying once look identical in a total. This splits
them, using the same thresholds the per-service leaderboard already uses.

THE SPLIT, stated exactly, because it is an editorial claim
One test, on the SHAPE OF DEMAND:

  concentrated   one wallet supplied >= 90% of that seller's dollars that day
                 (`top_payer_share >= 0.90`). One relationship is not a market.

Organic volume is what is left.

CIRCULAR IS A FLAG, NOT A SUBTRACTION, and this was got wrong once already.
Folding outflow into the headline scored the market 1% organic and knocked out
the single best example of distributed demand in the tape: a seller with 688
paying wallets, 651 of them returning, and a top payer supplying 1% of the
dollars, removed solely because it passes through roughly what it takes in.
Outflow is as often cost of goods as it is recycling, and a reseller is
indistinguishable from a recycler on this test, so `circular_usdc` rides
alongside the split as a caveat the reader can apply, never inside it. This
matches organic_score() in leaderboard.py, which excludes outflow for the same
reason, so the market number and the per-service flags cannot disagree.

DOLLARS AND PAYMENTS DIVERGE, AND THAT IS THE FINDING
Concentration is far heavier in transaction COUNT than in dollars: one spam-
volume wallet can be 96% of the payments and 13% of the money. Both are
reported, because either alone misleads.

WHAT THIS IS NOT
This measures SHAPE, not honesty, and the distinction is the whole reason the
wording on the page stays neutral. A single large legitimate customer and a
wallet paying itself produce identical concentration; a reseller passing through
cost of goods and a wallet recycling funds produce identical outflow. Nothing on
chain separates them. So the residual is reported as "concentrated or circular",
which is a fact about the shape, and never as "wash" or "fake", which would be
an accusation this data cannot support. See the verify-before-accusing rule.

TRUST
A day is `trusted` only if its meta reports zero failed log-range queries. Days
swept before 2026-08-21 mostly do not qualify: a failed range is not random, it
is the HIGH-VOLUME range (volume is what makes a getLogs response too large),
so a 4% query-failure rate cost half the transaction count on 8/21. Untrusted
days stay in the series, flagged, and are excluded from every headline.

Usage:
  python3 market.py            # -> data/market.json
  python3 market.py --show
"""
import argparse, glob, json, os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
HIST = os.path.join(DATA, "history")
OUT = os.path.join(DATA, "market.json")

CONCENTRATION_MAX = 0.90   # one wallet supplying this share stops being a market
PAYOUT_MAX = 0.90          # sending back this much is not net demand

# Transfers to these are USDC burns/redemptions, not payments. One seller
# advertised 0x0 as its payTo, so every burn on chain mapped to it and 8/18
# archived a $138,564,409 phantom. The pipeline fix came after that file was
# written, so the bad row is still in the tape and has to be dropped on READ,
# not merely on write.
BURN_ADDRS = {"0x0000000000000000000000000000000000000000",
              "0x000000000000000000000000000000000000dead"}

# `top_payer_share` was only added to the sweep on 2026-08-11. Without it the
# concentration test cannot fire, and treating that as "passed the test" reads
# a schema gap as evidence of organic demand: 8/04 scored a false 100%. A day
# is only splittable if most of its dollars carry the field.
MIN_CLASSIFIABLE = 0.80


def day_files():
    """Base settlement files only. Solana rides in a separate tape."""
    for f in sorted(glob.glob(os.path.join(HIST, "settlements_*.json"))):
        if "solana" in os.path.basename(f):
            continue
        yield f


def classify(rec):
    """Split one address-day into organic / concentrated / circular.

    Returns (bucket, usdc, settlements). A day can trip both tests; it is
    attributed to `concentrated` first so the two buckets never double-count
    and always sum back to the reported total.
    """
    usd = float(rec.get("usdc") or 0.0)
    txs = int(rec.get("settlements") or 0)
    if usd <= 0:
        return None, 0.0, txs

    top1 = rec.get("top_payer_share")

    # The split needs the concentration field. Without it the day is not
    # splittable: absence of a measurement is not evidence of organic demand.
    # (`top_payer_share` only entered the sweep on 2026-08-11.)
    if top1 is None:
        return "unclassified", usd, txs

    if top1 >= CONCENTRATION_MAX:
        return "concentrated", usd, txs
    return "organic", usd, txs


def is_circular(rec):
    """Seller passed >= PAYOUT_MAX of what it took straight back out.

    Reported BESIDE the split, never inside it. See the module docstring: a
    reseller paying cost of goods and a wallet recycling funds are identical
    here, so this cannot carry an accusation on its own.
    """
    usd = float(rec.get("usdc") or 0.0)
    out = rec.get("usdc_out")
    return bool(usd and out is not None and (float(out) / usd) >= PAYOUT_MAX)


def build():
    days = []
    for f in day_files():
        d = json.load(open(f))
        meta = d.get("meta") or {}
        by = d.get("by_address") or {}


        tot_u, tot_t = 0.0, 0
        buckets = {"organic": [0.0, 0], "concentrated": [0.0, 0],
                   "unclassified": [0.0, 0]}

        circ_u, circ_t = 0.0, 0
        sellers = 0
        organic_sellers = 0
        relationships = 0

        for addr, rec in by.items():
            if addr.lower() in BURN_ADDRS:
                continue          # burn/redemption, never a payment to a seller
            b, usd, txs = classify(rec)
            tot_u += usd
            tot_t += txs
            if usd > 0:
                sellers += 1
            # unique_payers is per-seller. The tape stores payer COUNTS, never
            # payer addresses, so the same wallet buying from three sellers is
            # counted three times. Reported as relationships, never as buyers.
            relationships += int(rec.get("unique_payers") or 0)
            if b:
                buckets[b][0] += usd
                buckets[b][1] += txs
                if b == "organic":
                    organic_sellers += 1
            if is_circular(rec):
                circ_u += usd
                circ_t += txs

        fq = meta.get("failed_queries")
        unclass = buckets["unclassified"][0]
        classifiable = (1 - unclass / tot_u) if tot_u else 0.0
        # Two independent ways a day can be untrustworthy, and they are not the
        # same thing: the sweep may have lost ranges (undercount), or the file
        # may predate the fields the split needs (unsplittable).
        swept_clean = (fq == 0)
        splittable = classifiable >= MIN_CLASSIFIABLE
        trusted = swept_clean and splittable
        days.append({
            "date": d.get("date"),
            "basis": meta.get("basis", "rolling_24h"),
            "backfilled": bool(meta.get("backfilled")),
            "trusted": trusted,
            "failed_queries": fq,
            "total_queries": meta.get("total_queries"),
            "reported_usdc": round(tot_u, 2),
            "reported_txs": tot_t,
            "organic_usdc": round(buckets["organic"][0], 2),
            "organic_txs": buckets["organic"][1],
            "concentrated_usdc": round(buckets["concentrated"][0], 2),
            "concentrated_txs": buckets["concentrated"][1],
            "circular_usdc": round(circ_u, 2),
            "circular_txs": circ_t,
            "unclassified_usdc": round(unclass, 2),
            "classifiable_share": round(classifiable, 4),
            "swept_clean": swept_clean,
            "splittable": splittable,
            "organic_share": (round(buckets["organic"][0] / tot_u, 4)
                              if tot_u and splittable else None),
            # The dollar share and the payment share are different stories and
            # both get published; one spam wallet can dominate counts while
            # barely moving the money.
            "organic_tx_share": (round(buckets["organic"][1] / tot_t, 4)
                                 if tot_t and splittable else None),
            "circular_share": (round(circ_u / tot_u, 4) if tot_u else None),
            "sellers_paid": sellers,
            "organic_sellers": organic_sellers,
            "payer_relationships": relationships,
        })

    days.sort(key=lambda x: x["date"])
    trusted_days = [d for d in days if d["trusted"]]

    def agg(sel):
        r = sum(d["reported_usdc"] for d in sel)
        o = sum(d["organic_usdc"] for d in sel)
        return {
            "days": len(sel),
            "reported_usdc": round(r, 2),
            "organic_usdc": round(o, 2),
            "reported_txs": sum(d["reported_txs"] for d in sel),
            "organic_txs": sum(d["organic_txs"] for d in sel),
            "organic_share": round(o / r, 4) if r else None,
            "circular_usdc": round(sum(d["circular_usdc"] for d in sel), 2),
        }

    latest = trusted_days[-1] if trusted_days else None
    out = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "method": {
            "concentration_max": CONCENTRATION_MAX,
            "payout_max": PAYOUT_MAX,
            "basis": "UTC calendar days",
            "source": "Base mainnet USDC Transfer logs to seller addresses read from "
                      "each service's own payment challenge",
            "note": "Organic excludes address-days where one wallet supplied 90%+ of the "
                    "dollars, or where the seller sent 90%+ straight back out. This is a "
                    "measurement of shape, not of honesty: one large genuine customer and "
                    "a wallet paying itself are identical on chain.",
        },
        "latest_trusted_day": latest,
        "last_7d": agg(trusted_days[-7:]),
        "last_30d": agg(trusted_days[-30:]),
        "days_total": len(days),
        "days_trusted": len(trusted_days),
        "days_untrusted": [d["date"] for d in days if not d["trusted"]],
        "series": days,
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    m = build()
    if args.show:
        print(f"{'date':<12}{'basis':<12}{'ok':<4}{'reported':>13}{'organic':>13}{'org%':>7}{'txs':>12}")
        for d in m["series"]:
            share = f"{100*d['organic_share']:.0f}%" if d["organic_share"] is not None else "-"
            print(f"{d['date']:<12}{d['basis']:<12}{'Y' if d['trusted'] else 'n':<4}"
                  f"${d['reported_usdc']:>12,.0f}${d['organic_usdc']:>12,.0f}{share:>7}"
                  f"{d['reported_txs']:>12,}")
        print(f"\ntrusted {m['days_trusted']}/{m['days_total']} days")
        if m["days_untrusted"]:
            print(f"untrusted (excluded from headlines): {', '.join(m['days_untrusted'])}")
        return

    json.dump(m, open(OUT, "w"), indent=1)
    print(f"wrote {OUT}: {m['days_trusted']}/{m['days_total']} trusted days")
    if m["latest_trusted_day"]:
        l = m["latest_trusted_day"]
        print(f"latest trusted {l['date']}: ${l['reported_usdc']:,.0f} reported, "
              f"${l['organic_usdc']:,.0f} organic "
              f"({100*l['organic_share']:.0f}%)" if l["organic_share"] else "")


if __name__ == "__main__":
    main()
