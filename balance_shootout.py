#!/usr/bin/env python3
"""Ask many APIs for one wallet's USDC balance on Base, check against the chain.

Ground truth is exact and free: the token contract's balanceOf, read from Base's
latest block. Unlike a static data feed, a balance moves, so this also catches who
returns a live number versus a stale cached one. Every seller is asked for the
same address (vitalik.eth), and its answer is graded against the chain in dollars.

Every call is capped and archived. Run: python3 balance_shootout.py
"""
import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from urllib.parse import urlparse

import conform

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
TARGET = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"   # vitalik.eth
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
BASE_RPCS = ["https://base.drpc.org", "https://mainnet.base.org", "https://base.llamarpc.com"]


def true_usdc(addr):
    data = "0x70a08231" + "0" * 24 + addr[2:].lower()
    for rpc in BASE_RPCS:
        try:
            req = urllib.request.Request(rpc, method="POST",
                headers={"content-type": "application/json", "user-agent": "Mozilla/5.0"},
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                 "params": [{"to": USDC, "data": data}, "latest"]}).encode())
            with urllib.request.urlopen(req, timeout=15) as r:
                res = json.load(r).get("result")
            if res and res != "0x":
                return int(res, 16) / 1e6
        except Exception:
            continue
    return None


_ADDR_KEYS = ("address", "wallet", "account", "addr", "holder")


def retarget(v):
    if isinstance(v, dict):
        return {k: (TARGET if str(k).lower() in _ADDR_KEYS else retarget(x)) for k, x in v.items()}
    return v


def balance_candidates(max_price):
    reg = json.load(open(os.path.join(DATA, "cdp_resources_raw.json")))
    seen, out = set(), []
    for r in reg:
        url = r.get("resource") or ""
        host = (urlparse(url).hostname or "").lower()
        blob = (url + " " + (r.get("description") or "")).lower()
        if not url.startswith("http") or host in seen:
            continue
        if not (("balance" in blob or "portfolio" in blob or "holdings" in blob)
                and ("wallet" in blob or "address" in blob) and "base" in blob):
            continue
        if any(b in blob for b in ("solana", "validate", "checksum", "vault", "yield")):
            continue
        bz = ((r.get("extensions") or {}).get("bazaar") or {}).get("info", {})
        inp = bz.get("input", {}) or {}
        method = (inp.get("method") or "GET").upper()
        p = conform.price_of(r)
        if not p or not (0 < p <= max_price):
            continue
        # build the call, injecting the target address into query / path / body
        call_url = url
        if ":address" in url or ":addr" in url or ":wallet" in url:
            call_url = url.replace(":address", TARGET).replace(":addr", TARGET).replace(":wallet", TARGET)
        qp = retarget(inp.get("queryParams")) if inp.get("queryParams") is not None else None
        if qp is None and method == "GET" and "{TARGET}" not in call_url and not any(
                s in call_url.lower() for s in (TARGET.lower(),)):
            qp = {"address": TARGET}                       # default GET param
        if qp:
            call_url = call_url + ("&" if "?" in call_url else "?") + urllib.parse.urlencode(qp)
        body = retarget(inp.get("body")) if inp.get("body") is not None else None
        if body is None and method == "POST":
            body = {"address": TARGET}
        seen.add(host)
        out.append({"host": host, "url": call_url, "method": method, "body": body, "price": p})
    out.sort(key=lambda c: c["price"])
    return out


def find_usdc(obj, ref):
    """Best USDC-balance candidate: a USDC-labelled field first, else the number
    closest to the chain value."""
    labelled, anynum = [], []

    def walk(o, ctx):
        if isinstance(o, dict):
            ctx_usdc = ctx or any("usdc" in str(k).lower() for k in o.keys())
            sym = str(o.get("symbol") or o.get("token") or o.get("ticker") or "").lower()
            here_usdc = "usdc" in sym or ctx_usdc
            for k, v in o.items():
                kl = str(k).lower()
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if 0 < v < 1e12:
                        anynum.append(float(v))
                        if here_usdc or "usdc" in kl or (("balance" in kl or "amount" in kl) and "usdc" in json.dumps(o).lower()):
                            labelled.append(float(v))
                elif isinstance(v, str):
                    try:
                        fv = float(v.replace(",", "").lstrip("$"))
                        if 0 < fv < 1e12:
                            anynum.append(fv)
                            if here_usdc or "usdc" in kl:
                                labelled.append(fv)
                    except (ValueError, AttributeError):
                        pass
                else:
                    walk(v, here_usdc)
        elif isinstance(o, list):
            for v in o:
                walk(v, ctx)

    walk(obj, False)
    if ref:
        pool = labelled or anynum
        near = [v for v in pool if abs(v - ref) <= max(0.5, ref * 0.02)]
        if near:
            return min(near, key=lambda v: abs(v - ref))
        if labelled:
            return min(labelled, key=lambda v: abs(v - ref))
    return labelled[0] if labelled else (None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--budget", type=float, default=0.12)
    ap.add_argument("--max-price", type=float, default=0.02)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    ref0 = true_usdc(TARGET)
    if ref0 is None and not a.dry_run:
        print("no chain reference; aborting")
        return 1
    print(f"chain truth: {TARGET} holds ${ref0:,.6f} USDC on Base\n" if ref0 else "")
    cands = balance_candidates(a.max_price)[:a.limit]
    print(f"selected {len(cands)} balance sellers, advertised total ${sum(c['price'] for c in cands):.4f}")
    if a.dry_run:
        for c in cands:
            print(f"  ${c['price']:.4f} {c['method']:<4} {c['host']}  body={c['body']}")
        return 0

    bal0 = conform.wallet_balance()
    rows, spent = [], 0.0
    for i, c in enumerate(cands, 1):
        if spent + c["price"] > a.budget:
            print(f"stopping at {i-1}: budget")
            break
        cap = min(round(c["price"] * 1.25 + 0.0001, 6), 0.02)
        ok, payload, meta = conform.call(c["url"], c["method"], c["body"], cap)
        paid = (meta or {}).get("payment") if isinstance((meta or {}).get("payment"), dict) else {}
        if paid.get("success"):
            spent += cap
        usd = find_usdc(payload, ref0) if ok and isinstance(payload, (dict, list)) else None
        dev = round(usd - ref0, 4) if (usd is not None and ref0) else None
        rows.append({"host": c["host"], "url": c["url"], "quoted": c["price"],
                     "usdc": usd, "dev": dev})
        shown = f"${usd:,.4f}" if usd is not None else "no usdc figure"
        d = f"  off by ${dev:+.4f}" if dev is not None else ""
        print(f"  {i:>2}/{len(cands)} ${c['price']:.4f}  {shown:<18}{d}  {c['host']}")
        time.sleep(0.7)
    ref1 = true_usdc(TARGET)
    bal1 = conform.wallet_balance()
    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "target": TARGET,
           "chain_usdc_start": ref0, "chain_usdc_end": ref1,
           "spent_onchain": round(bal0 - bal1, 6) if (bal0 is not None and bal1 is not None) else None,
           "rows": rows}
    json.dump(out, open(os.path.join(DATA, "balance_shootout.json"), "w"), indent=1)
    got = [r for r in rows if r["usdc"] is not None]
    exact = [r for r in got if abs(r["dev"]) <= max(0.01, ref0 * 0.001)]
    print(f"\n{len(got)}/{len(rows)} returned a USDC figure; {len(exact)} matched the chain exactly")
    if bal0 is not None and bal1 is not None:
        print(f"wallet moved ${bal0 - bal1:.6f}")
    print("wrote data/balance_shootout.json (raw responses archived in captures/)")


if __name__ == "__main__":
    main()
