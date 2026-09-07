#!/usr/bin/env python3
"""Buy the BTC/USD price from many sellers and grade it against live exchange spot.

There is no single true crypto price, so the reference is a PRIMARY source, the
median of Coinbase and Kraken spot captured at the same moment, not another price
API (that would just measure agreement between vendors). What we measure is
dispersion (how far each seller is from live exchange spot) and staleness (does
it timestamp, how old), not "accuracy vs the one true price."

Every call is capped and the raw response is archived by conform, so the price
extraction can be re-audited offline without re-paying. Run:
  python3 price_shootout.py --limit 15 --budget 0.20
"""
import argparse
import json
import os
import time
import urllib.request
from urllib.parse import urlparse

import conform

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def exchange_ref():
    """Median of Coinbase and Kraken BTC/USD spot. A primary reference, not an API vendor."""
    refs = {}
    try:
        with urllib.request.urlopen("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=15) as r:
            refs["coinbase"] = float(json.load(r)["data"]["amount"])
    except Exception as e:
        print(f"  coinbase ref failed: {e}")
    try:
        with urllib.request.urlopen("https://api.kraken.com/0/public/Ticker?pair=XBTUSD", timeout=15) as r:
            res = json.load(r)["result"]
            refs["kraken"] = float(res[list(res)[0]]["c"][0])
    except Exception as e:
        print(f"  kraken ref failed: {e}")
    vals = sorted(refs.values())
    med = (vals[len(vals) // 2] if len(vals) % 2 else (vals[0] + vals[1]) / 2) if vals else None
    return med, refs


_PRICE_KEYS = {"price", "last", "usd", "close", "rate", "spot", "value", "current_price",
               "priceusd", "price_usd", "lastprice", "usdprice", "amount", "quote"}
_TS_KEYS = {"timestamp", "time", "asof", "as_of", "updated", "updatedat", "date", "datetime", "lastupdated"}


def find_price(obj, ref):
    """Best BTC-price candidate from a response. Prefer a named price field in a
    plausible range; else the number closest to the live reference within 25%.
    Returns (value, path) or (None, why)."""
    named, anynum = [], []

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower().replace("_", "").replace(" ", "")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if 1000 < v < 10_000_000:
                        anynum.append((float(v), f"{path}.{k}"))
                        if kl in _PRICE_KEYS or any(pk in kl for pk in ("price", "usd")):
                            named.append((float(v), f"{path}.{k}"))
                elif isinstance(v, str):
                    try:
                        fv = float(v.replace(",", "").lstrip("$"))
                        if 1000 < fv < 10_000_000:
                            anynum.append((fv, f"{path}.{k}"))
                            if kl in _PRICE_KEYS or any(pk in kl for pk in ("price", "usd")):
                                named.append((fv, f"{path}.{k}"))
                    except (ValueError, AttributeError):
                        pass
                else:
                    walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")

    walk(obj, "")
    if ref:
        pool = named or anynum
        near = [(v, p) for v, p in pool if abs(v - ref) / ref <= 0.25]
        if near:
            return min(near, key=lambda x: abs(x[0] - ref))
        if named:                       # a named price, but far from spot -> a real miss
            return min(named, key=lambda x: abs(x[0] - ref))
    if named:
        return named[0]
    if anynum:
        return anynum[0]
    return None, "no plausible BTC price field in response"


def find_ts_age(obj):
    """Best-effort staleness: find a timestamp and return its age in seconds, or None."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower().replace("_", "")
                if kl in _TS_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
                    t = float(v)
                    if t > 1e12:
                        t /= 1000.0            # ms -> s
                    if 1.6e9 < t < 2.0e9:
                        found.append(t)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    if not found:
        return None
    now = time.time()
    return round(now - max(found), 1)


def btc_candidates(max_price):
    reg = json.load(open(os.path.join(DATA, "cdp_resources_raw.json")))
    seen, out = set(), []
    # spot-price shapes only; skip news/chart/perps/forecast/convert/swap
    BAD = ("news", "chart", "perp", "forecast", "history", "convert", "swap", "global", "market-cap")
    for r in reg:
        url = r.get("resource") or ""
        host = (urlparse(url).hostname or "").lower()
        blob = (url + " " + (r.get("description") or "")).lower()
        if not url.startswith("http") or host in seen:
            continue
        looks_price = ("price" in blob or "quote" in blob or "/spot" in blob or "ticker" in blob)
        is_btc = ("btc" in blob or "bitcoin" in blob)
        if not (looks_price and is_btc):
            continue
        if any(b in blob for b in BAD):
            continue
        c = conform.contract(r)
        p = conform.price_of(r)
        if not (c and p and 0 < p <= max_price):
            continue
        seen.add(host)
        out.append({"url": url, "host": host, "price": p, **c})
    out.sort(key=lambda c: c["price"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--budget", type=float, default=0.20)
    ap.add_argument("--max-price", type=float, default=0.01)
    ap.add_argument("--pause", type=float, default=0.8)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    ref0, refs0 = exchange_ref()
    if ref0 is None:
        print("no exchange reference; aborting so we do not grade blind")
        return 1
    print(f"reference BTC/USD spot: ${ref0:,.2f}  ({', '.join(f'{k} ${v:,.0f}' for k,v in refs0.items())})\n")

    cands = btc_candidates(a.max_price)[:a.limit]
    print(f"selected {len(cands)} BTC spot-price sellers, "
          f"advertised total ${sum(c['price'] for c in cands):.4f}")
    if a.dry_run:
        for c in cands:
            print(f"  ${c['price']:.4f} {c['method']:<4} {c['host']}")
        print("dry run, nothing paid")
        return 0

    bal0 = conform.wallet_balance()
    print(f"wallet before: ${bal0:.6f}\n" if bal0 is not None else "")
    rows, spent = [], 0.0
    for i, c in enumerate(cands, 1):
        if spent + c["price"] > a.budget:
            print(f"stopping at {i-1}: next call would pass the budget")
            break
        cap = min(round(c["price"] * 1.25 + 0.0001, 6), 0.01)
        ok, payload, meta = conform.call(c["url"], c["method"], c["body"], cap)
        paid = (meta or {}).get("payment") if isinstance((meta or {}).get("payment"), dict) else {}
        if paid.get("success"):
            spent += cap
        price, path = (find_price(payload, ref0) if ok and isinstance(payload, (dict, list))
                       else (None, "no readable response"))
        dev = round(10000 * (price - ref0) / ref0, 1) if price else None      # basis points
        age = find_ts_age(payload) if ok else None
        rows.append({"host": c["host"], "url": c["url"], "quoted": c["price"],
                     "paid": bool(paid.get("success")), "price": price, "field": path,
                     "dev_bps": dev, "staleness_s": age})
        mark = "  --  " if price is None else f"{dev:+.0f}bps"
        shown = f"${price:,.2f}" if price else "no price"
        print(f"  {i:>2}/{len(cands)} ${c['price']:.4f} {mark:>9}  {shown:<12} {c['host']}")
        time.sleep(a.pause)

    ref1, _ = exchange_ref()
    bal1 = conform.wallet_balance()
    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "symbol": "BTC/USD",
           "reference_start": ref0, "reference_end": ref1, "reference_sources": refs0,
           "spent_reported": round(spent, 6),
           "spent_onchain": round(bal0 - bal1, 6) if (bal0 is not None and bal1 is not None) else None,
           "rows": rows}
    json.dump(out, open(os.path.join(DATA, "price_shootout.json"), "w"), indent=1)
    got = [r for r in rows if r["price"]]
    print(f"\n{len(got)}/{len(rows)} returned a usable BTC price")
    if got:
        devs = sorted(abs(r["dev_bps"]) for r in got)
        print(f"  median abs deviation from spot: {devs[len(devs)//2]:.0f} bps")
        print(f"  worst: {max(got, key=lambda r: abs(r['dev_bps']))['host']} "
              f"at {max(abs(r['dev_bps']) for r in got):.0f} bps")
    if bal0 is not None and bal1 is not None:
        print(f"  wallet moved ${bal0 - bal1:.6f} (reported ${spent:.6f})")
    print("  wrote data/price_shootout.json (raw responses archived in captures/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
