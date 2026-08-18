#!/usr/bin/env python3
"""Revenue leaderboard: who actually got paid, over 1, 7 and 30 days.

Built from the daily settlement tape in data/history/. Each file is one 24-hour
sweep of USDC transfers on Base to the payment address a service asks us to pay.

Windows are only as deep as the tape. With one day collected, the 24-hour column
is real and the 7 and 30-day columns say so rather than quietly reporting a
one-day number under a seven-day heading. They fill in as the daily job runs.

Writes data/leaderboard.json for the site.
"""
import glob, json, math, os, time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
HIST = os.path.join(DATA, "history")

# The zero and dead addresses are USDC burns/redemptions, never a seller's revenue.
# One seller once returned 0x0 as a payTo in a 402, so every burn on the chain got
# mapped to it and the headline read $138M. Transfers to these are excluded from
# settlement everywhere: the total, the rows, and the address->host map.
BURN_ADDRS = {"0x0000000000000000000000000000000000000000",
              "0x000000000000000000000000000000000000dead"}


def address_to_service():
    """Map each payment address to the service that asked us to pay it."""
    latest = json.load(open(os.path.join(DATA, "latest.json")))
    owners = defaultdict(list)
    for r in latest["origins"]:
        # The probe now records payTo as a {address: count} dict, and older runs
        # used a payto_addresses list. Read both, or every Base address silently
        # stops resolving to a host and the leaderboard shows bare 0x strings.
        addrs = r.get("payto")
        if isinstance(addrs, dict):
            addrs = list(addrs.keys())
        elif not isinstance(addrs, list):
            addrs = r.get("payto_addresses") or []
        for a in addrs:
            if a and a.lower() not in BURN_ADDRS:
                owners[a.lower()].append(r)
    out = {}
    for addr, rows in owners.items():
        # prefer the most brand-like domain among listings sharing a wallet
        def score(r):
            d = r["origin"].lower()
            noise = sum(t in d for t in ("vercel.app", "run.app", "onrender.com",
                                         "railway.app", "workers.dev", "-git-"))
            return (noise, d.count("-"), len(d))
        head = sorted(rows, key=score)[0]
        out[addr] = {
            "service": head.get("service") or head["origin"],
            "host": head["origin"],
            "grade": head.get("grade"),
            "shares": len(rows),
        }
    return out


def load_days():
    """Base settlement files only. The Solana files live under the same prefix
    but a different shape, so they are matched by their own name and loaded
    separately; globbing them in here would crash the Base aggregation."""
    days = []
    for f in sorted(glob.glob(os.path.join(HIST, "settlements_*.json"))):
        if "solana" in os.path.basename(f):
            continue
        try:
            d = json.load(open(f))
        except Exception:
            continue
        days.append(d)
    return days


def load_solana(n):
    """Per-host Solana inflow over the most recent n daily sweeps.

    Kept separate from the Base tape and merged only into the headline revenue,
    because outflow and payer concentration are not swept on Solana. Folding
    Solana inflow into demand-shape metrics computed from Base alone would
    quietly corrupt them, so those stay Base-measured and labelled.
    """
    files = sorted(f for f in glob.glob(os.path.join(HIST, "settlements_solana_*.json")))
    files = files[-n:] if n <= len(files) else files
    per = defaultdict(lambda: {"usdc": 0.0, "settlements": 0, "payers": 0, "days": 0})
    dates = []
    for f in files:
        try:
            d = json.load(open(f))
        except Exception:
            continue
        dates.append(d.get("generated", "")[:10])
        for w, v in (d.get("by_wallet") or {}).items():
            host = v.get("host") or "?"
            if host == "?":
                continue
            p = per[host]
            p["usdc"] += v.get("usdc", 0.0)
            p["settlements"] += v.get("settlements", 0)
            p["payers"] = max(p["payers"], v.get("unique_payers", 0))
            p["days"] += 1
    return per, len(files)



def demand_label(payers, repeat, top1, hhi):
    """Plain-language read on whether the money looks like independent demand.

    Thresholds are stated rather than hidden in a score, because the components
    matter more than any number we could roll them into. A high concentration is
    a flag and not a verdict: one large legitimate customer looks identical to a
    wallet paying itself, and this measurement cannot tell them apart.
    """
    if not payers:
        return None
    if top1 is not None and (top1 >= 0.9 or (hhi is not None and hhi >= 0.8)):
        return "one wallet"
    if payers <= 5:
        return "thin"
    if payers >= 20 and (top1 is None or top1 <= 0.35) and (hhi is None or hhi <= 0.15):
        return "broad"
    return "mixed"


def organic_score(payers, repeat, top1, hhi, settlements):
    """Organic Demand Score, 0 to 100: how much of a service's traffic looks like
    independent buyers rather than one wallet.

    Three components, each returned alongside the total so the number can always
    be taken apart:

      breadth    40   how many distinct wallets paid, on a log scale
      dispersion 40   how evenly the dollars fell across them, from the HHI
      repeat     20   what share of payers came back

    Deliberately NOT included: revenue, and whether money flowed straight back
    out. A big number is not evidence of real demand, and outflow is as often
    cost of goods as it is recycling, so `circular` and `payout_pct` stay beside
    this score as separate flags rather than being folded into it.

    This measures shape, not honesty. One large genuine customer and a wallet
    paying itself produce the same low score, and nothing on chain separates
    them.
    """
    if not payers:
        return None

    def clamp(x):
        return max(0.0, min(1.0, x))

    breadth = 40.0 * clamp(math.log10(payers) / 2.0)          # 100+ payers tops out

    conc = hhi
    if conc is None and top1 is not None:
        conc = top1 * top1                                     # crude floor from top1 alone
    dispersion = 40.0 * clamp((1.0 - conc) / 0.95) if conc is not None else None

    repeat_rate = (repeat / payers) if payers else 0.0
    repeats = 20.0 * clamp(repeat_rate / 0.30)                 # 30% returning tops out

    parts = {"breadth": round(breadth, 1),
             "dispersion": round(dispersion, 1) if dispersion is not None else None,
             "repeat": round(repeats, 1)}
    if dispersion is None:
        return {"score": None, "parts": parts, "confidence": "unknown",
                "note": "no concentration data for this window"}

    total = breadth + dispersion + repeats
    n = settlements or 0
    if n < 10 or payers < 3:
        conf = "low"
    elif n < 100:
        conf = "medium"
    else:
        conf = "high"
    return {"score": round(total), "parts": parts, "confidence": conf,
            "repeat_rate": round(repeat_rate, 3)}


def main():
    days = load_days()
    if not days:
        raise SystemExit("no settlement history yet; run sweep.py first")

    a2s = address_to_service()
    today = time.strftime("%Y-%m-%d")

    def window(n):
        """Aggregate the most recent n dated sweeps."""
        picked = days[-n:] if n <= len(days) else days
        agg = defaultdict(lambda: {"settlements": 0, "usdc": 0.0, "usdc_out": 0.0, "payers": 0,
                                   "repeat": 0, "top1": None, "top5": None, "hhi": None})
        for d in picked:
            for addr, v in d["by_address"].items():
                if addr.lower() in BURN_ADDRS:
                    continue  # burns/redemptions, not payments to any seller
                a = agg[addr.lower()]
                a["settlements"] += v["settlements"]
                a["usdc"] += v["usdc"]
                a["usdc_out"] += v.get("usdc_out", 0.0)
                a["payers"] = max(a["payers"], v["unique_payers"])
                a["repeat"] = max(a["repeat"], v.get("repeat_payers", 0))
                # concentration is a property of a day, so carry the worst one
                for k, src in (("top1", "top_payer_share"), ("top5", "top5_payer_share"),
                               ("hhi", "payer_hhi")):
                    x = v.get(src)
                    if x is not None:
                        a[k] = x if a[k] is None else max(a[k], x)
        return agg, len(picked), [d["date"] for d in picked]

    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
           "days_collected": len(days),
           "first_day": days[0]["date"], "last_day": days[-1]["date"],
           "windows": {}}

    for label, n in (("1d", 1), ("7d", 7), ("30d", 30)):
        agg, got, dates = window(n)
        rows = []
        for addr, v in agg.items():
            meta = a2s.get(addr, {})
            ods = organic_score(v["payers"], v["repeat"], v["top1"], v["hhi"],
                                v["settlements"])
            rows.append({
                "address": addr,
                "service": meta.get("service") or addr[:10] + "…",
                "host": meta.get("host", ""),
                "grade": meta.get("grade"),
                "shared_wallet": meta.get("shares", 1) > 1,
                "usdc": round(v["usdc"], 4),
                "usdc_out": round(v["usdc_out"], 4),
                "kept": round(v["usdc"] - v["usdc_out"], 4),
                "payout_pct": round(100 * v["usdc_out"] / v["usdc"], 1) if v["usdc"] else None,
                # A wallet that returns almost everything it receives is recycling
                # stakes rather than selling. That is worth flagging, not deducting:
                # outflow can equally be cost of goods or a treasury sweep.
                "circular": bool(v["usdc"] and (v["usdc_out"] / v["usdc"]) >= 0.9),
                "settlements": v["settlements"],
                "payers": v["payers"],
                "avg_ticket": round(v["usdc"] / v["settlements"], 6) if v["settlements"] else None,
                # The shape of demand, not just its size. A total cannot tell you
                # whether a thousand buyers or one wallet produced it.
                "repeat_payers": v["repeat"],
                "top_payer_share": v["top1"],
                "top5_payer_share": v["top5"],
                "payer_hhi": v["hhi"],
                "demand": demand_label(v["payers"], v["repeat"], v["top1"], v["hhi"]),
                # Organic Demand Score: the shape of the demand as one number,
                # with its components kept so it can always be taken apart.
                "organic_score": (ods or {}).get("score"),
                "organic_parts": (ods or {}).get("parts"),
                "organic_confidence": (ods or {}).get("confidence"),
                # Every shape metric above is measured on Base only. Revenue below
                # is made cross-chain; these stay Base-sampled and say so.
                "demand_measured_on": "base",
                "base_usdc": round(v["usdc"], 4),
                "chains": {"base": {"usdc": round(v["usdc"], 4), "settlements": v["settlements"]}},
            })

        # --- fold in Solana, the other 41% of the market -----------------------
        # Revenue and settlement counts are simply additive across chains and
        # exact. A host that settles on both now shows its true total; a host that
        # settles only on Solana appears for the first time.
        sol, _sdays = load_solana(n)
        by_host = {}
        for r in rows:
            if r.get("host"):
                by_host.setdefault(r["host"], r)
        for host, sv in sol.items():
            su = round(sv["usdc"], 4)
            if host in by_host:
                r = by_host[host]
                r["usdc"] = round(r["usdc"] + su, 4)
                r["settlements"] += sv["settlements"]
                r["kept"] = round(r["usdc"] - r["usdc_out"], 4)
                r["payout_pct"] = round(100 * r["usdc_out"] / r["usdc"], 1) if r["usdc"] else None
                r["avg_ticket"] = round(r["usdc"] / r["settlements"], 6) if r["settlements"] else None
                r["chains"]["solana"] = {"usdc": su, "settlements": sv["settlements"],
                                         "payers": sv["payers"]}
            else:
                ods = organic_score(sv["payers"], 0, None, None, sv["settlements"])
                rows.append({
                    "address": None, "service": host, "host": host,
                    "grade": (a2s.get("", {}) or {}).get("grade"),
                    "shared_wallet": False,
                    "usdc": su, "usdc_out": 0.0, "kept": su, "payout_pct": None,
                    "circular": False, "settlements": sv["settlements"],
                    "payers": sv["payers"],
                    "avg_ticket": round(su / sv["settlements"], 6) if sv["settlements"] else None,
                    "repeat_payers": 0, "top_payer_share": None, "top5_payer_share": None,
                    "payer_hhi": None,
                    "demand": demand_label(sv["payers"], 0, None, None),
                    "organic_score": (ods or {}).get("score"),
                    "organic_parts": (ods or {}).get("parts"),
                    "organic_confidence": (ods or {}).get("confidence"),
                    "demand_measured_on": "solana",
                    "base_usdc": 0.0,
                    "chains": {"solana": {"usdc": su, "settlements": sv["settlements"],
                                          "payers": sv["payers"]}},
                })
        rows.sort(key=lambda r: -r["usdc"])
        # The ranked list is services we can name. A payTo wallet that resolves to
        # no advertised endpoint (host is empty) is real settlement money but not a
        # reviewable service, so it is kept in the totals below and dropped from the
        # displayed ranking rather than shown as a bare hex address masquerading as
        # a service. Only fully unattributed rows are dropped; a Solana-only host
        # keeps its name.
        _ranked = [r for r in rows if r.get("host")]
        out["windows"][label] = {
            "days_wanted": n, "days_have": got, "complete": got >= n,
            "dates": dates,
            "total_usdc": round(sum(r["usdc"] for r in rows), 2),
            "total_kept": round(sum(r["kept"] for r in rows), 2),
            "total_settlements": sum(r["settlements"] for r in rows),
            "unattributed_usdc": round(sum(r["usdc"] for r in rows if not r.get("host")), 2),
            "rows": _ranked[:60],
        }

    json.dump(out, open(os.path.join(DATA, "leaderboard.json"), "w"), indent=1)

    w = out["windows"]["1d"]
    print(f"tape: {len(days)} day(s), {out['first_day']} to {out['last_day']}")
    print(f"24h: ${w['total_usdc']:,.2f} across {w['total_settlements']:,} settlements\n")
    print(f"{'':2} {'service':30s} {'received':>11s} {'payout':>8s}")
    for i, r in enumerate(w["rows"][:12], 1):
        pct = f"{r['payout_pct']:.0f}%" if r.get("payout_pct") is not None else "-"
        flag = "  circular" if r.get("circular") else ""
        print(f"{i:2d} {r['service'][:30]:30s} ${r['usdc']:>10,.2f} {pct:>8}{flag}")
    for label in ("7d", "30d"):
        ww = out["windows"][label]
        if not ww["complete"]:
            print(f"\n{label}: {ww['days_have']} of {ww['days_wanted']} days collected, still filling")


if __name__ == "__main__":
    main()
