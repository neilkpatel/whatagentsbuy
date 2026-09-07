#!/usr/bin/env python3
"""Independent on-chain demand, keyed on the seller addresses we collect ourselves.

Why this exists: the public registry keeps its own usage counters and they
undercount badly (for one large seller it logs ~4,200 calls in 30 days while the
chain shows 150k-228k settlements per day). x402scan solves this by indexing USDC
Transfer events filtered to known facilitator addresses, via paid providers
(Coinbase CDP SQL, Bitquery, BigQuery) into ClickHouse.

We do the inverse, and it costs nothing: every graded service hands us its payTo
address in its own payment challenge, so we ask the chain what actually landed
there. Source: Blockscout's Base instance, free and unauthenticated.

We do not paginate through millions of transfers. We take the most recent page of
settlements per address and derive velocity, distinct payers and revenue rate from
it. That is enough to separate a live business from a listing, and it is honest
about being a sample.
"""
import json, os, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
API = "https://base.blockscout.com/api"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
UA = {"User-Agent": "touchstone-probe/0.1 (+https://touchstone.neilkpatel.com)"}
PAGE = 100          # most recent settlements sampled per address
WORKERS = 6         # polite against a free public indexer
RETRIES = 2


def fetch(addr):
    q = urllib.parse.urlencode({
        "module": "account", "action": "tokentx", "address": addr,
        "contractaddress": USDC, "page": 1, "offset": PAGE, "sort": "desc",
    })
    for attempt in range(RETRIES + 1):
        try:
            req = urllib.request.Request(f"{API}?{q}", headers=dict(UA))
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            return d.get("result") or []
        except Exception:
            if attempt == RETRIES:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def summarise(addr, rows, now=None):
    """Turn a page of transfers into demand facts. Inbound transfers only."""
    now = now or time.time()
    a = addr.lower()
    inb = [t for t in rows if (t.get("to") or "").lower() == a]
    if not inb:
        return {"address": addr, "settlements": 0, "sampled": len(rows)}

    ts = sorted(int(t["timeStamp"]) for t in inb)
    payers = {(t.get("from") or "").lower() for t in inb}
    total = sum(int(t.get("value", 0)) for t in inb) / 1e6
    span_h = max(1e-9, (ts[-1] - ts[0]) / 3600)
    # A full page means the sample is truncated, so the rate is a floor.
    truncated = len(rows) >= PAGE
    per_day = (len(inb) / span_h) * 24 if len(inb) > 1 else 0

    return {
        "address": addr,
        "settlements": len(inb),
        "sampled": len(rows),
        "truncated": truncated,
        # Payer count is only meaningful relative to the window it was measured
        # over. For a seller doing thousands of settlements an hour, 100 rows
        # covers seconds, so a low count here means "few payers in those
        # seconds", never "few customers".
        "window_hours": round(span_h, 3),
        "unique_payers": len(payers),
        "usdc_in_sample": round(total, 4),
        "avg_ticket": round(total / len(inb), 6) if inb else None,
        "first_ts": ts[0], "last_ts": ts[-1],
        "hours_since_last": round((now - ts[-1]) / 3600, 1),
        "settlements_per_day": round(per_day, 1),
        "usd_per_day": round((total / span_h) * 24, 4) if len(inb) > 1 else 0,
    }


def main():
    latest = json.load(open(os.path.join(DATA, "latest.json")))
    origins = latest["origins"]

    # map every graded service to the addresses its endpoints actually ask us to pay
    by_addr = {}
    for r in origins:
        seen = []
        for c in r["checked"]:
            a = (c.get("live_payto") or c.get("adv_payto") or "")
            if a.startswith("0x") and a.lower() not in [s.lower() for s in seen]:
                seen.append(a)
        r["payto_addresses"] = seen
        for a in seen:
            by_addr.setdefault(a.lower(), []).append(r["origin"])

    addrs = sorted(by_addr)
    print(f"resolving {len(addrs)} seller addresses on Base (Blockscout, free)")

    out, done = {}, 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(fetch, a): a for a in addrs}
        for f, a in futs.items():
            rows = f.result()
            out[a] = summarise(a, rows) if rows is not None else {"address": a, "error": True}
            done += 1
            if done % 100 == 0:
                print(f"  {done}/{len(addrs)}")

    # attach to each service: sum settlements, union payers, freshest timestamp
    live = 0
    for r in origins:
        recs = [out.get(a.lower()) for a in r.get("payto_addresses", [])]
        recs = [x for x in recs if x and not x.get("error") and x.get("settlements")]
        if not recs:
            r["onchain"] = None
            continue
        r["onchain"] = {
            "settlements_sampled": sum(x["settlements"] for x in recs),
            "unique_payers": max(x["unique_payers"] for x in recs),
            "settlements_per_day": round(sum(x["settlements_per_day"] for x in recs), 1),
            "usd_per_day": round(sum(x["usd_per_day"] for x in recs), 4),
            "avg_ticket": min(x["avg_ticket"] for x in recs if x["avg_ticket"] is not None),
            "hours_since_last": min(x["hours_since_last"] for x in recs),
            "truncated": any(x.get("truncated") for x in recs),
        }
        live += 1

    latest["onchain_source"] = "Blockscout Base USDC transfers to seller payTo addresses"
    latest["onchain_generated"] = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    json.dump(latest, open(os.path.join(DATA, "latest.json"), "w"), indent=1)
    json.dump(out, open(os.path.join(DATA, f"onchain_{time.strftime('%Y-%m-%d')}.json"), "w"), indent=1)

    got = [x for x in out.values() if x.get("settlements")]
    print(f"\naddresses with inbound USDC: {len(got)}/{len(addrs)}")
    print(f"services with on-chain demand: {live}/{len(origins)}")
    top = sorted(origins, key=lambda r: -((r.get("onchain") or {}).get("settlements_per_day") or 0))[:10]
    for r in top:
        o = r.get("onchain") or {}
        if o:
            print(f"  {o['settlements_per_day']:>10,.0f}/day  ${o['usd_per_day']:>9,.2f}/day  "
                  f"{o['unique_payers']:>3} payers  {r['origin'][:38]}")


if __name__ == "__main__":
    main()
