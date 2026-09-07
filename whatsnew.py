#!/usr/bin/env python3
"""What appeared in the agent economy since last time.

Pulls the public registry, diffs it against the last snapshot, and reports what
is genuinely new: services that did not exist before, endpoints added to
services we already knew, and listings that disappeared.

Keeps a small dated index of resource URLs (not the full 40MB catalog) so every
future run has something honest to diff against. Run it daily and the history
builds itself.

  python3 whatsnew.py            # diff against the most recent index, then save today
  python3 whatsnew.py --since 2026-08-04
  python3 whatsnew.py --no-save  # look without recording
"""
import argparse, gzip, hashlib, json, os, re, time, urllib.parse, urllib.request
from collections import defaultdict


def update_schema_store(items, data_dir):
    """Keep every distinct schema the market has ever published, once.

    The daily index records which fingerprint each URL had on each day. This
    stores what those fingerprints mean. Together they answer "what did this
    endpoint promise on 12 August", which a fingerprint alone cannot, and which
    is the question any conformance claim depends on.

    Content addressed, so the 15,168 endpoints publishing a schema collapse to
    8,712 distinct ones, and re-running only ever appends. The alternative was
    archiving the whole 43MB registry daily to keep schemas we mostly already
    have.
    """
    store_dir = os.path.join(data_dir, "schemas")
    os.makedirs(store_dir, exist_ok=True)
    path = os.path.join(store_dir, "store.json.gz")
    store = {}
    if os.path.exists(path):
        try:
            with gzip.open(path, "rt") as fh:
                store = json.load(fh)
        except Exception:
            store = {}
    before = len(store)
    for r in items:
        bz = ((r.get("extensions") or {}).get("bazaar") or {}).get("schema")
        if not bz:
            continue
        fp = hashlib.sha256(
            json.dumps(bz, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:12]
        if fp not in store:
            store[fp] = bz
    with gzip.open(path, "wt") as fh:
        json.dump(store, fh, separators=(",", ":"))
    return len(store) - before, len(store)


def read_index(path):
    """Snapshots are gzipped; plain .json is still read so older ones keep working."""
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt") as fh:
        return json.load(fh)


def write_index(path, obj):
    with gzip.open(path, "wt") as fh:
        json.dump(obj, fh, separators=(",", ":"))

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
IDX = os.path.join(DATA, "index")
API = "https://api.cdp.coinbase.com/platform/v2/x402/discovery/resources"
UA = {"User-Agent": "whatagentsbuy-probe/0.1 (+https://whatagentsbuy.com)"}
PAGE = 100
TRIES = 3


def fetch_registry():
    items, offset = [], 0
    while offset <= 60000:
        for attempt in range(TRIES):
            try:
                url = f"{API}?limit={PAGE}&offset={offset}"
                with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                    got = json.load(r).get("items", [])
                break
            except Exception:
                if attempt == TRIES - 1:
                    raise
                time.sleep(1.5 * (attempt + 1))
        items += got
        if len(got) < PAGE:
            break
        offset += PAGE
    return items


def index_of(items):
    """A compact record: url -> the few fields worth diffing on.

    The registry is a current-state view. It does not remember, and it churns
    hard: 2,796 of 14,668 endpoints listed on 2026-08-04 were gone seven days
    later and 111 hosts vanished entirely. Whatever is not written down here is
    unrecoverable, so this keeps the fields whose *change over time* is a story:

      payto   420 hosts advertise more than one address and 260 wallets are
              shared across hosts. Rotation and consolidation are only visible
              against yesterday.
      prices  every option, not just the first, so a price rise is detectable.
      schema  a fingerprint rather than the schema itself, so a seller quietly
              changing its output contract shows up without storing kilobytes.
    """
    out = {}
    for r in items:
        u = r.get("resource")
        if not isinstance(u, str) or not u.startswith("http"):
            continue
        acc = r.get("accepts") or [{}]
        a = acc[0]
        q = r.get("quality") or {}
        bz = ((r.get("extensions") or {}).get("bazaar") or {}).get("schema")
        out[u] = {
            "host": urllib.parse.urlparse(u).netloc,
            "service": r.get("serviceName") or "",
            "desc": (r.get("description") or "")[:200],
            "amount": a.get("amount"),
            "asset": a.get("asset"),
            "network": a.get("network"),
            "calls30d": q.get("l30DaysTotalCalls") or 0,
            "payers30d": q.get("l30DaysUniquePayers") or 0,
            "last_called": q.get("lastCalledAt"),
            "payto": sorted({(x.get("payTo") or "").lower() for x in acc if x.get("payTo")}),
            "options": [{"amount": x.get("amount"), "asset": x.get("asset"),
                         "network": x.get("network")} for x in acc][:6],
            "schema_fp": (hashlib.sha256(
                json.dumps(bz, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:12] if bz else None),
        }
    return out


def latest_index(before=None):
    if not os.path.isdir(IDX):
        return None, None
    files = sorted(f for f in os.listdir(IDX)
                   if re.match(r"index_\d{4}-\d{2}-\d{2}\.json(\.gz)?$", f))
    if before:
        files = [f for f in files if f[6:16] <= before]
    if not files:
        return None, None
    newest = files[-1]
    return read_index(os.path.join(IDX, newest)), newest[6:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="compare against the index from this date")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()

    prev, prev_date = latest_index(a.since)
    print("fetching the registry...")
    items = fetch_registry()
    cur = index_of(items)
    print(f"registry now: {len(cur):,} resources across "
          f"{len({v['host'] for v in cur.values()}):,} hosts")

    os.makedirs(IDX, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    if not a.no_save:
        write_index(os.path.join(IDX, f"index_{stamp}.json.gz"), cur)
        added, total = update_schema_store(items, os.path.dirname(IDX))
        print(f"schema store: +{added} new, {total:,} distinct schemas kept")

    # Record when each URL was first observed, and keep a rolling file of recent
    # arrivals with enough detail for the site to render them.
    FIRST = os.path.join(DATA, "first_seen.json")
    first = json.load(open(FIRST)) if os.path.exists(FIRST) else {}
    if not first and prev:
        # Never stamp an entire pre-existing catalog as "new today".
        for u in prev:
            first[u] = prev_date
    fresh = [u for u in cur if u not in first]
    for u in cur:
        first.setdefault(u, stamp)
    if not a.no_save:
        json.dump(first, open(FIRST, "w"))

        known_hosts = {v["host"] for u, v in cur.items() if first.get(u, stamp) < stamp}
        arrivals = defaultdict(lambda: {"endpoints": [], "new_service": False})
        for u in fresh:
            c = cur[u]
            key = (first[u], c["host"])
            g = arrivals[key]
            g["endpoints"].append({
                "url": u,
                "path": urllib.parse.urlparse(u).path,
                "desc": c["desc"],
                "amount": c["amount"],
                "asset": c["asset"],
                "network": c["network"],
                "calls30d": c["calls30d"],
            })
            g["service"] = c["service"] or c["host"]
            g["desc"] = g.get("desc") or (c["service"] and c["desc"] or "")
            g["amount"] = c["amount"]
            g["asset"] = c["asset"]
            g["network"] = c["network"]
            g["calls30d"] = max(g.get("calls30d", 0), c["calls30d"])
            g["new_service"] = c["host"] not in known_hosts
        rows = [{"date": d, "host": h, **v} for (d, h), v in arrivals.items()]
        rows.sort(key=lambda r: (r["date"], len(r["endpoints"])), reverse=True)
        existing = []
        NEWEST = os.path.join(DATA, "newest.json")
        if os.path.exists(NEWEST):
            existing = [r for r in json.load(open(NEWEST)) if r["date"] != stamp]
        json.dump((rows + existing)[:400], open(NEWEST, "w"), indent=1)
        print(f"\nrecorded {len(fresh):,} first-seen URLs across {len(rows)} services -> data/newest.json")

    if prev is None:
        # First run: fall back to the registry's own lastUpdated so today is not blank.
        today = [r for r in items if (r.get("lastUpdated") or "").startswith(stamp)]
        print(f"\nNo earlier index to diff against, so this run establishes the baseline.")
        print(f"Registry says {len(today):,} listings were touched today ({stamp}).")
        by_host = defaultdict(list)
        for r in today:
            by_host[urllib.parse.urlparse(r.get("resource", "")).netloc].append(r)
        for host, rows in sorted(by_host.items(), key=lambda kv: -len(kv[1]))[:a.top]:
            svc = next((x.get("serviceName") for x in rows if x.get("serviceName")), host)
            print(f"  {len(rows):>4} endpoints  {svc[:28]:30s} {host[:44]}")
        print("\nRun this again tomorrow for a true new-versus-gone diff.")
        return

    new_urls = [u for u in cur if u not in prev]
    gone_urls = [u for u in prev if u not in cur]
    new_hosts = {cur[u]["host"] for u in new_urls} - {v["host"] for v in prev.values()}
    reprice = [u for u in cur if u in prev and cur[u]["amount"] != prev[u]["amount"]
               and cur[u]["amount"] and prev[u]["amount"]]

    print(f"\nsince {prev_date}:")
    print(f"  {len(new_urls):>5} new endpoints")
    print(f"  {len(new_hosts):>5} of them on services that did not exist before")
    print(f"  {len(gone_urls):>5} endpoints gone")
    print(f"  {len(reprice):>5} price changes")

    if new_hosts:
        print("\nNEW SERVICES")
        by_host = defaultdict(list)
        for u in new_urls:
            if cur[u]["host"] in new_hosts:
                by_host[cur[u]["host"]].append(u)
        for host, urls in sorted(by_host.items(), key=lambda kv: -len(kv[1]))[:a.top]:
            first = cur[urls[0]]
            print(f"  {host}  ({len(urls)} endpoint{'s' if len(urls) != 1 else ''})")
            if first["service"]:
                print(f"      {first['service']}")
            if first["desc"]:
                print(f"      {first['desc'][:110]}")

    added_existing = [u for u in new_urls if cur[u]["host"] not in new_hosts]
    if added_existing:
        print(f"\nNEW ENDPOINTS ON KNOWN SERVICES ({len(added_existing)})")
        by_host = defaultdict(int)
        for u in added_existing:
            by_host[cur[u]["host"]] += 1
        for host, n in sorted(by_host.items(), key=lambda kv: -kv[1])[:a.top]:
            print(f"  {n:>4}  {host}")

    if reprice:
        print(f"\nPRICE CHANGES ({len(reprice)})")
        for u in reprice[:a.top]:
            print(f"  {prev[u]['amount']} -> {cur[u]['amount']}  ({cur[u]['asset'] or '?'})  {u[:70]}")
        print("  note: amounts are raw, in each asset's own decimals")

    if gone_urls:
        print(f"\nGONE ({len(gone_urls)})")
        by_host = defaultdict(int)
        for u in gone_urls:
            by_host[prev[u]["host"]] += 1
        for host, n in sorted(by_host.items(), key=lambda kv: -kv[1])[:a.top]:
            print(f"  {n:>4}  {host}")


if __name__ == "__main__":
    main()
