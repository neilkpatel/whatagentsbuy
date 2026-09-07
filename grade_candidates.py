#!/usr/bin/env python3
"""Surface a CURATED, WIDE list of endpoints worth buying + testing, on demand.

The universe is the WHOLE x402 market, not the 6 objective-price categories. We do
not know which x402 use cases will matter, so coverage has to be broad: LLM
inference, image/video, web search, onchain data, news, SEC filings, enrichment,
prediction markets, and the long tail in "other" are all worth testing. Two grades
apply:

  DELIVERY  (every category): pay it, did it return a usable response matching the
            fields it promised? This is the universal test and most of the value.
  ACCURACY  (only the 6 with an objective ground truth): is the returned NUMBER
            right vs a primary source? An extra grade on top of delivery.

Grading is manual and curated: money never leaves on a schedule, and most
endpoints are not worth paying to test (spam, dupes, priced-out, unsafe). This
walks the whole market down to a diverse, ranked shortlist and says why each
survived or was cut, so the spend is visible before any USDC moves.

    python3 categorize.py        # refresh the map first (free)
    python3 grade_candidates.py  # this list  (free, local only)

Reads local data only; makes no calls and spends nothing.
"""
import argparse, json, os
from urllib.parse import urlparse
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
PUB = os.path.join(HERE, "public", "api")
ACCURACY_CATS = {"crypto-price", "stock-price", "fx-rate", "gas-price", "wallet-balance", "weather"}


def _load(p, d):
    try:
        return json.load(open(p))
    except Exception:
        return d


def host_of(u):
    try:
        return urlparse(u if "://" in u else "https://" + u).netloc.lower().replace("www.", "")
    except Exception:
        return (u or "").lower()


def tested_hosts():
    """Every host we have ALREADY paid + tested, from receipts (delivery OR
    accuracy) and the accuracy lab's log. Dropping these is correct: we graded
    them, so they are not 'unlooked-at'."""
    hosts = set()
    rp = os.path.join(DATA, "receipts", "receipts.jsonl")
    if os.path.exists(rp):
        for line in open(rp):
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line).get("seller")
            except Exception:
                continue
            h = s.get("host") if isinstance(s, dict) else s
            if h:
                hosts.add(h.lower())
    for u in _load(os.path.join(DATA, "tested_log.json"), {}):
        hosts.add(host_of(u))
    return hosts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-price", type=float, default=0.05, help="skip endpoints pricier than this to test")
    ap.add_argument("--per-cat", type=int, default=6, help="how many to show per category")
    a = ap.parse_args()

    cmap = _load(os.path.join(DATA, "categories_map.json"), {}).get("categories", {})
    done = tested_hosts()
    pf = _load(os.path.join(PUB, "preflight.json"), {}).get("sellers", {})
    lb_rows = _load(os.path.join(PUB, "leaderboard.json"), {}).get("rows", [])
    lb = {r["host"]: r for r in lb_rows if r.get("host")}
    first_seen = _load(os.path.join(DATA, "first_seen.json"), {})
    graded_wallets = {(r.get("address") or "").lower() for r in lb_rows
                      if r.get("host") in done and r.get("address")}

    def fsd(url):
        return (first_seen.get(url) or first_seen.get(host_of(url)) or "")[:10]

    coverage = {}       # cat -> (tested, total)
    picks = defaultdict(list)
    skips = defaultdict(int)
    funnel = defaultdict(int)

    for cat, d in cmap.items():
        sellers = d.get("sellers") or []
        tested_in_cat = sum(1 for s in sellers if host_of(s["url"]) in done)
        coverage[cat] = (tested_in_cat, len(sellers))
        for s in sellers:
            url = s["url"]
            h = host_of(url)
            funnel["universe"] += 1
            if h in done:
                funnel["already tested"] += 1
                continue
            funnel["never tested"] += 1
            v = pf.get(h)
            price = s.get("price_usdc")
            row = lb.get(h)
            if not v or v.get("light") == "gray":
                skips["not reachable / no live verdict"] += 1
                continue
            if v.get("light") == "red":
                skips["preflight ABORT (unsafe to pay)"] += 1
                continue
            if not price or price <= 0 or price > a.max_price:
                skips[f"priced out (> ${a.max_price})"] += 1
                continue
            if row and (row.get("address") or "").lower() in graded_wallets:
                skips["same wallet as an already-tested seller"] += 1
                continue
            settled = (row or {}).get("usdc_received") or 0
            payers = (row or {}).get("paying_wallets") or 0
            new = fsd(url) > "2026-08-17"
            score = (settled or 0) + payers * 5 + (40 if new else 0) + (5 if price <= 0.003 else 0)
            if settled:
                why = f"${settled:,.0f} settled, {payers} buyers"
            elif new:
                why = f"new {fsd(url)}"
            else:
                why = "untested, no demand yet"
            picks[cat].append(dict(host=h, price=price, light=v.get("light"),
                                   conf=v.get("confidence"), settled=settled, new=new,
                                   score=score, why=why, accuracy=cat in ACCURACY_CATS))

    # ---- coverage: where the gaps are (the whole point of going wide) ----
    print("=== MARKET COVERAGE (tested / total per category) ===")
    for cat, (t, n) in sorted(coverage.items(), key=lambda x: (x[1][0] / max(x[1][1], 1))):
        bar = "*" * int(20 * t / max(n, 1))
        star = " [accuracy]" if cat in ACCURACY_CATS else ""
        print(f"  {cat:18} {t:4}/{n:<5} {100*t/max(n,1):4.0f}%  {bar}{star}")
    tt = sum(t for t, _ in coverage.values())
    tn = sum(n for _, n in coverage.values())
    print(f"  {'TOTAL':18} {tt:4}/{tn:<5} {100*tt/max(tn,1):4.0f}% of the whole market tested")
    print()
    print("=== FUNNEL ===")
    print(f"  universe {funnel['universe']} · already tested -{funnel['already tested']} · never tested {funnel['never tested']}")
    for r, n in sorted(skips.items(), key=lambda x: -x[1]):
        print(f"    cut: {r:42} -{n}")
    ncand = sum(len(v) for v in picks.values())
    est = sum(min(p["price"] * 1.25 + 0.0001, a.max_price) for v in picks.values() for p in v)
    print(f"  ---> worth-testing candidates: {ncand}   (~${est:.2f} to test them all)")

    # ---- candidates: least-covered categories first, so we go WIDE ----
    print("\n=== CURATED CANDIDATES (least-covered categories first) ===")
    order = sorted(coverage, key=lambda c: (coverage[c][0] / max(coverage[c][1], 1)))
    for cat in order:
        rows = sorted(picks.get(cat, []), key=lambda x: -x["score"])
        if not rows:
            continue
        grade = "delivery+accuracy" if cat in ACCURACY_CATS else "delivery"
        print(f"\n{cat}  ({len(rows)} candidates · grade: {grade})")
        for p in rows[:a.per_cat]:
            tag = "NEW " if p["new"] else "    "
            print(f"  {tag}{p['host'][:38]:38} ${p['price']:.4f}  {p['light']}/{p['conf']:9} {p['why']}")


if __name__ == "__main__":
    main()
