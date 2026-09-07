#!/usr/bin/env python3
"""Circle's Agent Marketplace as a second, independent registry.

Coinbase and Circle both publish a public catalog of x402 services, and they do
not agree. Coinbase indexes broadly; Circle curates. Reading both is the only way
to see how much of this market any single vantage point misses.

Circle also publishes a provider name and a category per resource, which Coinbase
does not, so this doubles as a taxonomy for services we already track.

  python3 circle.py            # pull, save, and compare against the Coinbase snapshot
  python3 circle.py --no-save
"""
import argparse, json, os, time, urllib.parse, urllib.request
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
API = "https://api.circle.com/v2/x402/discovery/resources"
UA = {"User-Agent": "whatagentsbuy-probe/0.1 (+https://whatagentsbuy.com)"}
PAGE = 200
TRIES = 3


def fetch():
    items, offset, total = [], 0, None
    while True:
        url = f"{API}?limit={PAGE}&offset={offset}"
        for attempt in range(TRIES):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                    d = json.load(r)
                break
            except Exception:
                if attempt == TRIES - 1:
                    raise
                time.sleep(1.5 * (attempt + 1))
        items += d.get("items", [])
        total = (d.get("pagination") or {}).get("total", total)
        offset += PAGE
        if total is not None and offset >= total:
            break
        if not d.get("items"):
            break
    return items, total


def host_of(u):
    return urllib.parse.urlparse(u).netloc if isinstance(u, str) and u.startswith("http") else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true")
    a = ap.parse_args()

    items, total = fetch()
    print(f"Circle: {len(items):,} resources fetched (registry reports {total:,})")

    stamp = time.strftime("%Y-%m-%d")
    if not a.no_save:
        json.dump(items, open(os.path.join(DATA, "circle_resources.json"), "w"))

    c_urls = {it["resource"] for it in items if it.get("resource")}
    c_hosts = {host_of(u) for u in c_urls} - {""}

    # category and provider are Circle-only metadata worth keeping
    cats = Counter()
    by_host_cat = {}
    for it in items:
        m = (it.get("metadata") or {}).get("provider") or {}
        cat = m.get("category") or "UNCATEGORISED"
        cats[cat] += 1
        h = host_of(it.get("resource", ""))
        if h and h not in by_host_cat:
            by_host_cat[h] = {"provider": m.get("name") or "", "category": cat}
    if not a.no_save:
        json.dump(by_host_cat, open(os.path.join(DATA, "circle_categories.json"), "w"), indent=1)

    print("\ncategories:")
    for c, n in cats.most_common():
        print(f"  {n:>5}  {c}")

    cb_path = os.path.join(DATA, "cdp_resources_raw.json")
    if not os.path.exists(cb_path):
        print("\nno Coinbase snapshot to compare against")
        return
    cb = json.load(open(cb_path))
    b_urls = {r["resource"] for r in cb if isinstance(r.get("resource"), str)}
    b_hosts = {host_of(u) for u in b_urls} - {""}

    both = c_urls & b_urls
    only_c = c_urls - b_urls
    only_b = b_urls - c_urls
    print(f"\nCoinbase: {len(b_urls):,} resources across {len(b_hosts):,} hosts")
    print(f"Circle:   {len(c_urls):,} resources across {len(c_hosts):,} hosts")
    print(f"\n  in both registries : {len(both):,}")
    print(f"  Circle only        : {len(only_c):,}  ({len({host_of(u) for u in only_c}):,} hosts)")
    print(f"  Coinbase only      : {len(only_b):,}  ({len({host_of(u) for u in only_b}):,} hosts)")
    overlap = 100 * len(both) / max(1, len(c_urls))
    print(f"\n  {overlap:.0f}% of Circle's catalog also appears in Coinbase's")

    if only_c:
        print("\nservices Circle lists that Coinbase does not:")
        byh = defaultdict(int)
        for u in only_c:
            byh[host_of(u)] += 1
        for h, n in sorted(byh.items(), key=lambda kv: -kv[1])[:12]:
            meta = by_host_cat.get(h, {})
            print(f"  {n:>4}  {h[:40]:42s} {meta.get('provider','')[:22]:24s} {meta.get('category','')}")


if __name__ == "__main__":
    main()
