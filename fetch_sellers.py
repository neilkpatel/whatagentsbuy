#!/usr/bin/env python3
"""Pull the x402scan seller list — the half of the market the CDP registry cannot see.

Why this exists: Coinbase's CDP discovery registry is not the market. Measured
2026-08-04, CDP attributes 3.0% of x402scan's transactions, and unevenly: 95.5%
coverage for x402.twit.sh, 0.05% for blockrun.ai, and 0% for claw402.ai,
api.clusterprotocol.ai, x402.dtelecom.org and api.vishwalab.com, which never
registered at all. Probing only the CDP registry misses 1,197 of 2,037
revenue-earning origins, about 35% of all seller dollars — including the #2
seller by revenue. This module supplies the missing half so probe.py can grade
the market rather than a Coinbase directory.

Source: x402scan undocumented tRPC `public.sellers.bazaar.list` (no auth,
browser UA required). `total_amount` is micro-USDC.

PAGINATION GOTCHA: the endpoint returns VARIABLE page sizes (88, then 95, then
99...) and a null `total`. The obvious `if len(items) < page_size: break` loop
stops on page 0 and yields 88 of 908 sellers — a 10x undercount. Walk until a
page comes back EMPTY, and dedup by recipient wallet, because one seller record
can carry several origins (http:// and https:// variants of the same host).
"""
import json, os, time, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
X = "https://www.x402scan.com/api/trpc/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEFRAME = 30
MAX_PAGES = 60


def trpc(proc, payload, tries=3):
    """Call a tRPC procedure. The `meta` block tells the server chain is
    undefined; include it ONLY when chain is None or the filter is ignored."""
    env = {"0": {"json": payload}}
    if payload.get("chain") is None:
        env["0"]["meta"] = {"values": {"chain": ["undefined"]}, "v": 1}
    url = f"{X}{proc}?batch=1&input={urllib.parse.quote(json.dumps(env))}"
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)[0]["result"]["data"]["json"]
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"tRPC {proc} failed after {tries} tries: {last}")


def host_of(url):
    if not isinstance(url, str):
        return ""
    return url.replace("https://", "").replace("http://", "").split("/")[0].strip().lower()


def fetch_sellers():
    """Walk every page until one comes back empty. Dedup by recipient wallet."""
    sellers, seen, page = [], set(), 0
    while page < MAX_PAGES:
        d = trpc("public.sellers.bazaar.list",
                 {"timeframe": TIMEFRAME, "sorting": {"id": "total_amount", "desc": True},
                  "pagination": {"page": page, "page_size": 100}})
        items = d.get("items", [])
        if not items:
            break
        for it in items:
            key = tuple(sorted(it.get("recipients") or [])) + (it.get("tx_count"),)
            if key in seen:
                continue
            seen.add(key)
            sellers.append(it)
        page += 1
    return sellers, page


def main():
    sellers, pages = fetch_sellers()
    if not sellers:
        raise SystemExit("x402scan returned no sellers; keeping the previous snapshot")

    by_host = {}
    for s in sellers:
        usd = (s.get("total_amount") or 0) / 1e6
        tx = s.get("tx_count") or 0
        buyers = s.get("unique_buyers") or 0
        for o in (s.get("origins") or []):
            h = host_of(o.get("origin"))
            if not h:
                continue
            # A seller's volume belongs to the seller, not to each of its origin
            # aliases — take the max rather than summing, or multi-origin sellers
            # get counted several times over.
            cur = by_host.get(h)
            if cur is None or usd > cur["usd"]:
                by_host[h] = {"host": h, "usd": round(usd, 6), "tx": tx, "buyers": buyers,
                              "chains": s.get("chains") or [],
                              "recipients": s.get("recipients") or []}

    stamp = time.strftime("%Y-%m-%d")
    payload = {"generated": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "date": stamp,
               "source": f"x402scan public.sellers.bazaar.list, timeframe={TIMEFRAME}d",
               "pages_walked": pages, "seller_records": len(sellers),
               "origins": sorted(by_host.values(), key=lambda r: -r["usd"])}

    out = os.path.join(DATA, "x402scan_sellers.json")
    prev = 0
    if os.path.exists(out):
        try:
            prev = len(json.load(open(out)).get("origins", []))
        except Exception:
            prev = 0
    if prev and len(by_host) < prev * 0.5:
        raise SystemExit(f"refusing to overwrite: got {len(by_host)} origins vs {prev} previously "
                         f"(looks like a truncated walk, not real churn)")

    json.dump(payload, open(out, "w"), indent=1)
    earning = sum(1 for v in by_host.values() if v["usd"] > 0)
    print(f"x402scan sellers: {len(sellers)} records over {pages} pages -> "
          f"{len(by_host)} origins ({earning} with revenue), "
          f"${sum(v['usd'] for v in by_host.values()):,.0f} total 30d")


if __name__ == "__main__":
    main()
