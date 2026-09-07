#!/usr/bin/env python3
"""Who pays the long tail?

A normal buyer pays a handful of services it actually uses. A wallet that pays
dozens of unrelated endpoints tiny, regular amounts is doing something else:
keeping listings alive. The CDP registry delists a resource after roughly 30
days with no settled payment, and at least one service now sells a subscription
that fires those payments on a customer's behalf. This finds that pattern
without assuming who is doing it.

    python3 funders.py --days 3
    python3 funders.py --days 3 --sender 0x...   # everything one wallet paid

Scans USDC Transfer logs on Base filtered to the payTo addresses the registry
advertises, then groups by sender. Failed RPC ranges are counted and reported,
because a truncated scan looks exactly like a clean one.
"""
import argparse
import collections
import json
import os
import sys
import time
import urllib.request
from urllib.parse import urlparse

RPCS = ["https://base.drpc.org", "https://base-rpc.publicnode.com", "https://mainnet.base.org"]
UA = {"Content-Type": "application/json", "User-Agent": "whatagentsbuy-funders/0.1"}
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
BLOCKS_PER_DAY = 43200  # Base, 2s blocks

_fail = 0     # ranges whose data we genuinely never got
_probe = 0    # wide attempts that were refused and then retried narrower


def rpc(method, params, tries=3, count_fail=True):
    """count_fail=False for the deliberately-too-wide first attempt: a refusal
    there is how we discover the provider's cap, not a hole in the data. Mixing
    the two made a complete scan report 1,046 failures and look truncated."""
    global _fail, _probe
    for _ in range(tries):
        for url in RPCS:
            try:
                req = urllib.request.Request(
                    url, headers=UA,
                    data=json.dumps({"jsonrpc": "2.0", "id": 1,
                                     "method": method, "params": params}).encode())
                j = json.load(urllib.request.urlopen(req, timeout=45))
                if "result" in j:
                    return j["result"]
            except Exception:
                continue
        time.sleep(1)
    if count_fail:
        _fail += 1
    else:
        _probe += 1
    return None


def known_payto():
    """host -> payTo, and the reverse, from the registry snapshot."""
    reg = json.load(open(os.path.join(DATA, "cdp_resources_raw.json")))
    owner, addrs = {}, set()
    for r in reg:
        host = (urlparse(r.get("resource") or "").hostname or "").lower()
        if not host:
            continue
        for a in r.get("accepts") or []:
            p = (a.get("payTo") or "").lower()
            if p:
                addrs.add(p)
                owner.setdefault(p, host)
    return owner, sorted(addrs)


def scan(addrs, days, chunk=350, span=9000):
    """USDC transfers into `addrs` over the last `days`, grouped by sender.

    Ranges start wide and halve on refusal. Providers cap eth_getLogs by block
    span or result count and the cap differs per provider, so a fixed small span
    wastes hundreds of round trips while a fixed large one silently fails.
    """
    tip = rpc("eth_blockNumber", [])
    if not tip:
        raise SystemExit("cannot reach any Base RPC")
    tip = int(tip, 16)
    start = tip - int(days * BLOCKS_PER_DAY)
    pad = ["0x" + "0" * 24 + a[2:] for a in addrs]

    senders = collections.defaultdict(lambda: {"n": 0, "usdc": 0.0,
                                               "to": collections.Counter(),
                                               "first": None, "last": None})
    groups = [pad[i:i + chunk] for i in range(0, len(pad), chunk)]
    for gi, g in enumerate(groups, 1):
        lo, width = start, span
        while lo < tip:
            hi = min(lo + width - 1, tip)
            logs = rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi),
                                        "address": USDC, "topics": [TRANSFER, None, g]}],
                       tries=1, count_fail=False)
            if logs is None:
                if width > 500:                     # too wide, back off and retry
                    width = max(500, width // 2)
                    continue
                logs = rpc("eth_getLogs", [{"fromBlock": hex(lo), "toBlock": hex(hi),
                                            "address": USDC, "topics": [TRANSFER, None, g]}])
            for L in logs or []:
                frm = "0x" + L["topics"][1][-40:]
                to = "0x" + L["topics"][2][-40:]
                try:
                    v = int(L["data"], 16) / 1e6
                except Exception:
                    v = 0.0
                b = int(L["blockNumber"], 16)
                s = senders[frm]
                s["n"] += 1
                s["usdc"] += v
                s["to"][to] += 1
                s["first"] = b if s["first"] is None else min(s["first"], b)
                s["last"] = b if s["last"] is None else max(s["last"], b)
            lo = hi + 1
            width = min(span, int(width * 1.5))     # creep back up after success
        print(f"  group {gi}/{len(groups)} done", file=sys.stderr)
    return senders, tip, start


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=3)
    ap.add_argument("--sender")
    ap.add_argument("--min-payees", type=int, default=5)
    a = ap.parse_args()

    owner, addrs = known_payto()
    print(f"{len(addrs):,} advertised payTo addresses from the registry, "
          f"scanning {a.days} day(s) of Base USDC transfers into them\n", file=sys.stderr)
    senders, tip, start = scan(addrs, a.days)
    print(f"\nblocks {start:,} to {tip:,} | {_fail} range(s) genuinely lost "
          f"| {_probe} wide probe(s) refused and retried narrower")
    if _fail:
        print("  WARNING: this scan is incomplete, treat counts as floors\n")
    else:
        print("  complete: every block range in the window returned\n")

    if a.sender:
        s = senders.get(a.sender.lower())
        if not s:
            print("that wallet paid none of the listed endpoints in this window")
            return 0
        print(f"{a.sender}\n  {s['n']} payments  ${s['usdc']:,.4f}  "
              f"{len(s['to'])} distinct endpoints\n")
        for to, n in s["to"].most_common():
            print(f"    {n:>4} x  {owner.get(to, '?'):<38} {to}")
        return 0

    rows = [(f, v) for f, v in senders.items() if len(v["to"]) >= a.min_payees]
    rows.sort(key=lambda kv: (-len(kv[1]["to"]), -kv[1]["n"]))
    print(f'{"wallet":<44}{"payees":>7}{"pays":>7}{"USDC":>12}{"avg":>11}')
    print("-" * 81)
    for f, v in rows[:25]:
        avg = v["usdc"] / v["n"] if v["n"] else 0
        print(f'{f:<44}{len(v["to"]):>7}{v["n"]:>7}{v["usdc"]:>12,.4f}{avg:>11.6f}')
    print(f"\n{len(rows)} wallet(s) paid {a.min_payees}+ distinct listed endpoints in this window.")
    print("A wallet paying many unrelated endpoints tiny amounts is the keepalive signature.")
    print("Run again with --sender to see exactly what one of them paid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
