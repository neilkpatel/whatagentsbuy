#!/usr/bin/env python3
"""Reconcile a conformance file's client-reported `charged` against the chain.

agentcash's meta.price (what conform records as `charged`) is self-reported and
unreliable in BOTH directions: on 2026-08-30 it reported $0.02 for calls where
the chain shows the seller received exactly the advertised $0.015 (20 phantom
overcharge flags against honest sellers), and nothing prevents the mirror case,
a client UNDER-reporting a real overcharge. The chain is truth, so every row
with a settled transaction gets reconciled, not only the ones the client
already flagged: checking only client-reported anomalies would let the client's
own blind spot decide what we verify.

States, explicit so an unknown can never harden into a fact:
  charged_reconciled = True    chain read; `charged` is the on-chain amount and
                               `charged_reported` preserves the client figure
                               when the two disagreed
  charged_verified   = False   an RPC could not be reached for this tx; the
                               client figure REMAINS but is marked unverified,
                               and no claim may be built on it
  (neither field)              no settled tx recorded; nothing to reconcile,
                               and "no tx" is itself not proof nothing settled

Fail-open on RPC failure means: keep the client number, mark it unverified,
never invent an accusation from it. See the 2026-08-30 correction study and
verify-before-accusing.
"""
import json, os, sys, time, urllib.request

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WALLET = "0xc533bf5268a2f64adde58dce380651f70aa92d7a"
UA = {"content-type": "application/json", "User-Agent": "touchstone-probe/0.1 (+https://whatagentsbuy.com)"}
RPCS = ([os.environ["BASE_RPC_URL"]] if os.environ.get("BASE_RPC_URL") else []) + \
       ["https://base.drpc.org", "https://base-rpc.publicnode.com"]

# Reconciling every settled row means hundreds of RPC lookups on a big run.
# Tolerance for "client agrees with chain": exact to the micro-USDC.
TOL = 1e-6


def onchain_paid(tx):
    """USDC that actually left our wallet in this tx, or None if unreachable."""
    if not tx:
        return None
    for u in RPCS:
        try:
            req = urllib.request.Request(u, headers=UA, data=json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx]}).encode())
            rc = json.load(urllib.request.urlopen(req, timeout=20)).get("result")
            if not rc:
                continue
            total = 0.0
            for lg in rc.get("logs", []):
                if (lg["address"].lower() == USDC and lg["topics"][0].lower() == TRANSFER
                        and ("0x" + lg["topics"][1][-40:]).lower() == WALLET):
                    total += int(lg["data"], 16) / 1e6
            return round(total, 6)
        except Exception:
            continue
    return None


def reconcile(path, lookup=onchain_paid):
    """Reconcile EVERY row with a settled tx in one conformance file.

    Returns (checked, corrected, cleared, unverified):
      checked    rows with a tx that were looked up (or already reconciled)
      corrected  rows whose client figure disagreed with the chain (either
                 direction) and were corrected to the on-chain amount
      cleared    the subset of corrections that erased an apparent overcharge
      unverified rows whose RPC lookup failed; marked charged_verified=False
    `lookup` is injectable so tests never touch the network.
    """
    d = json.load(open(path))
    rows = d.get("rows", [])
    checked = corrected = cleared = unverified = 0
    changed = False
    for r in rows:
        if not r.get("tx") or r.get("charged_reconciled"):
            continue
        real = lookup(r["tx"])
        if real is None:
            # Unknown stays unknown: the client figure survives but carries an
            # explicit unverified mark instead of silently reading as a fact.
            if r.get("charged_verified") is not False:
                r["charged_verified"] = False
                changed = True
            unverified += 1
            continue
        checked += 1
        c, q = r.get("charged"), r.get("quoted")
        r.pop("charged_verified", None)          # a chain read supersedes the mark
        r["charged_reconciled"] = True
        changed = True
        if c is not None and abs((c or 0) - real) > TOL:
            r["charged_reported"] = c            # keep the client figure for the record
            r["charged"] = real                  # chain truth, both directions
            corrected += 1
            if q is not None and (c or 0) > q * 1.001 and real <= q * 1.001:
                cleared += 1                     # was a phantom overcharge, now cleared
        elif c is None:
            r["charged"] = real
            corrected += 1
    if changed:
        json.dump(d, open(path, "w"), indent=1)
    return checked, corrected, cleared, unverified


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", f"conformance_{time.strftime('%Y-%m-%d')}.json")
    checked, corrected, cleared, unverified = reconcile(p)
    print(f"reconciled {checked} settled rows against the chain in {os.path.basename(p)}: "
          f"{corrected} client figures corrected ({cleared} phantom overcharges cleared), "
          f"{unverified} unreachable and marked charged_verified=false")
