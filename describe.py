#!/usr/bin/env python3
"""What each service actually sells, in one line.

A leaderboard of hostnames tells you nothing. This builds a description for every
service we rank, preferring, in order:

  1. the seller's own `instructions` from /.well-known/x402  (usually the clearest)
  2. the description in the public registry
  3. nothing, said plainly, rather than a guess

Cached in data/services.json so repeat runs are cheap and offline-safe.
"""
import json, os, re, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
CACHE = os.path.join(DATA, "services.json")
UA = {"User-Agent": "whatagentsbuy-probe/0.1 (+https://whatagentsbuy.com)"}


def clean(text, limit=180):
    t = re.sub(r"\s+", " ", (text or "")).strip()
    t = t.split(" Visit https://")[0].strip()          # drop trailing doc pointers
    t = re.split(r"\s*\|\s*Step 1", t)[0].strip()      # drop usage walkthroughs
    if len(t) > limit:
        cut = t[:limit].rsplit(" ", 1)[0]
        t = cut + "..."
    return t


def self_described(host):
    """Ask the service what it is. Never raises: a dead host is just no answer."""
    try:
        req = urllib.request.Request(f"https://{host}/.well-known/x402", headers=UA)
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.load(r)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    for key in ("instructions", "description", "summary"):
        v = d.get(key)
        if isinstance(v, list) and v:
            return clean(v[0])
        if isinstance(v, str) and v.strip():
            return clean(v)
    return None


def registry_descriptions():
    p = os.path.join(DATA, "cdp_resources_raw.json")
    if not os.path.exists(p):
        return {}
    out = {}
    for r in json.load(open(p)):
        u = r.get("resource")
        if not isinstance(u, str) or not u.startswith("http"):
            continue
        host = u.split("/")[2]
        d = (r.get("description") or "").strip()
        if d and host not in out:
            out[host] = clean(d)
    return out


def main():
    lb = json.load(open(os.path.join(DATA, "leaderboard.json")))
    hosts = []
    for w in lb["windows"].values():
        for row in w["rows"]:
            if row.get("host") and row["host"] not in hosts:
                hosts.append(row["host"])

    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    reg = registry_descriptions()
    todo = [h for h in hosts if h not in cache]
    print(f"{len(hosts)} services on the board, {len(todo)} to describe")

    if todo:
        def safe(h):
            try:
                return self_described(h)
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=8) as ex:
            for host, said in zip(todo, ex.map(safe, todo)):
                desc = said or reg.get(host)
                cache[host] = {"desc": desc, "source": "self" if said else ("registry" if desc else None)}
        json.dump(cache, open(CACHE, "w"), indent=1)

    have = sum(1 for h in hosts if cache.get(h, {}).get("desc"))
    print(f"described {have}/{len(hosts)}")
    for h in hosts[:12]:
        c = cache.get(h, {})
        print(f"  {h[:30]:32s} [{c.get('source') or 'none':8s}] {(c.get('desc') or '')[:90]}")


if __name__ == "__main__":
    main()
