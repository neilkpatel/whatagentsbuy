#!/usr/bin/env python3
"""Ask sanctions-screening services whether known OFAC-sanctioned wallets are flagged.

Ground truth is objective and public: an address is on the OFAC SDN list or it is
not. We feed each screener several addresses currently on the SDN list plus clean
controls, and record whether it flags each. A screener that clears a genuinely
sanctioned wallet is the finding, and because that is a serious claim, every cell
is re-checked against the archived raw response before anything is concluded.

Run: python3 aml_shootout.py
"""
import json
import os
import time
import urllib.parse

import conform

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# 3 currently-OFAC-SDN-listed ETH addresses + 2 clean controls (verified off-list).
ADDRS = [
    ("0x0330070FD38Ec3bB94F58FA55D40368271E9e54A", "sanctioned"),
    ("0x5a7a51bfb49f190e5a6060a5bc6052ac14a3b59f", "sanctioned"),
    ("0xb6f5ec1a0a9cd1526536d3f0426c429529471f40", "sanctioned"),
    ("0xC533Bf5268A2F64aDDe58dcE380651f70Aa92D7A", "clean (our wallet)"),
    ("0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "clean (vitalik.eth)"),
]

# Each screener, with how to send it an address. Kept explicit rather than
# auto-retargeted, because a sanctions test must call each service exactly right.
SCREENERS = [
    {"host": "api.anchor-x402.com", "method": "POST",
     "url": lambda a: "https://api.anchor-x402.com/v1/screen", "body": lambda a: {"wallet": a}},
    {"host": "lionx402.com", "method": "GET",
     "url": lambda a: "https://lionx402.com/api/x402/sanctions-screen-json?" + urllib.parse.urlencode({"address": a}),
     "body": lambda a: None},
    {"host": "trust-agent.io", "method": "GET",
     "url": lambda a: "https://trust-agent.io/v1/verify/quick?" + urllib.parse.urlencode({"address": a, "chain": "ethereum", "type": "wallet"}),
     "body": lambda a: None},
]

_HIT = ("sanction", "ofac", "flag", "block", "hit", "match", "listed", "denied", "illicit")
_CLEAR = ("clear", "clean", "not_sanctioned", "no match", "no_match", "not found", "pass")


def classify(payload):
    """Heuristic verdict from a screener response: flagged / clear / unclear.
    Deliberately conservative; the raw response is archived for a hand audit."""
    if not isinstance(payload, (dict, list)):
        return "unclear"
    blob = json.dumps(payload).lower()
    # explicit booleans first
    def find_bool(o):
        hits = []
        def w(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    kl = str(k).lower()
                    if any(h in kl for h in ("sanction", "ofac", "flagged", "blocked", "isclean", "clean", "match")):
                        if isinstance(v, bool):
                            hits.append((kl, v))
                    w(v)
            elif isinstance(x, list):
                for v in x:
                    w(v)
        w(o)
        return hits
    bools = find_bool(payload)
    for kl, v in bools:
        if ("clean" in kl or "isclean" in kl) and v is True:
            return "clear"
        if ("sanction" in kl or "ofac" in kl or "flagged" in kl or "blocked" in kl or "match" in kl) and v is True:
            return "flagged"
        if ("sanction" in kl or "ofac" in kl or "flagged" in kl or "blocked" in kl) and v is False:
            return "clear"
    # risk level words
    if any(w in blob for w in ("\"high\"", "\"critical\"", "\"severe\"", "risk_high")):
        return "flagged"
    if any(w in blob for w in ("\"low\"", "\"none\"", "risk_low", "no risk")):
        return "clear" if not any(h in blob for h in ("sanction", "ofac")) or "false" in blob else "unclear"
    if any(c in blob for c in _CLEAR):
        return "clear"
    if any(h in blob for h in _HIT):
        return "flagged"
    return "unclear"


def main():
    bal0 = conform.wallet_balance()
    print(f"wallet before: ${bal0:.6f}\n" if bal0 is not None else "")
    print(f"{'service':<24}" + "".join(f"{lbl.split()[0][:11]:>13}" for _, lbl in ADDRS))
    print("-" * (24 + 13 * len(ADDRS)))
    rows, spent = [], 0.0
    for s in SCREENERS:
        cells = []
        line = f"{s['host']:<24}"
        for addr, lbl in ADDRS:
            url = s["url"](addr)
            cap = 0.01
            ok, payload, meta = conform.call(url, s["method"], s["body"](addr), cap)
            paid = (meta or {}).get("payment") if isinstance((meta or {}).get("payment"), dict) else {}
            if paid.get("success"):
                spent += cap
            v = classify(payload) if ok else "no response"
            cells.append({"address": addr, "expect": lbl, "verdict": v})
            line += f"{v:>13}"
            time.sleep(0.7)
        rows.append({"host": s["host"], "cells": cells})
        print(line)
    bal1 = conform.wallet_balance()
    out = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
           "ground_truth": "OFAC SDN list (ETH), mirror updated 2026-08-08",
           "addresses": [{"address": a, "expect": l} for a, l in ADDRS],
           "spent_onchain": round(bal0 - bal1, 6) if (bal0 is not None and bal1 is not None) else None,
           "rows": rows}
    json.dump(out, open(os.path.join(DATA, "aml_shootout.json"), "w"), indent=1)
    print(f"\nwallet moved ${bal0 - bal1:.6f}" if (bal0 is not None and bal1 is not None) else "")
    # the finding: any sanctioned address a screener did not flag
    misses = [(r["host"], c["address"]) for r in rows for c in r["cells"]
              if c["expect"] == "sanctioned" and c["verdict"] not in ("flagged",)]
    print(f"sanctioned-address results that were NOT 'flagged': {len(misses)} (audit each against raw before trusting)")
    for h, a in misses:
        print(f"  {h}  {a}")
    print("wrote data/aml_shootout.json (raw responses archived in captures/)")


if __name__ == "__main__":
    main()
