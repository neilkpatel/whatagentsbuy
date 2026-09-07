#!/usr/bin/env python3
"""Solana settlement sweep: the other half of the tape.

The Base sweep answers "who got paid on Base". 41% of the services we track also
quote Solana, and we proved the gap is real by making a payment ourselves that
our Base-only tape never saw. This closes it for Solana, which is the dominant
non-Base chain in this market by a wide margin.

Solana is not EVM, so the method differs from sweep.py:
  1. getTokenAccountsByOwner(wallet, {mint: USDC})  -> the wallet's USDC account
  2. getSignaturesForAddress(that account, since <cutoff>)  -> recent activity
  3. getTransaction(sig)  -> the exact USDC delta and the counterparty

Everything is public JSON-RPC, no key. Public RPCs rate-limit hard, so this
paces itself and counts failures, and like the Base sweep a partial result is
reported as a floor rather than passed off as complete.

    python3 sweep_solana.py --hours 24
    python3 sweep_solana.py --hours 24 --wallets 8EgAC...,53Jhu...
"""
import argparse, json, os, sys, time, urllib.request
from collections import defaultdict
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
RPCS = ["https://api.mainnet-beta.solana.com",
        "https://solana-rpc.publicnode.com",
        "https://rpc.ankr.com/solana"]
UA = {"Content-Type": "application/json", "User-Agent": "whatagentsbuy-sol/0.1"}

_fail = 0
_rpc_i = 0


def rpc(method, params, tries=4):
    """Round-robin across public RPCs, backing off on rate limits."""
    global _fail, _rpc_i
    for attempt in range(tries):
        url = RPCS[(_rpc_i + attempt) % len(RPCS)]
        try:
            req = urllib.request.Request(
                url, headers=UA,
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                 "params": params}).encode())
            j = json.load(urllib.request.urlopen(req, timeout=30))
            if "result" in j:
                _rpc_i = (_rpc_i + attempt) % len(RPCS)
                return j["result"]
            if j.get("error", {}).get("code") == -32005:   # rate limited
                time.sleep(1.5 * (attempt + 1))
        except Exception:
            time.sleep(1.0 * (attempt + 1))
    _fail += 1
    return None


def usdc_account(wallet):
    """The wallet's USDC token account, or None if it has never held USDC."""
    res = rpc("getTokenAccountsByOwner",
              [wallet, {"mint": USDC_MINT}, {"encoding": "jsonParsed"}])
    if not res or not res.get("value"):
        return None
    return res["value"][0]["pubkey"]


def sweep(wallets, hours):
    """Return per-wallet USDC inflow over the window, and metadata."""
    cutoff = time.time() - hours * 3600
    per = defaultdict(lambda: {"settlements": 0, "usdc": 0.0, "payers": set(),
                               "chain": "solana"})
    checked = 0
    for w in wallets:
        acct = usdc_account(w)
        if not acct:
            continue
        before = None
        stop = False
        while not stop:
            params = [acct, {"limit": 100}]
            if before:
                params[1]["before"] = before
            sigs = rpc("getSignaturesForAddress", params)
            if not sigs:
                break
            for s in sigs:
                bt = s.get("blockTime")
                if bt is not None and bt < cutoff:
                    stop = True
                    break
                if s.get("err"):
                    continue
                tx = rpc("getTransaction",
                         [s["signature"], {"encoding": "jsonParsed",
                                           "maxSupportedTransactionVersion": 0}])
                if not tx:
                    continue
                delta, payer = usdc_inflow(tx, acct, w)
                if delta > 0:
                    p = per[w]
                    p["settlements"] += 1
                    p["usdc"] += delta
                    if payer:
                        p["payers"].add(payer)
                time.sleep(0.12)   # be gentle with the public RPC
            if len(sigs) < 100:
                break
            before = sigs[-1]["signature"]
        checked += 1
        if checked % 10 == 0:
            print(f"    {checked}/{len(wallets)} wallets, {_fail} rpc failures",
                  file=sys.stderr)
    out = {}
    for w, v in per.items():
        out[w] = {"settlements": v["settlements"], "usdc": round(v["usdc"], 6),
                  "unique_payers": len(v["payers"]), "chain": "solana"}
    return out, {"provider": "solana public rpc", "wallets_with_usdc_account": checked,
                 "rpc_failures": _fail, "hours": hours}


def usdc_inflow(tx, acct, owner):
    """USDC credited to `acct` in this tx, and the paying owner if determinable.

    Compares the token balance of the account before and after. Reading the delta
    off the ledger is exact and does not depend on parsing instruction shapes,
    which vary by wallet.
    """
    meta = tx.get("meta") or {}
    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances", [])
           if b.get("mint") == USDC_MINT}
    post = {b["accountIndex"]: b for b in meta.get("postTokenBalances", [])
            if b.get("mint") == USDC_MINT}
    keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])

    def idx_of(pubkey):
        for i, k in enumerate(keys):
            kk = k.get("pubkey") if isinstance(k, dict) else k
            if kk == pubkey:
                return i
        return None

    ai = idx_of(acct)
    if ai is None or ai not in post:
        return 0.0, None
    before = float((pre.get(ai) or {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
    after = float(post[ai]["uiTokenAmount"].get("uiAmount") or 0)
    delta = after - before
    if delta <= 0:
        return 0.0, None
    # the payer is whichever USDC account went DOWN by ~the same amount
    payer_owner = None
    for i, b in post.items():
        if i == ai:
            continue
        pb = float((pre.get(i) or {}).get("uiTokenAmount", {}).get("uiAmount") or 0)
        pa = float(b["uiTokenAmount"].get("uiAmount") or 0)
        if pb - pa >= delta - 1e-9:
            payer_owner = b.get("owner")
            break
    return delta, payer_owner


def tracked_solana_wallets():
    """Solana payTo addresses for every host we grade or rank."""
    reg = json.load(open(os.path.join(DATA, "cdp_resources_raw.json")))
    try:
        lb = json.load(open(os.path.join(DATA, "leaderboard.json")))
        tracked = {r["host"] for r in lb["windows"]["7d"]["rows"] if r.get("host")}
    except Exception:
        tracked = set()
    tracked |= {(p.get("api") or {}).get("name", "")
                for p in json.load(open(os.path.join(DATA, "feed.json"))) if p.get("grade")}
    tracked.discard("")
    wallets = {}
    for r in reg:
        h = (urlparse(r.get("resource") or "").hostname or "").lower()
        if h not in tracked:
            continue
        for a in (r.get("accepts") or []):
            if (a.get("network") or "").startswith("solana") and a.get("payTo"):
                wallets[a["payTo"]] = h
    return wallets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--wallets", help="comma-separated, overrides the tracked set")
    a = ap.parse_args()

    if a.wallets:
        wmap = {w.strip(): "?" for w in a.wallets.split(",") if w.strip()}
    else:
        wmap = tracked_solana_wallets()
    print(f"sweeping {len(wmap)} Solana payTo address(es) over {a.hours}h", file=sys.stderr)
    out, meta = sweep(list(wmap), a.hours)
    for w in out:
        out[w]["host"] = wmap.get(w, "?")
    tot = sum(v["usdc"] for v in out.values())
    print(f"\n{len(out)} wallet(s) received USDC on Solana, ${tot:,.4f} total, "
          f"{meta['rpc_failures']} rpc failures")
    for w, v in sorted(out.items(), key=lambda x: -x[1]["usdc"])[:12]:
        print(f"  ${v['usdc']:>10,.4f}  {v['settlements']:>4} tx  {v['host'][:30]:<31} {w[:16]}...")
    stamp = time.strftime("%Y-%m-%d")
    path = os.path.join(DATA, "history", f"settlements_solana_{stamp}.json")
    # Refuse to overwrite a good same-day sweep with a materially smaller one. A
    # rerun that hit rate limits or died partway would otherwise replace real
    # settlement history with a truncated file, and this tape cannot be rebuilt.
    if meta.get("rpc_failures") and os.path.exists(path):
        try:
            old_tot = sum(v.get("usdc", 0.0) for v in json.load(open(path)).get("by_wallet", {}).values())
            if old_tot > tot * 1.1 + 1:
                print(f"\nREFUSING to overwrite {os.path.basename(path)}: existing sweep has "
                      f"${old_tot:,.2f} vs this run's ${tot:,.2f} with {meta['rpc_failures']} rpc failures. "
                      "Keeping the larger existing file.")
                return 1
        except Exception:
            pass
    json.dump({"chain": "solana", "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
               "hours": a.hours, "meta": meta, "by_wallet": out},
              open(path, "w"), indent=1)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
