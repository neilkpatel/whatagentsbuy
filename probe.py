#!/usr/bin/env python3
"""Touchstone probe: ground-truth quality signals for x402 endpoints.

Everything here is free. We never pay. The 402 challenge itself carries the
quote, so we can compare what a seller advertises against what it actually
demands without spending a cent.

Signals collected per origin:
  reachable          origin answers at all
  correct_402        paid resources return 402 when unpaid (not 200/404/5xx)
  price_honest       live quote matches the advertised catalog price
  payto_stable       live payment address matches the catalog address
  phantom            does a nonsense path also return 402 (catalog is fiction)
  schema             live challenge carries an input schema agents can use
  discoverable       origin serves /openapi.json or /.well-known/x402
  latency            median challenge response time
  breadth            how many resources the origin lists
  operator           payTo address, for cross-origin concentration analysis
"""
import json, os, random, re, string, sys, time, urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
UA = {"User-Agent": "touchstone-probe/0.1 (+https://touchstone.neilkpatel.com) quality-review-bot"}
# The zero/dead address is never a real payTo. A 402 that quotes it is a broken or
# placeholder challenge; recording it once mapped every USDC burn on chain to the
# seller that returned it. Never store it as a payment address.
BURN_ADDRS = {"0x0000000000000000000000000000000000000000",
              "0x000000000000000000000000000000000000dead"}
MAX_PER_ORIGIN = 4
TIMEOUT = 12
WORKERS = 40


# --- asset decimals -------------------------------------------------------
# A quote amount is an integer in the asset's own decimals. USDC is 6; most
# BNB-chain and many other tokens are 18. Assuming 6 everywhere turned a
# one-cent charge into a ten-billion-dollar headline, so the decimals are
# looked up per asset and cached.
DECIMALS_CACHE = os.path.join(DATA, "decimals.json")
_DEC = {}
if os.path.exists(DECIMALS_CACHE):
    try:
        _DEC = json.load(open(DECIMALS_CACHE))
    except Exception:
        _DEC = {}

EVM_RPC = {
    "eip155:8453": "https://base.drpc.org",
    "eip155:1": "https://eth.drpc.org",
    "eip155:56": "https://bsc-dataseed.binance.org",
    "eip155:137": "https://polygon.drpc.org",
    "eip155:42161": "https://arbitrum.drpc.org",
}
# Well-known 6-decimal stablecoins, so the common case never needs a network call.
for _a in ("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
           "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
           "0xaf88d065e77c8cc2239327c5edb3a432268e5831",
           "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359",
           "0xdac17f958d2ee523a2206206994597c13d831ec7"):
    _DEC.setdefault(_a, 6)


def asset_decimals(asset, network):
    """Decimals for an asset, cached. None when we cannot establish it."""
    if not asset:
        return None
    key = str(asset).lower()
    if key in _DEC:
        return _DEC[key]
    # Well-known non-EVM mints. Without these, a seller quoting only Solana is
    # unpriceable and reads as broken rather than as a seller on another chain.
    NON_EVM = {
        "epjfwdd5aufqssqem2qn1xzybapc8g4weggkzwytdt1v": 6,   # USDC, Solana
        "es9vmfrzacermjfrf4h2fyd4kconky11mcce8benwnyb": 6,   # USDT, Solana
    }
    if key in NON_EVM:
        return NON_EVM[key]
    rpc = EVM_RPC.get(str(network))
    if not rpc or not key.startswith("0x"):
        return None  # other non-EVM assets: still unresolved, and reported as such
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                           "params": [{"to": key, "data": "0x313ce567"}, "latest"]})
        req = urllib.request.Request(rpc, data=body.encode(),
                                     headers={**UA, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.load(r).get("result")
        if res and res != "0x":
            _DEC[key] = int(res, 16)
            json.dump(_DEC, open(DECIMALS_CACHE, "w"))
            return _DEC[key]
    except Exception:
        pass
    return None


def to_usd(amount, asset, network):
    """Convert a raw quote amount to dollars, or None if decimals are unknown."""
    d = asset_decimals(asset, network)
    if d is None:
        return None
    try:
        return int(amount) / (10 ** d)
    except Exception:
        return None


def http(url, method="GET", body=None, timeout=TIMEOUT):
    """Return (status, headers, text, elapsed_ms). Never raises."""
    t0 = time.time()
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, headers=dict(UA), method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(200_000).decode("utf-8", "replace"), int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as e:
        try:
            txt = e.read(200_000).decode("utf-8", "replace")
        except Exception:
            txt = ""
        return e.code, dict(e.headers or {}), txt, int((time.time() - t0) * 1000)
    except Exception as e:
        return None, {}, f"{type(e).__name__}: {e}", int((time.time() - t0) * 1000)


def parse_quote(status, headers, text):
    """Pull an amount (USD float), payTo and network out of a 402 response."""
    out = {"amount": None, "payTo": None, "network": None, "schema": False, "raw": False}
    blob = None
    try:
        blob = json.loads(text)
    except Exception:
        pass
    # x402 standard body
    if isinstance(blob, dict):
        out["raw"] = True
        accepts = blob.get("accepts")
        if isinstance(accepts, list) and accepts:
            # Take the first option we can actually price, not accepts[0].
            # Sellers increasingly list several chains and the order is theirs to
            # choose; three of the busiest sixty list Solana first. Reading index
            # zero blindly priced them at None and reported a working endpoint as
            # unpayable, which cost api.syraa.fun a D for a $0.08 call that is
            # payable on Base.
            best = None
            for a in accepts:
                amt = a.get("amount") or a.get("maxAmountRequired")
                if amt is None:
                    continue
                usd = to_usd(amt, a.get("asset"), a.get("network"))
                if usd is not None:
                    best = (a, usd)
                    break
            a, usd = best if best else (accepts[0], None)
            out["amount"] = usd
            out["payTo"] = a.get("payTo") or a.get("recipient")
            out["network"] = a.get("network")
            out["options"] = len(accepts)
        ext = blob.get("extensions") or {}
        if isinstance(ext, dict) and ext.get("bazaar"):
            out["schema"] = True
        # non-standard shapes (e.g. blockrun's {"price": {"amount": "0.0085"}})
        if out["amount"] is None:
            p = blob.get("price")
            if isinstance(p, dict) and p.get("amount") is not None:
                try:
                    out["amount"] = float(p["amount"])
                except Exception:
                    pass
            if isinstance(blob.get("paymentInfo"), dict):
                out["network"] = out["network"] or blob["paymentInfo"].get("network")
    # Header-carried challenges. Case-insensitive: header casing varies by server.
    import base64
    h = {k.lower(): v for k, v in (headers or {}).items()}

    def take(dec):
        """Absorb a decoded x402 challenge document."""
        a = (dec.get("accepts") or [{}])[0]
        amt = a.get("amount") or a.get("maxAmountRequired")
        if out["amount"] is None and amt is not None:
            out["amount"] = to_usd(amt, a.get("asset"), a.get("network"))
        out["payTo"] = out["payTo"] or a.get("payTo") or a.get("recipient")
        out["network"] = out["network"] or a.get("network")
        if (dec.get("extensions") or {}).get("bazaar"):
            out["schema"] = True

    # 1. payment-required: <base64 json>  (widely used, was previously missed)
    pr = h.get("payment-required") or h.get("x-payment-required") or ""
    if pr:
        try:
            take(json.loads(base64.b64decode(pr + "=" * (-len(pr) % 4))))
        except Exception:
            pass

    # 2. WWW-Authenticate: X402 requirements="<base64 json>"
    wa = h.get("www-authenticate") or ""
    if "requirements=" in wa:
        m = re.search(r'requirements="([^"]+)"', wa)
        if m:
            try:
                take(json.loads(base64.b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4))))
            except Exception:
                pass

    # 3. WWW-Authenticate: Payment ... request="<base64 json>"  (MPP / Tempo style)
    if out["amount"] is None and wa.strip().lower().startswith("payment"):
        m = re.search(r'request="([^"]+)"', wa)
        if m:
            try:
                dec = json.loads(base64.b64decode(m.group(1) + "=" * (-len(m.group(1)) % 4)))
                amt = dec.get("amount")
                if amt is not None:
                    try:
                        out["amount"] = int(amt) / 1e6
                    except Exception:
                        pass
                out["payTo"] = out["payTo"] or dec.get("recipient")
                out["network"] = out["network"] or "tempo"
            except Exception:
                pass
    return out


def declared_call(item):
    """How the registry says this resource should be called: (method, body)."""
    info = (((item.get("extensions") or {}).get("bazaar") or {}).get("info") or {})
    inp = info.get("input") or {}
    method = str(inp.get("method") or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        method = "GET"
    body = inp.get("body")
    if body is not None and not isinstance(body, str):
        body = json.dumps(body)
    return method, body


def usage(item):
    q = item.get("quality") or {}
    return {
        "calls30d": q.get("l30DaysTotalCalls") or 0,
        "payers30d": q.get("l30DaysUniquePayers") or 0,
        "last_called": q.get("lastCalledAt"),
    }


def catalog_price(item):
    for a in item.get("accepts") or []:
        amt = a.get("amount") or a.get("maxAmountRequired")
        if amt is not None:
            usd = to_usd(amt, a.get("asset"), a.get("network"))
            if usd is not None:
                return usd, a.get("payTo") or a.get("recipient"), a.get("network")
    return None, None, None


def derive_from_wellknown(origin):
    """Build probeable resource stubs for an origin the CDP registry doesn't list.

    Sellers that never registered with Coinbase discovery still often publish
    /.well-known/x402. BlockRun's shape is {"version":1,"resources":["POST /api/v1/
    chat/completions", ...]}; others use objects. Handle both, defensively — a
    seller we can't parse is left with no sample rather than a false verdict.
    """
    st, _, txt, _ = http(f"https://{origin}/.well-known/x402", timeout=10)
    if st != 200 or not txt.strip().startswith("{"):
        return []
    try:
        doc = json.loads(txt)
    except Exception:
        return []
    out = []
    for r in (doc.get("resources") or [])[:MAX_PER_ORIGIN * 3]:
        method, path = "GET", None
        if isinstance(r, str):
            parts = r.split(None, 1)
            if len(parts) == 2 and parts[0].isalpha() and parts[0].isupper():
                method, path = parts[0], parts[1]
            else:
                path = r
        elif isinstance(r, dict):
            path = r.get("resource") or r.get("url") or r.get("path")
            method = str(r.get("method") or "GET").upper()
        if not isinstance(path, str) or not path:
            continue
        if "{" in path or "}" in path or ":" in path.split("/")[-1]:
            continue  # templated path — we'd be probing a URL that cannot exist
        url = path if path.startswith("http") else f"https://{origin}{path if path.startswith('/') else '/' + path}"
        out.append({
            "resource": url,
            "extensions": {"bazaar": {"info": {"input": {"method": method}}}},
            "_derived": True,
        })
    return out


def probe_origin(origin, items, seed_demand=None):
    """Probe one origin: discovery docs, sample resources, phantom-path test.

    `items` may be empty for origins that settle on chain but never registered
    with CDP discovery. Those still get probed — we derive what to call from
    /.well-known/x402 — and their demand comes from `seed_demand` (x402scan
    settlement) rather than from registry counters that do not exist.
    """
    derived = False
    if not items:
        items = derive_from_wellknown(origin)
        derived = True
    names = [i.get("serviceName") for i in items if i.get("serviceName")]
    tags = []
    for i in items[:12]:
        for t in (i.get("tags") or []):
            if t not in tags:
                tags.append(t)
    u = [usage(i) for i in items]
    last = sorted([x["last_called"] for x in u if x["last_called"]], reverse=True)
    sd = seed_demand or {}
    res = {
        "origin": origin, "breadth": len(items), "checked": [],
        "service": names[0] if names else origin,
        "blurb": ((items[0].get("description") if items else "") or "")[:220],
        "tags": tags[:6],
        "listed_in_cdp": not derived,
        "derived_resources": derived and bool(items),
        "settled_tx30d": sd.get("tx") or 0,
        "settled_usd30d": sd.get("usd") or 0.0,
        "settled_buyers30d": sd.get("buyers") or 0,
        "calls30d": sum(x["calls30d"] for x in u),
        "payers30d": max((x["payers30d"] for x in u), default=0),
        "last_called": last[0] if last else None,
        "discoverable": False, "discovery_paths": [], "free_tier": False,
        "phantom": None, "latencies": [], "payto": defaultdict(int), "rotates_payto": None,
        "descriptions": [], "errors": [],
    }

    for path in ("/openapi.json", "/.well-known/x402"):
        st, _, txt, _ = http(f"https://{origin}{path}", timeout=8)
        if st == 200 and txt.strip().startswith("{"):
            res["discoverable"] = True
            res["discovery_paths"].append(path)

    # phantom test: a path that cannot exist. A 402 here means the seller
    # demands payment for resources it does not have.
    junk = "".join(random.choice(string.ascii_lowercase) for _ in range(18))
    st, _, _, _ = http(f"https://{origin}/__touchstone_{junk}", timeout=8)
    res["phantom"] = (st == 402)
    res["phantom_status"] = st

    # Rank by usage so the sample covers what agents actually buy.
    ordered = sorted(items, key=lambda i: -(usage(i)["calls30d"]))
    sample = ordered[:MAX_PER_ORIGIN]
    for it in sample:
        url = it.get("resource")
        if not isinstance(url, str) or not url.startswith("http"):
            continue
        adv_amt, adv_payto, adv_net = catalog_price(it)
        method, ex_body = declared_call(it)
        body = ex_body if ex_body is not None else ("{}" if method != "GET" else None)
        st, hd, txt, ms = http(url, method=method, body=body)
        # 405/400/401 can all mean we guessed the call shape wrong rather than
        # that the seller is broken — api.clusterprotocol.ai answers 401 to a GET
        # on an endpoint that wants POST. Retry the other verb before judging.
        if st in (400, 401, 405):
            alt = "POST" if method == "GET" else "GET"
            st2, hd2, txt2, ms2 = http(url, method=alt, body="{}" if alt == "POST" else None)
            if st2 in (402, 200):
                method, st, hd, txt, ms = alt, st2, hd2, txt2, ms2
        q = parse_quote(st, hd, txt)
        # Some sellers mint a fresh receiving address per request. That is a
        # routing pattern, not a defect, so confirm before treating a difference
        # from the registry as staleness.
        rotates = None
        if st == 402 and q["payTo"] and not res["checked"]:
            _, hd2, txt2, _ = http(url, method=method, body=body)
            q2 = parse_quote(st, hd2, txt2)
            if q2["payTo"]:
                rotates = q2["payTo"].lower() != q["payTo"].lower()
                res["rotates_payto"] = rotates
        desc = ((it.get("metadata") or {}).get("description")
                or (it.get("resource_metadata") or {}).get("description") or "")
        if desc:
            res["descriptions"].append(desc[:160])
        # payable = a standards-compliant agent can act on this challenge alone:
        # it must carry both an amount and a destination address.
        payable = bool(st == 402 and q["amount"] is not None and q["payTo"])
        # For a DERIVED resource we invented the call shape: /.well-known/x402
        # gives a path but no request schema, so an empty body can trip the
        # seller's own validation before it ever reaches the paywall
        # (sol.blockrun.ai answers 400 to `{}` and 402 to a well-formed body).
        # That is our ignorance, not their defect — mark it inconclusive so it
        # can never produce a failing grade.
        inconclusive = bool(it.get("_derived") and st not in (200, 402))
        row = {
            "url": url, "method": method, "status": st, "ms": ms,
            "adv_amount": adv_amt, "live_amount": q["amount"],
            "adv_payto": adv_payto, "live_payto": q["payTo"],
            "network": q["network"] or adv_net, "schema": q["schema"],
            "structured": q["raw"], "payable": payable,
            "derived": bool(it.get("_derived")), "inconclusive": inconclusive,
            "body_sample": (txt or "")[:200] if st == 402 and not payable else None,
        }
        if st == 402:
            res["latencies"].append(ms)
            if q["payTo"] and q["payTo"].lower() not in BURN_ADDRS:
                res["payto"][q["payTo"]] += 1
        elif st == 200:
            res["free_tier"] = True
        elif st is None:
            res["errors"].append(txt[:80])
        res["checked"].append(row)
    res["payto"] = dict(res["payto"])
    return res


def score(o):
    """Weighted quality score. Every component is evidence we measured.

    Inconclusive checks are excluded: those are derived requests we could not
    form correctly, so they are evidence about us, not about the seller.
    """
    checked = [c for c in o["checked"] if not c.get("inconclusive")]
    paid = [c for c in checked if c["status"] == 402]
    reached = [c for c in checked if c["status"] is not None]
    s, notes, flags = 0.0, [], []

    # 1. Reachable (15)
    if checked:
        rate = len(reached) / len(checked)
        s += 15 * rate
        if rate < 1:
            flags.append(f"{len(checked)-len(reached)}/{len(checked)} listed resources unreachable")
    else:
        flags.append("no probeable resources")

    # 2. Correct 402 handshake (15)
    if checked:
        good = len(paid) + len([c for c in checked if c["status"] == 200])
        s += 15 * (good / len(checked))
        odd = [c for c in checked if c["status"] not in (402, 200, None)]
        if odd:
            codes = sorted({c["status"] for c in odd})
            flags.append(f"{len(odd)} listed resource(s) answer {codes[0]} instead of asking for payment")

    # 3. Payable challenge (25) — can an agent act on the 402 alone?
    if paid:
        payable = [c for c in paid if c["payable"]]
        s += 25 * (len(payable) / len(paid))
        if not payable:
            samp = next((c["body_sample"] for c in paid if c["body_sample"]), "") or ""
            why = "empty body" if samp.strip() in ("{}", "") else "no payment requirements in the response"
            flags.append(f"402 carries no machine-payable quote ({why})")
        elif len(payable) < len(paid):
            flags.append(f"{len(paid)-len(payable)}/{len(paid)} challenges are not machine-payable")
        else:
            notes.append("challenges carry a complete, machine-payable quote")

    # 4. Price honesty (15) — live quote vs advertised
    comps = [c for c in paid if c["adv_amount"] is not None and c["live_amount"] is not None]
    if comps:
        oks = 0
        for c in comps:
            hi, lo = max(c["adv_amount"], c["live_amount"]), min(c["adv_amount"], c["live_amount"])
            if hi == 0 or (hi - lo) / hi <= 0.02:
                oks += 1
        s += 15 * (oks / len(comps))
        if oks < len(comps):
            worst = max(comps, key=lambda c: abs((c["live_amount"] or 0) - (c["adv_amount"] or 0)))
            flags.append(f"price mismatch: registry says ${worst['adv_amount']}, endpoint demands ${worst['live_amount']}")
        else:
            notes.append("live quotes match the public registry")

    # 5. Payment address (10) — matches the registry, or rotates by design
    pcomp = [c for c in paid if c["adv_payto"] and c["live_payto"]]
    if o.get("rotates_payto"):
        s += 10
        notes.append("issues a fresh receiving address per request")
    elif pcomp:
        match = sum(1 for c in pcomp if c["adv_payto"].lower() == c["live_payto"].lower())
        s += 10 * (match / len(pcomp))
        if match < len(pcomp):
            flags.append("pays to an address that is not the one on file in the registry")

    # 6. Machine-readable (10): discovery docs + input schema in challenge
    if o["discoverable"]:
        s += 6
        notes.append("publishes " + " and ".join(o["discovery_paths"]))
    if any(c["schema"] for c in paid):
        s += 4
        notes.append("challenge carries an input schema")

    # 7. Speed (10)
    lat = sorted(o["latencies"])
    med = lat[len(lat) // 2] if lat else None
    if med is not None:
        s += 10 if med < 400 else 7 if med < 1000 else 4 if med < 2500 else 1

    # Penalties
    if o["phantom"]:
        s -= 30
        flags.append("demands payment for paths that do not exist")
    if o["free_tier"]:
        s += 3
        notes.append("has free endpoints to try first")

    s = max(0.0, min(100.0, s))
    grade = "A" if s >= 85 else "B" if s >= 70 else "C" if s >= 55 else "D" if s >= 40 else "F"
    return round(s, 1), grade, notes, flags, med


def adoption(o, now=None):
    """Second axis: is anyone actually buying, and is that demand real?

    Technical compliance turns out to be table stakes (most sellers pass). What
    separates a service worth calling from a listing nobody wants is demand, and
    demand has to be checked for self-dealing: a high call count from a single
    paying wallet is the operator calling its own endpoint, not a market.
    """
    # CDP counters cover ~3% of the network and unevenly (95.5% of x402.twit.sh's
    # traffic, 0.05% of blockrun.ai's, 0% for sellers that never registered). When
    # the registry has nothing but the chain says otherwise, believe the chain.
    calls, payers = o["calls30d"], o["payers30d"]
    o["demand_source"] = "cdp-registry"
    if (o.get("settled_tx30d") or 0) > calls:
        calls, payers = o["settled_tx30d"], o.get("settled_buyers30d") or 0
        o["demand_source"] = "x402scan-settlement"
    days = None
    if o.get("last_called"):
        try:
            t = time.strptime(o["last_called"][:19], "%Y-%m-%dT%H:%M:%S")
            days = max(0, int((time.time() - time.mktime(t) + time.timezone) / 86400))
        except Exception:
            pass
    o["days_since_call"] = days
    per_payer = calls / payers if payers else calls

    if payers >= 20 and calls >= 200:
        tier, why = "established", f"{payers} distinct wallets paid it in the last 30 days"
    elif payers >= 5:
        tier, why = "traction", f"{payers} distinct paying wallets"
    elif calls >= 500 and payers <= 2:
        tier, why = "self-traffic", f"{calls:,} calls from only {payers} paying wallet(s)"
    elif calls > 0:
        tier, why = "thin", f"{calls:,} calls from {payers} wallet(s)"
    else:
        tier, why = "dormant", "no recorded paid calls"

    if days is not None and days > 14 and tier not in ("dormant",):
        tier, why = "dormant", f"last paid call {days} days ago"

    o["wash_ratio"] = round(per_payer, 1) if payers else None
    return tier, why


def load_universe():
    """The set of origins worth grading = CDP registry UNION x402scan sellers.

    Probing only the CDP registry grades a Coinbase directory, not the market:
    measured 2026-08-04, that misses 1,197 of 2,037 revenue-earning origins and
    about 35% of all seller dollars, including the #2 seller by revenue
    (api.clusterprotocol.ai) and claw402.ai, x402.dtelecom.org, api.vishwalab.com.
    """
    raw = json.load(open(os.path.join(DATA, "cdp_resources_raw.json")))
    by_origin = defaultdict(list)
    for it in raw:
        u = it.get("resource", "")
        if isinstance(u, str) and u.startswith("http"):
            by_origin[urllib.parse.urlparse(u).netloc].append(it)

    demand = {}
    p = os.path.join(DATA, "x402scan_sellers.json")
    if os.path.exists(p):
        for r in json.load(open(p)).get("origins", []):
            h = r.get("host")
            if h:
                demand[h] = r
                by_origin.setdefault(h, [])          # union: seed the missing half
    else:
        print("! data/x402scan_sellers.json missing — run fetch_sellers.py first; "
              "grading the CDP registry alone understates the market by ~35% of dollars")

    # Rank by settled dollars first (grade what money actually flows through),
    # then by how much the origin lists. Keeps `probe.py N` samples meaningful.
    origins = sorted(by_origin, key=lambda o: (-(demand.get(o, {}).get("usd") or 0.0),
                                               -len(by_origin[o])))
    return by_origin, demand, origins


def main():
    by_origin, demand, origins = load_universe()
    cdp_only = sum(1 for o in origins if by_origin[o] and o not in demand)
    both = sum(1 for o in origins if by_origin[o] and o in demand)
    scan_only = sum(1 for o in origins if not by_origin[o])

    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(origins)
    origins = origins[:limit]
    print(f"probing {len(origins)} origins ({sum(len(by_origin[o]) for o in origins)} listed resources)")
    print(f"  universe: {both} in both sources · {cdp_only} CDP-only · {scan_only} x402scan-only "
          f"(no CDP listing — resources derived from /.well-known/x402)")

    out, done = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(probe_origin, o, by_origin[o], demand.get(o)): o for o in origins}
        for f in futs:
            pass
        for f, o in list(futs.items()):
            try:
                r = f.result()
            except Exception as e:
                d = demand.get(o) or {}
                r = {"origin": o, "breadth": len(by_origin[o]), "checked": [], "discoverable": False,
                     "discovery_paths": [], "free_tier": False, "phantom": None, "latencies": [],
                     "payto": {}, "descriptions": [], "errors": [f"probe failed: {e}"],
                     "service": o, "blurb": "", "tags": [],
                     "listed_in_cdp": bool(by_origin[o]), "derived_resources": False,
                     "settled_tx30d": d.get("tx") or 0, "settled_usd30d": d.get("usd") or 0.0,
                     "settled_buyers30d": d.get("buyers") or 0,
                     "calls30d": 0, "payers30d": 0, "last_called": None}
            sc, gr, notes, flags, med = score(r)
            tier, why = adoption(r)
            # An origin we could not introspect is NOT a failing origin. If we
            # found no resource to call — no CDP listing and no parseable
            # /.well-known/x402 — we have measured nothing, and publishing an F
            # would be exactly the kind of false accusation this site exists to
            # avoid. Mark it unrated and say why.
            conclusive = [c for c in r["checked"] if not c.get("inconclusive")]
            assessable = bool(conclusive)
            if not assessable:
                gr, sc = "?", 0.0
                flags = []
                notes = (["not assessable: every derived request was rejected before the "
                          "paywall, so we never saw a payment challenge to grade — we lack "
                          "the request schema, which is not a defect in the seller"]
                         if r["checked"] else
                         ["not assessable: no listing in CDP discovery and no parseable "
                          "/.well-known/x402, so there was no declared resource to call"])
            r.update(score=sc, grade=gr, notes=notes, flags=flags, median_ms=med,
                     tier=tier, tier_why=why, assessable=assessable)
            out.append(r)
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(origins)}")

    TIER_RANK = {"established": 0, "traction": 1, "thin": 2, "self-traffic": 3, "dormant": 4}
    out.sort(key=lambda r: (TIER_RANK.get(r["tier"], 5),
                            -(r.get("settled_usd30d") or 0.0), -r["calls30d"], -r["score"]))
    stamp = time.strftime("%Y-%m-%d")
    payload = {"generated": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "date": stamp,
               "source": "Coinbase CDP x402 discovery API + x402scan seller settlement "
                         "+ live Touchstone probes",
               "universe": {"total": len(out),
                            "listed_in_cdp": sum(1 for r in out if r.get("listed_in_cdp")),
                            "x402scan_only": sum(1 for r in out if not r.get("listed_in_cdp")),
                            "resources_derived": sum(1 for r in out if r.get("derived_resources"))},
               "origins": out}
    json.dump(payload, open(os.path.join(DATA, f"probe_{stamp}.json"), "w"), indent=1)
    json.dump(payload, open(os.path.join(DATA, "latest.json"), "w"), indent=1)
    # The archive, gzipped and version controlled. A probe is a measurement of a
    # market that no longer exists an hour later: which origins answered, what
    # each quoted, where the money pointed. Re-running never recovers yesterday,
    # and until 2026-08-12 every run silently destroyed the one before it.
    import gzip
    arc_dir = os.path.join(DATA, "probe")
    os.makedirs(arc_dir, exist_ok=True)
    arc = os.path.join(arc_dir, f"probe_{stamp}.json.gz")
    with gzip.open(arc, "wt") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    print(f"\narchived {arc} ({os.path.getsize(arc)/1048576:.2f} MB)")
    print(f"wrote data/latest.json — {len(out)} origins")
    for r in out[:15]:
        print(f"  {r['grade']} {r['score']:5.1f}  {r['origin'][:40]:42s} {len(r['checked'])} checked")


if __name__ == "__main__":
    main()
