#!/usr/bin/env python3
"""How much of the money landing on seller payTo addresses is actually x402?

A payTo is just an address. Anyone can send USDC to it for any reason: treasury
moves, funding, OTC, a multisig operation. The settlement tape counts every USDC
Transfer into the seller set, so it cannot tell an agent paying for an API call
from a company moving its own money into the same wallet.

The EIP-3009 finding gives us the separator. An x402 payment is signed off-chain
by the buyer and submitted by a facilitator, so it always arrives via
`transferWithAuthorization` (selector 0xe3ee160e). An ordinary transfer arrives
via `transfer()`, `transferFrom()`, a Safe `execTransaction`, a bridge, or a DEX
route. Classifying by the method that CARRIED the transfer splits the two.

Resolving every settlement costs one eth_getTransactionByHash each (~17k/day), so
this SAMPLES: N evenly spaced windows across a range, every seller-inbound
transfer inside them, each classified by its carrying transaction. The sampling
method is recorded in the output and must be stated wherever a number from it is
published.

BURN/MINT EXCLUSION IS NOT OPTIONAL. library.proofivy.com advertises 0x0 as its
payTo, so every USDC burn on Base maps to the seller set. That produced a $138M
phantom headline on 8/18, and an earlier version of THIS script reproduced it:
27 `burn()` calls carried $23.8M of a $35.4M sample. sweep.decode() and
buyers.decode_buyers() both filter it; anything reading these logs must too.

  python3 classify_settlements.py --windows 8 --hours 6
"""
import argparse
import json
import os
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import buyer_probe
import buyers
import sweep

HERE = os.path.dirname(os.path.abspath(__file__))
PROBES = os.path.join(HERE, "data", "probes")

# Selectors seen carrying USDC into seller addresses. Only the first is x402.
SELECTORS = {
    "0xe3ee160e": "x402 (EIP-3009 transferWithAuthorization)",
    "0xa9059cbb": "plain ERC20 transfer()",
    "0x23b872dd": "plain ERC20 transferFrom()",
    "0x6a761202": "Gnosis Safe execTransaction",
    "0x42966c68": "burn()",
}
X402_SELECTOR = "0xe3ee160e"
EXCLUDE = sweep.BURN_ADDRS | buyers._MINTS


def rpc(url, method, params, tries=3, timeout=60):
    for _ in range(tries):
        try:
            return sweep.jrpc(url, method, params, timeout=timeout)
        except Exception as e:
            if sweep.is_endpoint_refusal(e):
                return None
            time.sleep(1.0)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=8)
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--threads", type=int, default=3)
    ap.add_argument("--min-resolved", type=float, default=0.95,
                    help="refuse to report below this share of transactions resolved")
    a = ap.parse_args()

    latest = json.load(open(os.path.join(HERE, "data", "latest.json")))
    addrs = sorted(buyer_probe.seller_addresses(latest) - EXCLUDE)
    topics = [sweep.topic_for(x) for x in addrs]
    batches = [topics[i:i + sweep.ADDR_BATCH] for i in range(0, len(topics), sweep.ADDR_BATCH)]
    url, tip = sweep.pick_rpc(sample_topics=batches[0] if batches else None)
    span = int(a.hours * 3600 / sweep.BLOCK_SECONDS)
    lo, hi = tip - span, tip
    print(f"{len(addrs):,} seller addresses (burn/mint excluded), endpoint {url}")

    W = sweep.WINDOW_BLOCKS
    starts = [lo + int(i * (hi - lo - W) / max(a.windows - 1, 1)) for i in range(a.windows)]
    tx_of = {}
    for s in starts:
        for bt in batches:
            r = rpc(url, "eth_getLogs", [{"address": sweep.USDC_BASE,
                                          "topics": [sweep.TRANSFER, None, bt],
                                          "fromBlock": hex(s), "toBlock": hex(s + W - 1)}])
            for L in (r or []):
                frm = ("0x" + L["topics"][1][-40:]).lower()
                to = ("0x" + L["topics"][2][-40:]).lower()
                if frm in EXCLUDE or to in EXCLUDE:
                    continue
                tx_of.setdefault(L["transactionHash"], []).append(
                    (frm, to, int(L["data"], 16) / 1e6))
    print(f"{len(tx_of):,} transactions carry seller-inbound USDC in {a.windows} x {W}-block windows")

    def fetch(h):
        for attempt in range(4):
            tx = rpc(url, "eth_getTransactionByHash", [h], tries=1, timeout=30)
            if tx:
                return h, tx
            time.sleep(0.4 * (attempt + 1))   # rate limited: back off, do not drop
        return h, None

    resolved = {}
    with ThreadPoolExecutor(max_workers=a.threads) as ex:
        for i, (h, tx) in enumerate(ex.map(fetch, list(tx_of)), 1):
            if tx:
                resolved[h] = tx["input"][:10]
            if i % 500 == 0:
                print(f"  resolved {i:,}/{len(tx_of):,}")

    # A sample that resolved a quarter of its transactions prints exactly like one
    # that resolved all of them, and the shares it reports are whatever survived
    # the rate limiter. The first run of this script resolved 888 of 3,868 (23%)
    # against a threaded pool and produced a confident-looking 33% dollar share
    # from the 23% it happened to get. Refuse to report below the floor.
    rate = len(resolved) / max(len(tx_of), 1)
    if rate < a.min_resolved:
        raise SystemExit(
            f"refusing to report: only {len(resolved):,}/{len(tx_of):,} transactions resolved "
            f"({rate:.1%}, floor {a.min_resolved:.0%}). The endpoint is refusing lookups, so the "
            f"surviving sample is selected by the rate limiter, not by chance. Lower --threads, "
            f"lower --windows, or set BASE_RPC_URL to a keyed endpoint.")

    cnt, usd = Counter(), Counter()
    for h, evs in tx_of.items():
        sel = resolved.get(h)
        if sel is None:
            continue
        k = SELECTORS.get(sel, f"other {sel}")
        cnt[k] += len(evs)
        usd[k] += sum(v for _, _, v in evs)
    tc, tu = sum(cnt.values()), sum(usd.values()) or 1.0

    x_label = SELECTORS[X402_SELECTOR]
    out = {"kind": "probe", "not_the_tape": True,
           "method": (f"{a.windows} evenly spaced {W}-block windows across the last {a.hours}h; "
                      "every USDC Transfer into an advertised payTo inside them, classified by "
                      "the method of the transaction that carried it. Burn/mint addresses "
                      "excluded on both sides. SAMPLED, not a census."),
           "generated": time.strftime("%Y-%m-%dT%H%M%S"), "endpoint": url,
           "from_block": lo, "to_block": hi, "windows": starts,
           "transactions": len(tx_of), "resolved": len(resolved),
           "transfers": tc, "usdc": round(tu, 6),
           "x402_transfer_share": round(cnt[x_label] / tc, 4) if tc else None,
           "x402_dollar_share": round(usd[x_label] / tu, 6),
           "by_method": {k: {"transfers": cnt[k], "usdc": round(usd[k], 6)} for k in cnt}}
    os.makedirs(PROBES, exist_ok=True)
    p = os.path.join(PROBES, f"method_split_{out['generated']}.json")
    json.dump(out, open(p, "w"), indent=1)

    print(f"\n{len(resolved):,}/{len(tx_of):,} transactions resolved | {tc:,} transfers | ${tu:,.2f}\n")
    print(f'{"method":<44}{"transfers":>10}{"share":>8}{"USDC":>14}{"share":>8}')
    print("-" * 84)
    for k, _ in usd.most_common(12):
        print(f"{k:<44}{cnt[k]:>10,}{cnt[k]/tc:>8.1%}{usd[k]:>14,.2f}{usd[k]/tu:>8.1%}")
    print(f"\nx402: {cnt[x_label]:,} of {tc:,} transfers ({cnt[x_label]/tc:.1%}), "
          f"${usd[x_label]:,.2f} of ${tu:,.2f} ({usd[x_label]/tu:.2%} of dollars)")
    print(f"saved {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
