#!/usr/bin/env python3
"""Buy one US stock's real-time price from many x402 sellers, grade against FMP.

The reference is a PRIMARY source an agent cannot get for free through these
sellers: Financial Modeling Prep's real-time quote (a paid, professional feed),
not another x402 vendor. What we measure is dispersion (how far each seller is
from the FMP quote) and whether it returns a usable price at all.

verify-before-accusing: a returned number counts as this ticker's price only if
it is within a band of the FMP reference. A seller returning a different
instrument, or no price, reads as "could not isolate the price", never a wrong
price. Only a value genuinely near the reference is graded for accuracy.

Every call is capped and the raw response is archived by conform, so extraction
is re-auditable offline without re-paying. Run:
  python3 stock_shootout.py --ticker AAPL --dry-run
  python3 stock_shootout.py --ticker AAPL --budget 0.10
"""
import argparse
import json
import os
import re
import time
import urllib.request
from urllib.parse import urlparse, urlencode

import conform

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
FMP_KEY = os.environ.get("FMP_API_KEY", "")


def fmp_ref(ticker):
    """FMP real-time quote price for the ticker. The paid, primary reference."""
    if not FMP_KEY:
        print("  no FMP_API_KEY in env; cannot grade")
        return None
    url = f"https://financialmodelingprep.com/stable/quote?symbol={ticker}&apikey={FMP_KEY}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            d = json.load(r)
        q = (d[0] if isinstance(d, list) and d else d) or {}
        return float(q["price"]) if q.get("price") is not None else None
    except Exception as e:
        print(f"  FMP ref failed: {e}")
        return None


_PRICE_KEYS = {"price", "last", "close", "regularmarketprice", "currentprice",
               "lastprice", "quote", "value", "latestprice", "current", "c", "p"}


def find_price(obj, ref, band=0.15):
    """Best stock-price candidate: a named price field near the FMP reference.
    Returns (value, path). If nothing is within `band` of the reference, returns
    (None, why): we could not isolate THIS ticker's price, which is inconclusive,
    not a wrong-price accusation."""
    named, anynum = [], []

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower().replace("_", "").replace(" ", "")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if 0.1 < v < 100000:
                        anynum.append((float(v), f"{path}.{k}"))
                        if kl in _PRICE_KEYS or "price" in kl:
                            named.append((float(v), f"{path}.{k}"))
                elif isinstance(v, str):
                    try:
                        fv = float(v.replace(",", "").lstrip("$"))
                        if 0.1 < fv < 100000:
                            anynum.append((fv, f"{path}.{k}"))
                            if kl in _PRICE_KEYS or "price" in kl:
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
        near = [(v, p) for v, p in pool if abs(v - ref) / ref <= band]
        if near:
            return min(near, key=lambda x: abs(x[0] - ref))
        return None, "no price near the FMP reference (could not isolate this ticker)"
    return (named or anynum or [(None, "no numeric field")])[0]


# Real-time SINGLE-stock price sellers only. The band guard in find_price is the
# safety net, but the filter also excludes categories that are not an AAPL price
# (tokenized/crypto, forex, indices/ETFs, filings, calendars, news) so we do not
# pay them just to mark them inconclusive.
_YES = ("real-time", "realtime", "current price", "stock quote", "stock price",
        "equity quote", "quote with", "price, change", "price, intraday", "realtime price")
_NEED = ("stock", "equity", "ticker", "aapl")
_NO = ("token", "tokeni", "forex", " fx ", "exchange rate", "index", "etf", "googl",
       "crypto", "filing", "edgar", "sec ", "earnings", "insider", "form 4",
       "calendar", "indicator", "screen", "sentiment", "congress", "13f", "news",
       "option", "yield", "treasury", "china", "ohlcv", "candles", "swap", "s&p")


def stock_candidates(ticker, max_price):
    reg = json.load(open(os.path.join(DATA, "cdp_resources_raw.json")))
    seen, out = set(), []
    for r in reg:
        url = r.get("resource") or ""
        host = (urlparse(url).hostname or "").lower()
        blob = (url + " " + (r.get("description") or "")).lower()
        if not url.startswith("http") or host in seen:
            continue
        if not (any(k in blob for k in _YES) and any(k in blob for k in _NEED)):
            continue
        if any(k in blob for k in _NO):
            continue
        # A different ticker hard-coded as the terminal path segment (e.g.
        # /stock/quote/avgo) cannot return our ticker, so skip it rather than pay
        # for the wrong stock. Only the LAST segment, and never a common API word,
        # so /stocks/us/price/AAPL and /api/stock/quote are kept.
        _last = url.lower().split("?")[0].rstrip("/").split("/")[-1]
        _NONTICKER = {"quote", "price", "latest", "snapshot", "data", "stocks", "stock",
                      "us", "market", "current", "realtime", "finance"}
        if (_last != ticker.lower() and _last not in _NONTICKER
                and re.fullmatch(r"[a-z]{2,5}", _last)):
            continue
        p = conform.price_of(r)
        c = conform.contract(r)
        if not (p and 0 < p <= max_price):
            continue
        method = (c or {}).get("method", "GET")
        # Inject the ticker: already in the path (blockrun /AAPL), else a query
        # param on a GET, else a body on a POST. symbol AND ticker cover both
        # common param names; the endpoint uses whichever it knows.
        call_url, body = url, None
        if ticker.lower() in url.lower():
            pass
        elif method == "GET":
            call_url = url + ("&" if "?" in url else "?") + urlencode({"symbol": ticker, "ticker": ticker})
        else:
            body = {"symbol": ticker, "ticker": ticker}
        seen.add(host)
        out.append({"host": host, "url": call_url, "method": method, "body": body, "price": p})
    out.sort(key=lambda c: c["price"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", default="AAPL")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--budget", type=float, default=0.10)
    ap.add_argument("--max-price", type=float, default=0.02)
    ap.add_argument("--pause", type=float, default=0.8)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    tkr = a.ticker.upper()

    ref = fmp_ref(tkr)
    if ref is None and not a.dry_run:
        print("no FMP reference; aborting so we do not grade blind")
        return 1
    print(f"reference {tkr}: ${ref:,.2f}  (FMP /stable/quote)\n" if ref else "")

    cands = stock_candidates(tkr, a.max_price)[:a.limit]
    print(f"selected {len(cands)} real-time stock-price sellers, "
          f"advertised total ${sum(c['price'] for c in cands):.4f}")
    if a.dry_run:
        for c in cands:
            print(f"  ${c['price']:.4f} {c['method']:<4} {c['host']:<40} {c['url'][:70]}")
        print("\ndry run, nothing paid")
        return 0

    bal0 = conform.wallet_balance()
    print(f"wallet before: ${bal0:.6f}\n" if bal0 is not None else "")
    rows, spent = [], 0.0
    for i, c in enumerate(cands, 1):
        if spent + c["price"] > a.budget:
            print(f"stopping at {i-1}: next call would pass the budget")
            break
        cap = min(round(c["price"] * 1.25 + 0.0001, 6), 0.02)
        ok, payload, meta = conform.call(c["url"], c["method"], c["body"], cap)
        paid = (meta or {}).get("payment") if isinstance((meta or {}).get("payment"), dict) else {}
        if paid.get("success"):
            spent += cap
        price, path = (find_price(payload, ref) if ok and isinstance(payload, (dict, list))
                       else (None, "no readable response"))
        dev = round(10000 * (price - ref) / ref, 1) if price else None      # basis points
        rows.append({"host": c["host"], "url": c["url"], "quoted": c["price"],
                     "paid": bool(paid.get("success")), "price": price, "field": path,
                     "dev_bps": dev})
        mark = "  --  " if price is None else f"{dev:+.0f}bps"
        shown = f"${price:,.2f}" if price else "no price"
        print(f"  {i:>2}/{len(cands)} ${c['price']:.4f} {mark:>9}  {shown:<12} {c['host']}")
        time.sleep(a.pause)

    ref_end = fmp_ref(tkr)
    bal1 = conform.wallet_balance()
    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "symbol": tkr,
           "reference": ref, "reference_end": ref_end, "reference_source": "FMP /stable/quote",
           "spent_reported": round(spent, 6),
           "spent_onchain": round(bal0 - bal1, 6) if (bal0 is not None and bal1 is not None) else None,
           "rows": rows}
    json.dump(out, open(os.path.join(DATA, "stock_shootout.json"), "w"), indent=1)
    got = [r for r in rows if r["price"]]
    print(f"\n{len(got)}/{len(rows)} returned a usable {tkr} price")
    if got:
        devs = sorted(abs(r["dev_bps"]) for r in got)
        print(f"  median abs deviation from FMP: {devs[len(devs)//2]:.0f} bps")
        worst = max(got, key=lambda r: abs(r["dev_bps"]))
        print(f"  worst: {worst['host']} at {worst['dev_bps']:+.0f} bps")
    if bal0 is not None and bal1 is not None:
        print(f"  wallet moved ${bal0 - bal1:.6f} (reported ${spent:.6f})")
    print("  wrote data/stock_shootout.json (raw responses archived in captures/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
