#!/usr/bin/env python3
"""The daily endpoint lab: grade every accuracy-checkable seller against a primary
source, once per day, within a budget, and grow the corpus.

One config-driven engine replacing the per-category shootouts. For each of the
six accuracy categories it injects a FIXED probe (BTC, AAPL, EUR/USD, New York
weather, vitalik's wallet, Base gas), fetches the primary-source truth once, then
queries every seller the category classifier found, extracts the value near the
reference (verify-before-accusing: a value not near the reference is 'could not
measure', never a wrong number), grades the deviation, and records it. Output
feeds receipts.py (accuracy receipts) and the per-category leaderboard.

Every call is capped and archived by conform. Run:
  python3 lab.py --dry-run                 # coverage per category, free
  python3 lab.py --budget 4.5              # grade the accuracy corpus
  python3 lab.py --category stock-price --budget 0.3
"""
import argparse
import json
import os
import time
import urllib.request

import re

import conform
from stock_shootout import fmp_ref
from price_shootout import exchange_ref
from balance_shootout import true_usdc, TARGET as BAL_TARGET
from weather_shootout import nws_reference

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TESTED_LOG = os.path.join(DATA, "tested_log.json")   # url -> {last_tested, price}
BASE_RPCS = ["https://base.drpc.org", "https://mainnet.base.org", "https://base.llamarpc.com"]


# ---- primary-source references (the ground truth per category) ---------------
def _crypto_ref():
    return exchange_ref()[0]


def _fx_ref():
    # ECB's own daily reference rate for EUR/USD, from its published XML feed.
    try:
        req = urllib.request.Request("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
                                     headers={"user-agent": "whatagentsbuy.com (contact https://whatagentsbuy.com/about)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            xml = r.read().decode()
        m = re.search(r"currency=['\"]USD['\"] rate=['\"]([0-9.]+)['\"]", xml)
        return float(m.group(1)) if m else None
    except Exception as e:
        print(f"  fx ref failed: {e}")
        return None


def _gas_ref():
    # Base's live base fee in gwei, straight off the latest block. Free, exact.
    for rpc in BASE_RPCS:
        try:
            req = urllib.request.Request(rpc, method="POST",
                headers={"content-type": "application/json", "user-agent": "Mozilla/5.0"},
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_getBlockByNumber",
                                 "params": ["latest", False]}).encode())
            with urllib.request.urlopen(req, timeout=15) as r:
                b = json.load(r).get("result") or {}
            if b.get("baseFeePerGas"):
                return int(b["baseFeePerGas"], 16) / 1e9
        except Exception:
            continue
    return None


def _weather_ref():
    # New York current temperature (F). Open-Meteo is a primary weather model
    # (free, no key, always returns), more reliable than NWS's sometimes-null
    # observations; NWS is the fallback.
    try:
        u = ("https://api.open-meteo.com/v1/forecast?latitude=40.7128&longitude=-74.0060"
             "&current=temperature_2m&temperature_unit=fahrenheit")
        with urllib.request.urlopen(u, timeout=15) as r:
            t = json.load(r).get("current", {}).get("temperature_2m")
        if t is not None:
            return round(float(t), 1)
    except Exception as e:
        print(f"  open-meteo failed: {e}")
    d = nws_reference()
    return d.get("f") if isinstance(d, dict) else None


def _balance_ref():
    return true_usdc(BAL_TARGET)


def _weather_variants(v):
    """A weather number may already be Fahrenheit, or be Celsius, or Kelvin. Return
    all three converted INTO Fahrenheit, so a seller reporting 25 (C = 77F) or 297
    (K = 74.9F) is graded on the correct value rather than dropped as unmeasurable.
    Confirmed against the raw archive: 17 weather sellers report C/K, not F."""
    return (v, v * 9.0 / 5.0 + 32.0, (v - 273.15) * 9.0 / 5.0 + 32.0)


# ---- per-category config: probe, reference, and how to grade ------------------
# kind "pct": deviation in basis points, band and tolerance are fractions/bps.
# kind "abs": deviation in the raw unit, band and tolerance are absolute.
CONFIG = {
    "crypto-price": dict(inject={"symbol": "BTC", "ticker": "BTCUSD", "coin": "bitcoin", "pair": "BTC-USD"},
        ref=_crypto_ref, kind="pct", band=0.06, tol=100.0, unit="bps", vrange=(1000, 10_000_000),
        metric="BTC/USD spot price", source="Coinbase/Kraken median"),
    "stock-price": dict(inject={"symbol": "AAPL", "ticker": "AAPL"},
        ref=lambda: fmp_ref("AAPL"), kind="pct", band=0.05, tol=100.0, unit="bps", vrange=(1, 100000),
        metric="AAPL real-time price", source="FMP real-time quote"),
    "fx-rate": dict(inject={"from": "EUR", "to": "USD", "base": "EUR", "symbol": "EURUSD", "pair": "EUR/USD"},
        ref=_fx_ref, kind="pct", band=0.015, tol=50.0, unit="bps", vrange=(0.3, 3.0),
        metric="EUR/USD rate", source="ECB reference rate"),
    "gas-price": dict(inject={"chain": "base", "network": "base"},
        ref=_gas_ref, kind="abs", band=0.05, tol=0.01, unit="gwei", vrange=(0.0001, 5000),
        metric="Base gas base fee (gwei)", source="Base chain latest block"),
    "wallet-balance": dict(inject={"address": BAL_TARGET, "wallet": BAL_TARGET, "account": BAL_TARGET},
        ref=_balance_ref, kind="abs", band=1.0, tol=0.01, unit="USDC", vrange=(0, 1e9),
        metric="USDC balance of vitalik.eth on Base", source="Base chain balanceOf"),
    "weather": dict(inject={"location": "New York", "city": "New York", "q": "New York", "place": "New York"},
        ref=_weather_ref, kind="abs", band=12.0, tol=5.0, unit="°F", vrange=(-40, 320),
        variants=_weather_variants,   # accept Celsius/Kelvin, converted to F; vrange admits Kelvin
        metric="New York temperature", source="Open-Meteo (New York)"),
}


_VALUE_HINT = ("price", "last", "close", "rate", "value", "usd", "balance", "temp",
               "current", "amount", "quote", "spot", "basefee", "gwei", "gas")


def find_value(obj, ref, band, kind, vrange, variants=None):
    """Best value near the reference, from anywhere in the response. A value not
    within `band` of the reference returns (None, why): could not isolate this
    metric, which is inconclusive, never a wrong-value accusation.

    `variants(v)` expands one raw number into the ways it could legitimately encode
    the metric in a different unit, each already converted INTO the reference's
    unit. Weather sellers return the temperature in Celsius or Kelvin as often as
    Fahrenheit, and the F-only match discarded 17 correct weather sellers as
    'could not measure'. The value RETURNED is the converted one, so the deviation
    is computed in the reference's unit. Default: identity (no unit ambiguity)."""
    variants = variants or (lambda v: (v,))
    named, anynum = [], []
    lo, hi = vrange

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower().replace("_", "").replace(" ", "")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if lo < v < hi:
                        anynum.append((float(v), f"{path}.{k}"))
                        if any(h in kl for h in _VALUE_HINT):
                            named.append((float(v), f"{path}.{k}"))
                elif isinstance(v, str):
                    try:
                        fv = float(v.replace(",", "").lstrip("$"))
                        if lo < fv < hi:
                            anynum.append((fv, f"{path}.{k}"))
                            if any(h in kl for h in _VALUE_HINT):
                                named.append((fv, f"{path}.{k}"))
                    except (ValueError, AttributeError):
                        pass
                else:
                    walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")

    walk(obj, "")
    if ref is None:
        return None, "no reference"
    # THE FIELD IS CHOSEN BY ITS NAME, NEVER BY THE ANSWER KEY. The old form
    # pooled every number in the response and kept whichever landed closest to
    # the reference, which let the answer key pick the answer: a response of
    # {price: 50000, previous_close: 60000} against a reference of 60000 earned
    # a perfect match off the wrong field, while a plainly wrong {price: 50000}
    # alone was censored to "inconclusive" instead of graded off. Now:
    #   1. only NAMED candidates qualify (no name, no measurement);
    #   2. among named candidates, the strongest name wins (_VALUE_HINT is
    #      ordered, canonical name first), decided before the reference is
    #      consulted;
    #   3. the reference disambiguates only the UNIT of that one chosen field
    #      (Celsius vs Kelvin vs Fahrenheit), never which field is the metric;
    #   4. a chosen field whose value is far from the reference is RETURNED and
    #      graded off, not censored.
    if not named:
        return None, ("no field named like the metric (extraction limit, not evidence "
                      "the seller is wrong)")

    def name_rank(path):
        kl = path.rsplit(".", 1)[-1].lower().replace("_", "").replace(" ", "")
        return min((i for i, h in enumerate(_VALUE_HINT) if h in kl), default=len(_VALUE_HINT))

    best_rank = min(name_rank(p) for _, p in named)
    finalists = [(raw, p) for raw, p in named if name_rank(p) == best_rank]

    picked = None   # (dist, value, path); unit choice may use ref, field choice may not
    for raw, p in finalists:
        cands = list(variants(raw))
        scored = [(abs(c - ref) / ref if kind == "pct" else abs(c - ref), c) for c in cands]
        inband = [sc for sc in scored if sc[0] <= band]
        # in-band variant -> that unit reading; otherwise keep the RAW value so a
        # wrong answer stays measurable instead of vanishing
        off, val = min(inband) if inband else (
            (abs(raw - ref) / ref if kind == "pct" else abs(raw - ref)), float(raw))
        if picked is None or off < picked[0]:
            picked = (off, val, p)
    return picked[1], picked[2]


def _build_call(url, method, inject, asset_in_path):
    """Inject the fixed probe into a seller's call: query params on a GET, a body
    on a POST, unless the ticker/asset is already fixed in the URL path."""
    from urllib.parse import urlencode
    if asset_in_path:
        return url, None
    if method == "GET":
        return url + ("&" if "?" in url else "?") + urlencode(inject), None
    return url, dict(inject)


def category_sellers(cat, max_price):
    m = json.load(open(os.path.join(DATA, "categories_map.json")))["categories"]
    out = []
    for s in (m.get(cat, {}).get("sellers") or []):
        p = s.get("price_usdc")
        if not (p and 0 < p <= max_price):
            continue
        out.append(s)
    out.sort(key=lambda s: s["price_usdc"])
    return out


def load_tested_log():
    if os.path.exists(TESTED_LOG):
        try:
            return json.load(open(TESTED_LOG))
        except Exception:
            return {}
    return {}


def save_tested_log(log):
    json.dump(log, open(TESTED_LOG, "w"), indent=1)


def _days_since(date_str, today):
    import datetime
    try:
        return (datetime.date.fromisoformat(today)
                - datetime.date.fromisoformat(str(date_str)[:10])).days
    except Exception:
        return 999


def select_targets(sellers, log, today, freshness_days):
    """Self-pacing selection. A paid check measures a STABLE property, so it is
    not repeated daily. Priority: never-tested, then endpoints whose advertised
    price changed since we tested them (a real change worth re-checking), then a
    slow rotation of the oldest past the freshness floor. Recent, unchanged
    endpoints are skipped, which keeps the spend on new coverage rather than
    re-grading the same sellers. Returns (ordered targets, skipped count)."""
    scored, skipped = [], 0
    for s in sellers:
        e = log.get(s["url"])
        if e is None:
            scored.append((0, 10**9, s))                               # never tested
        elif abs((e.get("price") or 0) - (s.get("price_usdc") or 0)) > 1e-9:
            scored.append((0, 10**9, s))                               # advertised price changed
        else:
            age = _days_since(e.get("last_tested"), today)
            if age >= freshness_days:
                scored.append((1, age, s))                             # stale past the floor
            else:
                skipped += 1                                           # recent + unchanged -> skip
    scored.sort(key=lambda x: (x[0], -x[1]))           # new/changed first, then oldest
    return [s for _, _, s in scored], skipped


def grade_seller(cat, s, ref, cap):
    cfg = CONFIG[cat]
    method = "GET"                      # the map does not carry method; default GET, POST fallback below
    url, body = _build_call(s["url"], method, cfg["inject"], False)
    ok, payload, meta = conform.call(url, method, body, cap)
    if not (ok and isinstance(payload, (dict, list))):
        # one POST retry: some quote endpoints only accept a body
        url2, body2 = _build_call(s["url"], "POST", cfg["inject"], False)
        ok, payload, meta = conform.call(url2, "POST", body2, cap)
        url = url2
    paid = (meta or {}).get("payment") if isinstance((meta or {}).get("payment"), dict) else {}
    val, path = (find_value(payload, ref, cfg["band"], cfg["kind"], cfg["vrange"], cfg.get("variants"))
                 if ok and isinstance(payload, (dict, list)) else (None, "no readable response"))
    if val is None:
        dev = None
    elif cfg["kind"] == "pct":
        dev = round(10000 * (val - ref) / ref, 1)
    else:
        dev = round(val - ref, 4)
    return {"host": s["host"], "url": url, "quoted": s["price_usdc"],
            "paid": bool(paid.get("success")), "value": val, "field": path, "dev": dev,
            "tx": paid.get("transactionHash")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default=None, help="one category, else all accuracy categories")
    ap.add_argument("--budget", type=float, default=4.5, help="hard $ cap for the whole run")
    ap.add_argument("--max-price", type=float, default=0.10,
                    help="per-call price ceiling; --budget bounds total spend. Was 0.02, which "
                         "silently excluded every objective-category seller priced above 2c.")
    ap.add_argument("--pause", type=float, default=0.5)
    ap.add_argument("--freshness-days", type=int, default=14,
                    help="re-test an unchanged endpoint only after this many days")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cats = [a.category] if a.category else list(CONFIG)
    today = time.strftime("%Y-%m-%d")
    log = load_tested_log()
    if a.dry_run:
        grand = 0.0
        for cat in cats:
            sellers = category_sellers(cat, a.max_price)
            targets, skipped = select_targets(sellers, log, today, a.freshness_days)
            cost = sum(min(s["price_usdc"] * 1.25, 0.02) for s in targets)
            grand += cost
            print(f"{cat:<16} {len(sellers):>4} known  {len(targets):>4} to test  "
                  f"{skipped:>4} skipped(fresh)  ~${cost:.3f}   {CONFIG[cat]['source']}")
        print(f"\nthis run would spend ~${grand:.3f}  (freshness floor {a.freshness_days}d, "
              f"{sum(1 for v in log.values())} endpoints already in the log)")
        return 0

    # tests before spending, like every paid path here
    if not os.environ.get("WAB_SKIP_TESTS"):
        t = conform.subprocess.run([conform.sys.executable, os.path.join(HERE, "tests.py")],
                                   capture_output=True, text=True)
        if t.returncode != 0:
            print(t.stdout + t.stderr + "\nREFUSING TO SPEND: tests failed.")
            return 1

    bal0 = conform.wallet_balance()
    if bal0 is None:
        print("cannot read wallet; aborting")
        return 1
    # Guard: never grade on an empty/near-empty wallet. Each grade attempt would
    # fail its payment, yet still stamp the seller as "tested" in the freshness log,
    # which suppresses a real grade for 14 days and overwrites lab.json with empty
    # rows. So if there is not enough to pay even a couple of calls, skip entirely
    # and leave the log untouched. This is what makes it safe to run daily before
    # the wallet is funded (it no-ops cleanly instead of poisoning the corpus).
    min_needed = max(a.max_price * 2, 0.02)
    if bal0 < min_needed:
        print(f"wallet ${bal0:.6f} < ${min_needed:.4f} needed; skipping grading so the freshness "
              f"log is not poisoned. Fund USDC on Base to grade the {'' if a.dry_run else ''}new sellers.")
        return 0
    print(f"wallet before: ${bal0:.6f}\n")

    all_rows, spent = {}, 0.0
    for cat in cats:
        ref = CONFIG[cat]["ref"]()
        if ref is None:
            print(f"[{cat}] no reference; skipping so we never grade blind")
            continue
        sellers = category_sellers(cat, a.max_price)
        targets, skipped = select_targets(sellers, log, today, a.freshness_days)
        print(f"[{cat}] ref={ref} ({CONFIG[cat]['source']}), {len(targets)} to test, {skipped} skipped (fresh)")
        rows = []
        for i, s in enumerate(targets, 1):
            cap = min(round(s["price_usdc"] * 1.25 + 0.0001, 6), a.max_price)
            if spent + cap > a.budget:
                print(f"  budget reached, stopping {cat} at {i-1}")
                break
            r = grade_seller(cat, s, ref, cap)
            if r["paid"]:
                spent += cap
            rows.append(r)
            log[s["url"]] = {"last_tested": today, "price": s.get("price_usdc")}
            shown = ("--" if r["value"] is None else f"{r['value']}")
            devs = "" if r["dev"] is None else f" ({r['dev']:+}{CONFIG[cat]['unit']})"
            print(f"    {i:>3}/{len(targets)} ${s['price_usdc']:.4f}  {str(shown):<12}{devs:<14} {r['host']}")
            time.sleep(a.pause)
        all_rows[cat] = {"reference": ref, "source": CONFIG[cat]["source"],
                         "metric": CONFIG[cat]["metric"], "unit": CONFIG[cat]["unit"],
                         "tol": CONFIG[cat]["tol"], "kind": CONFIG[cat]["kind"], "rows": rows}
        save_tested_log(log)                            # persist after each category

    bal1 = conform.wallet_balance()
    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "spent_onchain": round(bal0 - bal1, 6) if bal1 is not None else None,
           "categories": all_rows}
    json.dump(out, open(os.path.join(DATA, "lab.json"), "w"), indent=1)
    graded = sum(len([r for r in c["rows"] if r["value"] is not None]) for c in all_rows.values())
    total = sum(len(c["rows"]) for c in all_rows.values())
    print(f"\ngraded {graded}/{total} sellers with a usable value across {len(all_rows)} categories")
    print(f"wallet moved ${bal0 - (bal1 or bal0):.6f}; wrote data/lab.json (raw archived in captures/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
