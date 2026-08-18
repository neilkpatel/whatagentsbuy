#!/usr/bin/env python3
"""Buy "the gas price" from many sellers and check it against the chain's own base fee.

Unlike a token price, there is no single gas price: it differs by chain (Base is
~1000x cheaper than Ethereum), by tier (base fee vs priority vs total), and by
unit (gwei vs a USD cost). So the reference is the live base fee of BOTH Base and
Ethereum, read straight from each chain's latest block, and each seller is graded
against whichever chain it is actually quoting, plus how many blocks stale it is.

Every call is capped and archived by conform, so the extraction is re-auditable
offline. Run: python3 gas_shootout.py --limit 10 --budget 0.12
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

RPCS = {
    "base": ["https://mainnet.base.org", "https://base.drpc.org", "https://base.llamarpc.com"],
    "ethereum": ["https://ethereum-rpc.publicnode.com", "https://eth.drpc.org", "https://cloudflare-eth.com"],
}


def _rpc(url, method, params):
    # Many public RPCs 403 the default python-urllib User-Agent, so send a normal one.
    req = urllib.request.Request(url, method="POST",
        headers={"content-type": "application/json", "user-agent": "Mozilla/5.0 (whatagentsbuy)"},
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode())
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("result")


def chain_refs():
    """Live base fee (gwei), block number and timestamp for each chain."""
    refs = {}
    for name, urls in RPCS.items():
        for url in urls:
            try:
                b = _rpc(url, "eth_getBlockByNumber", ["latest", False])
                refs[name] = {"gwei": int(b["baseFeePerGas"], 16) / 1e9,
                              "block": int(b["number"], 16),
                              "ts": int(b["timestamp"], 16)}
                break
            except Exception:
                continue
    return refs


_GAS_KEYS = ("gas", "gwei", "fee", "base", "priority", "standard", "propose", "fast",
             "slow", "safe", "average", "maxfee", "price")


def find_gas(obj):
    """Every numeric candidate that could be a gas figure, with its key path.
    Classification is deliberately left to the audit against the raw response."""
    out = []

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower()
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if 0 < v < 1e6 and any(g in kl for g in _GAS_KEYS):
                        out.append({"value": float(v), "key": str(k), "path": f"{path}.{k}"})
                elif isinstance(v, str):
                    try:
                        fv = float(v.replace(",", "").lstrip("$"))
                        if 0 < fv < 1e6 and any(g in kl for g in _GAS_KEYS):
                            out.append({"value": fv, "key": str(k), "path": f"{path}.{k}"})
                    except (ValueError, AttributeError):
                        pass
                else:
                    walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")

    walk(obj, "")
    return out


def find_ts_or_block(obj, base_ref, eth_ref):
    """Staleness: a timestamp age in seconds or a block delta, if present."""
    tss, blocks = [], []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                kl = str(k).lower().replace("_", "")
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    if any(t in kl for t in ("timestamp", "time", "asof", "updated", "date")):
                        t = float(v) / (1000.0 if v > 1e12 else 1.0)
                        if 1.6e9 < t < 2.0e9:
                            tss.append(t)
                    if "block" in kl and 1e6 < v < 1e9:
                        blocks.append(int(v))
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    age = round(time.time() - max(tss), 1) if tss else None
    blk_delta = None
    if blocks:
        b = max(blocks)
        for ref in (base_ref, eth_ref):
            if ref and abs(b - ref["block"]) < 5000:
                blk_delta = ref["block"] - b
    return age, blk_delta


def gas_candidates(max_price):
    reg = json.load(open(os.path.join(DATA, "cdp_resources_raw.json")))
    seen, out = set(), []
    BAD = ("history", "chart", "forecast", "news", "tx-status", "wallet-balance", "withdraw",
           "permit2", "pqs", "robinhood", "hedera", "block-inspect", "chain-info", "chain-tokens")
    for r in reg:
        url = r.get("resource") or ""
        host = (urlparse(url).hostname or "").lower()
        blob = (url + " " + (r.get("description") or "")).lower()
        if not url.startswith("http") or host in seen:
            continue
        if "gas" not in blob or any(b in blob for b in BAD):
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
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--budget", type=float, default=0.12)
    ap.add_argument("--max-price", type=float, default=0.01)
    ap.add_argument("--pause", type=float, default=0.8)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    refs = chain_refs()
    if not refs:
        print("no chain reference; aborting so we do not grade blind")
        return 1
    for name, r in refs.items():
        print(f"  {name:<9} base fee {r['gwei']:.6f} gwei  block {r['block']:,}")
    print()

    cands = gas_candidates(a.max_price)[:a.limit]
    print(f"selected {len(cands)} gas sellers, advertised total ${sum(c['price'] for c in cands):.4f}")
    if a.dry_run:
        for c in cands:
            print(f"  ${c['price']:.4f} {c['method']:<4} {c['host']}")
        return 0

    bal0 = conform.wallet_balance()
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
        cands_gas = find_gas(payload) if ok and isinstance(payload, (dict, list)) else []
        age, blk = find_ts_or_block(payload, refs.get("base"), refs.get("ethereum")) if ok else (None, None)
        rows.append({"host": c["host"], "url": c["url"], "quoted": c["price"],
                     "paid": bool(paid.get("success")), "gas_fields": cands_gas,
                     "staleness_s": age, "block_delta": blk})
        n = len(cands_gas)
        top = cands_gas[0] if cands_gas else None
        shown = f"{top['value']:g} ({top['key']})" if top else "no gas number"
        stale = f" stale {age:.0f}s" if age else (f" {blk} blocks behind" if blk else "")
        print(f"  {i:>2}/{len(cands)} ${c['price']:.4f}  {n} fields  {shown:<28}{stale}  {c['host']}")
        time.sleep(a.pause)

    bal1 = conform.wallet_balance()
    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "asset": "gas price",
           "chain_refs": refs, "spent_reported": round(spent, 6),
           "spent_onchain": round(bal0 - bal1, 6) if (bal0 is not None and bal1 is not None) else None,
           "rows": rows}
    json.dump(out, open(os.path.join(DATA, "gas_shootout.json"), "w"), indent=1)
    got = [r for r in rows if r["gas_fields"]]
    print(f"\n{len(got)}/{len(rows)} returned a gas number")
    if bal0 is not None and bal1 is not None:
        print(f"  wallet moved ${bal0 - bal1:.6f} (reported ${spent:.6f})")
    print("  wrote data/gas_shootout.json (raw responses archived in captures/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
