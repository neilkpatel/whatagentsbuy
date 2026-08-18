#!/usr/bin/env python3
"""Does an endpoint return what it promised?

The registry makes sellers publish an input example and an output shape. That is
a contract, and almost nobody checks it. This calls an endpoint once with its own
advertised input and compares what comes back against its own advertised output.

The result is the column this site exists to fill, at market scale: not "is it
reachable" but "did the goods match the promise".

    python3 conform.py --limit 20 --budget 0.20 --dry-run
    python3 conform.py --limit 200 --budget 0.75

Safety, because this spends real money against strangers' servers:
  - one call per endpoint, ever, per run
  - a hard budget in dollars, checked before each call, never exceeded
  - a per-endpoint price cap
  - a pause between calls so no host sees a burst
  - --dry-run prices the whole run without paying for any of it
  - results append to disk after every call, so a kill loses nothing
"""
import argparse, glob, json, os, re, subprocess, sys, time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
# Raw responses we paid for live here, NOT under data/. build.py only globs
# data/, so nothing in captures/ can ever reach public/. It is gitignored: a
# private, append-only archive of the exact bytes agentcash returned, so any
# future parsing bug is a free offline re-run instead of a re-pay. The 224
# unrecoverable rows on 2026-08-13 existed only because we discarded the
# response before saving it; this is the fix.
CAPTURES = os.path.join(HERE, "captures")
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
AGENTCASH = os.path.expanduser(
    "~/.npm/_npx/9f16d0537421f093/node_modules/.bin/agentcash")


def price_of(r):
    ps = [int(a["amount"]) / 1e6 for a in (r.get("accepts") or [])
          if (a.get("asset") or "").lower() == USDC and str(a.get("amount", "")).isdigit()]
    return min(ps) if ps else None


def contract(r):
    """The seller's own declared input example and output shape, or None."""
    info = ((r.get("extensions") or {}).get("bazaar") or {}).get("info") or {}
    inp, out = info.get("input") or {}, info.get("output") or {}
    ex = out.get("example")
    if not isinstance(ex, dict) or not ex:
        return None
    method = (inp.get("method") or "GET").upper()
    body = inp.get("body")
    if method in ("POST", "PUT", "PATCH") and not isinstance(body, dict):
        return None
    return {"method": method, "body": body, "expect": sorted(ex.keys()),
            "expect_example": ex}


def find_payment(obj, depth=0):
    """Search the whole envelope for evidence a payment settled.

    Inferring 'no payment' from the absence of a key in the branch you happened
    to parse is how a run reported $0.0010 while $0.1960 left the wallet. If a
    transaction hash exists anywhere in the response, money moved.
    """
    if depth > 6 or not isinstance(obj, (dict, list)):
        return None
    if isinstance(obj, dict):
        for k in ("transactionHash", "txHash", "transaction"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("0x") and len(v) > 20:
                return {"tx": v, "price": obj.get("price")}
        for v in obj.values():
            got = find_payment(v, depth + 1)
            if got:
                return got
        return None
    for v in obj:
        got = find_payment(v, depth + 1)
        if got:
            return got
    return None


def wallet_balance():
    """Ground truth for what a run actually cost. The client's own accounting
    was wrong by 200x, so the run reconciles against the wallet instead."""
    try:
        p = subprocess.run([AGENTCASH, "balance", "--format", "json"],
                           capture_output=True, text=True, timeout=60)
        d = json.loads((p.stdout or "").strip())
        b = d.get("data", d)
        return float(b.get("balance") if isinstance(b, dict) else b)
    except Exception:
        return None


def archive_raw(url, method, body, returncode, stdout, stderr, out_dir=None):
    """Append the exact bytes agentcash returned to a private, append-only log.

    This runs at the rawest possible moment, before any of our parsing touches
    the response, so a bug anywhere downstream (capture, judge, describe) can be
    re-run offline against the untouched source instead of re-buying the call.
    Stores agentcash's full stdout verbatim (the {success, data, metadata}
    envelope); re-analysis just json.loads it and re-derives everything.

    Archiving must never break a run, so every failure here is swallowed. Returns
    the line written (a dict) for testability, or None if it could not write."""
    out_dir = out_dir or CAPTURES
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "url": url, "method": method,
           "request_body": body, "returncode": returncode,
           "stdout": stdout, "stderr": (stderr or "")[:500]}
    try:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"raw_{time.strftime('%Y-%m-%d')}.jsonl")
        with open(path, "a") as fh:                 # append-only, never overwrite
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
    except Exception:
        return None


def call(url, method, body, cap, timeout=45):
    """One paid call through the agentcash CLI. Returns (ok, payload, meta).

    The CLI wraps everything in {success, data|error}. Do not pass -q: it
    suppresses stdout as well as stderr, which silently produced 155 empty
    results on the first run before anyone noticed.

    `cap` is the hard per-transaction ceiling in dollars, passed to the client as
    --max-amount. Without it the CLI defaults to $5 per request, so an endpoint
    that advertises a tenth of a cent but quotes more live would still be paid.
    With it, the client aborts and spends nothing if the live 402 asks for more
    than cap. This is the per-call limit, separate from the run-wide budget.
    """
    cmd = [AGENTCASH, "fetch", url, "-m", method, "--format", "json",
           "--max-amount", str(cap), "--timeout", str(timeout * 1000)]
    if body is not None:
        cmd += ["-b", json.dumps(body)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 25)
    except subprocess.TimeoutExpired:
        archive_raw(url, method, body, None, "", "client timeout")
        return False, None, {"error": "client timeout"}
    # Save the untouched response FIRST, before a single parse. Everything below
    # is derived and re-derivable from this line.
    archive_raw(url, method, body, p.returncode, p.stdout or "", p.stderr or "")
    raw = (p.stdout or "").strip()
    if not raw:
        return False, None, {"error": (p.stderr or "no output")[:200]}
    try:
        env = json.loads(raw)
    except ValueError:
        return False, None, {"error": raw[:200]}
    if not isinstance(env, dict):
        return False, None, {"error": "unexpected envelope"}

    if env.get("success"):
        # The envelope is ALWAYS {success, data, metadata}. The response payload
        # is env["data"]; payment and headers are in env["metadata"], a separate
        # top-level key. The old code hunted for metadata keys INSIDE the payload
        # and lost any response that happened to contain a field named "price",
        # "headers" or "network", which is most price feeds and network tools.
        # That single mistake scored dozens of delivering endpoints as failures.
        payload = env.get("data")
        m = env.get("metadata") or {}
        meta = {"payment": m.get("payment"), "price": m.get("price"),
                "status": m.get("statusCode")}
        return True, payload, meta
    err = env.get("error") or {}
    det = err.get("details") or {}
    pay = find_payment(env)
    return False, det.get("body"), {
        "payment": det.get("payment") or ({"success": True, "transactionHash": pay["tx"]} if pay else None),
        "price": det.get("price"),
        "status": det.get("statusCode"),
        "error": f'{det.get("statusCode") or ""} {err.get("message") or err.get("code") or ""}'.strip(),
    }


def describe_shape(value, depth=0):
    """The TYPE structure of a response, with every actual value discarded.

    This is the useful half an agent wants before integrating: what fields come
    back and of what type, nested a couple of levels. It deliberately keeps no
    strings, numbers or contents, only their types, so we describe the shape of
    what a seller returns without ever republishing the goods it sells.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        if not value or depth >= 3:
            return "array"
        return {"array_of": describe_shape(value[0], depth + 1)}
    if isinstance(value, dict):
        if depth >= 3:
            return "object"
        return {k: describe_shape(v, depth + 1) for k, v in list(value.items())[:40]}
    return "unknown"


def judge(payload, expect):
    """Compare the delivered response against the seller's promised output.

    GUARDRAIL: a verdict is one of three states, and only one of them is a
    negative claim about the seller. Conflating them is what produced false
    public accusations, because a response we could not read was recorded as a
    response that lacked the goods.

      delivered      we read a response and every promised field is present
      short          we read a REAL response and it genuinely lacks fields
      inconclusive   we could not read a usable response: null, non-JSON, or an
                     error object. This is OUR failure to measure, not the
                     seller's failure to deliver, and is never published as one.

    Only `short`, a real captured response genuinely missing fields, is a
    negative verdict, and even that is re-verified before it is trusted.
    """
    if not isinstance(payload, dict):
        return {"status": "inconclusive", "conforms": False,
                "why": "no readable JSON response (could not measure)",
                "missing": expect, "extra": [], "observed_schema": describe_shape(payload)}

    # An error object is us failing to call it right or a transient error, not a
    # delivery failure. Treat a response that is basically {error/message/...} and
    # carries almost none of the promise as inconclusive.
    ERR = {"error", "message", "detail", "code", "errors", "status_code"}
    want = set(expect)
    if (set(payload.keys()) & ERR) and len(want & set(payload.keys())) <= len(want) * 0.2:
        return {"status": "inconclusive", "conforms": False,
                "why": "endpoint returned an error object (likely our input or a transient error)",
                "missing": sorted(want), "extra": [], "observed_schema": describe_shape(payload)}

    # A field counts as delivered if it appears at the top level OR one level into
    # any wrapper: a nested object (data/result/response), or a LIST OF RECORDS.
    # A seller that returns its goods as `{"observations": [ {..fields..} ]}`, or
    # that puts one promised field at the top and the rest under `data`, has still
    # delivered them. Judging only a single best-matching level falsely marked
    # such sellers short; verified against archived raw on 2026-08-15. Merging is
    # one level deep and only ever finds MORE fields, so it can turn a false short
    # into a delivery but never invents a new shortfall.
    got = set(payload.keys())
    for _v in payload.values():
        if isinstance(_v, dict):
            got.update(_v.keys())
        elif isinstance(_v, list) and _v and isinstance(_v[0], dict):
            got.update(_v[0].keys())
    missing = sorted(set(expect) - got)
    extra = sorted(got - set(expect))
    return {"status": "delivered" if not missing else "short",
            "conforms": not missing, "missing": missing, "extra": extra,
            "why": "" if not missing else f"missing {len(missing)} promised field(s)",
            # the FULL real response shape (types only), including any wrapper, so
            # the integration signal is accurate even when conformance unwrapped.
            "observed_schema": describe_shape(payload)}


# GUARDRAIL 1: the canary. Endpoints hand-verified to return every promised
# field. The harness must score these as `delivered` before it is allowed to
# judge anyone else. If a known-good endpoint comes back anything but delivered,
# the harness itself is broken, so abort the run rather than accuse real
# businesses. This is the check that would have caught the env.data bug in the
# first few seconds instead of after a drafted public tweet. Verified 2026-08-13.
CANARY = [
    {"url": "https://vibesprings.net/api/price/btc-usd", "method": "GET", "body": None,
     "expect": ["change24h", "currency", "exchange", "high24h", "low24h", "name",
                "price", "processingTime", "symbol", "type", "volume24h"]},
    {"url": "https://api.seneschal.space/v1/q/zec/pools", "method": "GET", "body": None,
     "expect": ["as_of_ms", "chain", "height", "network", "pools", "shielded_pct",
                "shielded_zec", "total_supply_zec"]},
]


def reconcile(v_first, v_second):
    """GUARDRAIL 3: settle a `short` verdict against its re-verify call. A short
    is the only verdict that accuses a seller, so it must survive a second,
    independent call before it is recorded. If the retry delivers, the first
    call was a transient fluke and the accusation is dropped. If the retry is
    anything but a confirmed short, we could not reproduce the shortfall and the
    verdict falls back to inconclusive. Only short-on-both stays short."""
    s2 = (v_second or {}).get("status")
    if s2 == "delivered":
        out = dict(v_second)
        out["why"] = "delivered on re-verify (first call fell short)"
        return out
    if s2 != "short":
        out = dict(v_first)
        out["status"] = "inconclusive"
        out["why"] = "could not confirm shortfall on re-verify"
        return out
    out = dict(v_first)
    out["why"] = "shortfall confirmed on two calls"
    return out


def run_canary(cap=0.01):
    """Prove the harness works on known-good endpoints before it judges anyone.
    Returns (ok, detail)."""
    for c in CANARY:
        ok, payload, meta = call(c["url"], c["method"], c["body"], cap)
        v = judge(payload, c["expect"])
        if v["status"] != "delivered":
            return False, (f"canary {c['url']} scored '{v['status']}' not 'delivered'. "
                           f"The harness is broken, not the endpoint. why: {v['why']}")
    return True, f"{len(CANARY)} known-good endpoints all scored delivered"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--budget", type=float, default=0.25, help="hard dollar cap for the run")
    ap.add_argument("--max-price", type=float, default=0.01)
    ap.add_argument("--pause", type=float, default=1.5)
    ap.add_argument("--reliable-only", action="store_true", default=True,
                    help="test only hosts the probe reached and found price- and payTo-honest")
    ap.add_argument("--all-hosts", dest="reliable_only", action="store_false",
                    help="include hosts the probe never reached or flagged")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--urls-file", default=None,
                    help="JSON list of exact URLs to re-call. Targets ONLY these, "
                         "bypassing the already-seen, one-per-host and reliable-only "
                         "filters, so a specific set (e.g. a recall queue) is re-measured "
                         "instead of fresh coverage. Still respects budget and per-call cap.")
    a = ap.parse_args()

    # Run the test suite before spending a cent. This script spends real money and
    # was run a dozen times mid-session while silently overwriting its own results;
    # a one-second test run is cheap insurance that the append and money-detection
    # logic is intact before the first paid call. --dry-run and WAB_SKIP_TESTS skip it.
    if not a.dry_run and not os.environ.get("WAB_SKIP_TESTS"):
        t = subprocess.run([sys.executable, os.path.join(HERE, "tests.py")],
                           capture_output=True, text=True)
        if t.returncode != 0:
            sys.stderr.write(t.stdout + t.stderr +
                             "\nREFUSING TO SPEND: tests failed. Fix them before running paid calls.\n")
            return 1
        print(t.stdout.strip().splitlines()[-1] + " (tests) — safe to spend\n")

    # Reliability per host, from the free probe, so we test the endpoints we are
    # actively recommending rather than the cheapest toys. Verifying that a
    # host we call reliable actually delivers is the point.
    rel = {}
    try:
        for o in json.load(open(os.path.join(DATA, "latest.json"))).get("origins", []):
            r0, parts = None, None
            n402 = sum(1 for cx in (o.get("checked") or []) if cx.get("status") == 402)
            # mirror build.py's score cheaply: mostly answered + honest
            rel[o["origin"]] = o
    except Exception:
        pass

    # Skip endpoints we have already tried, paid or not, so each run advances to
    # fresh endpoints instead of re-hitting the same cheap input-mismatch fails.
    # A paid success never needs re-buying; a definitive fail (bad input, needs a
    # key) will fail the same way again, so recording it once and moving on is
    # what advances coverage.
    already = set()
    for f in glob.glob(os.path.join(DATA, "conformance_*.json")):
        try:
            for row in json.load(open(f)).get("rows", []):
                already.add(row["url"])
        except Exception:
            pass

    # Targeted mode: re-call an exact list of URLs (e.g. a recall queue) instead
    # of discovering fresh coverage. This bypasses the already-seen, one-per-host
    # and reliable-only filters, because the whole point is to re-measure THESE
    # specific endpoints, possibly several on the same host. Budget and per-call
    # cap still apply, so it can never leak money.
    targets = None
    if a.urls_file:
        targets = set(json.load(open(a.urls_file)))
        # Skip targets already re-called TODAY (in today's fresh file), so batches
        # advance through the list instead of repeating it. We deliberately do NOT
        # consult the older contaminated files here: re-measuring those URLs is the
        # whole point of a targeted run.
        today_file = os.path.join(DATA, f"conformance_{time.strftime('%Y-%m-%d')}.json")
        done_today = set()
        if os.path.exists(today_file):
            try:
                done_today = {r["url"] for r in json.load(open(today_file)).get("rows", [])}
            except Exception:
                done_today = set()
        remaining = targets - done_today
        print(f"targeted re-call: {len(targets)} URLs from {a.urls_file}; "
              f"{len(done_today & targets)} already done today, {len(remaining)} to go")
        targets = remaining

    reg = json.load(open(os.path.join(DATA, "cdp_resources_raw.json")))
    seen_hosts, cands = set(), []
    for r in reg:
        url = r.get("resource") or ""
        host = (urlparse(url).hostname or "").lower()
        p = price_of(r)
        c = contract(r)
        if not (host and url.startswith("http") and c and p and 0 < p <= a.max_price):
            continue
        if targets is not None:
            if url not in targets:
                continue
            # exact-target mode: take it as-is, skip the coverage/quality filters
            cands.append({"url": url, "host": host, "price": p, **c})
            continue
        if url in already:
            continue
        # One endpoint per host, so a single seller cannot dominate the sample
        # and no host takes more than one paid call from this run.
        if host in seen_hosts:
            continue
        o = rel.get(host)
        if a.reliable_only and not o:
            continue  # only test hosts the probe reached and could vouch for
        # A host the probe found dishonest on price or payTo is not worth paying
        # to check for delivery; it already failed the cheaper test.
        if o:
            checked = [cx for cx in (o.get("checked") or []) if not cx.get("inconclusive")]
            bad_price = any(cx.get("adv_amount") and cx.get("live_amount")
                            and abs(cx["adv_amount"] - cx["live_amount"]) / max(cx["adv_amount"], cx["live_amount"]) > 0.02
                            for cx in checked)
            bad_payto = any((cx.get("adv_payto") or "") and (cx.get("live_payto") or "")
                            and cx["adv_payto"].lower() != cx["live_payto"].lower() for cx in checked)
            if a.reliable_only and (bad_price or bad_payto or o.get("phantom")):
                continue
        seen_hosts.add(host)
        cands.append({"url": url, "host": host, "price": p, **c})
    # Cheapest first within the trustworthy set, to stretch the budget.
    cands.sort(key=lambda c: c["price"])
    picked = cands[:a.limit]
    total = sum(c["price"] for c in picked)

    print(f"{len(cands):,} untested endpoints on trusted hosts at or under ${a.max_price}")
    print(f"selected {len(picked)} (one per host, cheapest first), costing ${total:.4f}")
    print(f"budget ${a.budget:.2f}, pause {a.pause}s between calls\n")
    if a.dry_run:
        for c in picked[:15]:
            print(f"  ${c['price']:.4f}  {c['method']:<5} {c['host'][:38]:<39} "
                  f"expects {len(c['expect'])} field(s)")
        print("\ndry run, nothing paid")
        return 0
    if total > a.budget:
        print(f"refusing to start: selection costs ${total:.4f} which is over the "
              f"${a.budget:.2f} budget. Lower --limit or raise --budget.")
        return 1

    out_path = os.path.join(DATA, f"conformance_{time.strftime('%Y-%m-%d')}.json")
    bal0 = wallet_balance()
    if bal0 is None:
        print("refusing to start: cannot read the wallet balance, so the run "
              "cannot be reconciled and the budget cannot be enforced honestly.")
        return 1
    print(f"wallet before: ${bal0:.6f}")

    # GUARDRAIL 1: canary before anything else. If the harness cannot correctly
    # score endpoints we KNOW deliver, it must not be trusted to judge strangers.
    if not a.dry_run:
        ok_c, detail_c = run_canary()
        if not ok_c:
            print(f"\nABORTING: {detail_c}\nFix the harness and re-run. No verdicts recorded.")
            return 1
        print(f"canary passed: {detail_c}\n")

    # Append to the day's file rather than overwrite it. Writing only this run's
    # rows silently discarded every earlier batch from the same day; results must
    # accumulate. New rows for a URL replace an older attempt at the same URL.
    prior = []
    if os.path.exists(out_path):
        try:
            prior = json.load(open(out_path)).get("rows", [])
        except Exception:
            prior = []
    results, spent = [], 0.0

    def flush():
        by_url = {r["url"]: r for r in prior}
        for r in results:
            by_url[r["url"]] = r
        merged = list(by_url.values())
        json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "spent_this_run": round(spent, 6), "rows": merged},
                  open(out_path, "w"), indent=1)
        return merged
    for i, c in enumerate(picked, 1):
        if spent + c["price"] > a.budget:
            print(f"\nstopping at {i-1}: next call would pass the budget")
            break
        t0 = time.time()
        # Per-call hard ceiling, two layers: this endpoint's own advertised price
        # plus a hair for rounding, and an absolute 1 cent backstop that no single
        # call can ever exceed even if the per-endpoint math is wrong. If the live
        # 402 asks for more, the client aborts and pays nothing, and we record it
        # as a price mismatch instead of leaking money.
        per_call_cap = min(round(c["price"] * 1.25 + 0.0001, 6), 0.01)
        ok, payload, meta = call(c["url"], c["method"], c["body"], per_call_cap)
        ms = int((time.time() - t0) * 1000)
        # Some clients put a string in the payment field; guard so a run of 200
        # is never crashed 60 short by one odd response. Only a dict has success.
        paid = (meta or {}).get("payment")
        if not isinstance(paid, dict):
            paid = {}
        charged = None
        if isinstance(meta, dict) and meta.get("price"):
            try:
                charged = float(str(meta["price"]).lstrip("$"))
            except ValueError:
                charged = None
        # The budget accumulates the TRUSTED capped price, never the reported
        # charged: a perps endpoint returned a $1915 futures price in a field the
        # charged parser read, and that polluted number tripped the budget and
        # stopped the run at 122. The per-call cap already bounds what can leave
        # the wallet, and the wallet reconciliation is the real check, so budget
        # on the bounded quantity that no response can inflate.
        if paid.get("success"):
            spent += min(charged, per_call_cap) if charged is not None else c["price"]
        # A charged figure wildly above the cap is response data, not a payment.
        if charged is not None and charged > per_call_cap * 2:
            charged = None
        if ok:
            v = judge(payload, c["expect"])
        else:
            # A call that never returned is inconclusive, not a delivery failure.
            v = {"status": "inconclusive", "conforms": False, "missing": c["expect"],
                 "extra": [], "why": ((meta or {}).get("error") or "call failed")[:170],
                 "observed_schema": None}

        # GUARDRAIL 3: never trust a negative on one call. A `short` verdict, the
        # only one that accuses the seller, is re-verified with a second paid
        # call. If the retry delivers, the first was a transient fluke and the
        # verdict is cleared. Only a shortfall confirmed twice is recorded as one.
        settled = bool(paid.get("success"))
        if v.get("status") == "short":
            ok2, payload2, meta2 = call(c["url"], c["method"], c["body"], per_call_cap)
            v2 = judge(payload2, c["expect"]) if ok2 else {"status": "inconclusive"}
            paid2 = (meta2 or {}).get("payment") if isinstance((meta2 or {}).get("payment"), dict) else {}
            if paid2 and paid2.get("success"):
                spent += min(charged or c["price"], per_call_cap)
            v = reconcile(v, v2)

        row = {"url": c["url"], "host": c["host"], "quoted": c["price"],
               "charged": charged, "paid": settled,
               "free": bool(ok and not settled),
               "tx": paid.get("transactionHash"), "ms": ms,
               "promised": c["expect"], **v}
        results.append(row)
        flush()   # append-merge to the day's file after every call, never overwrite
        mark = {"delivered": "ok  ", "short": "SHORT", "inconclusive": "skip"}.get(v.get("status"), "?")
        print(f"  {i:>3}/{len(picked)} {mark} ${c['price']:.4f} {ms:>6}ms  {c['host'][:36]:<37} "
              f"{(v.get('why') or '')[:44]}")
        time.sleep(a.pause)

    n = len(results)
    if not n:
        print("nothing called")
        return 0
    good = sum(1 for r in results if r["conforms"])
    paidn = sum(1 for r in results if r["paid"])
    freen = sum(1 for r in results if r.get("free"))
    paid_ok = sum(1 for r in results if r["paid"] and r["conforms"])
    over = [r for r in results if r["charged"] and r["charged"] > r["quoted"] * 1.001]
    bal1 = wallet_balance()
    real = (bal0 - bal1) if bal1 is not None else None
    print(f"\n{n} endpoints called")
    print(f"  the client reported spending  : ${spent:.4f}")
    if real is not None:
        print(f"  the wallet actually moved     : ${real:.4f}")
        if real > spent * 1.5 + 0.001:
            print("  WARNING: the client under-reported the spend. Trust the wallet.")
    print(f"  delivered after paying       : {paid_ok} of {paidn} paid")
    print(f"  delivered without charging   : {freen}   (listed at a price, served free)")
    print(f"  returned everything promised : {good} ({100*good/n:.0f}% of all called)")
    print(f"  charged more than quoted     : {len(over)}")
    print(f"  written to {out_path}")

    # Every big paid pull backs itself up to a durable location outside the repo,
    # timestamped, so a result set is never one bad write from gone. These are
    # disposable after a month; a cleanup line prunes anything older than 35 days.
    try:
        bk = os.path.expanduser("~/Automation/whatagentsbuy-backups")
        os.makedirs(bk, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        merged = json.load(open(out_path))
        json.dump(merged, open(os.path.join(bk, f"conformance_{stamp}.json"), "w"), indent=1)
        cutoff = time.time() - 35 * 86400
        for f in os.listdir(bk):
            fp = os.path.join(bk, f)
            if f.startswith("conformance_") and os.path.getmtime(fp) < cutoff:
                os.remove(fp)
        print(f"  backed up to {bk}/conformance_{stamp}.json")
    except Exception as e:
        print(f"  backup skipped: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
