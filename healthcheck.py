#!/usr/bin/env python3
"""End-to-end data-flow check. Run daily, after the build and deploy.

tests.py proves the LOGIC is right against fixtures. This proves the DATA is
actually moving: that the tape advanced, that the split is computable, that the
build picked it up, and that the live site is serving today's numbers rather
than a cached copy of last week's.

The failure this is built to catch is the silent one. Every real incident on
this project looked healthy from the inside: the sweep "succeeded" while writing
a phantom $0 day, the git push logged "skipped (nothing new)" for weeks while
backing up nothing, the site served a stale build after a failed deploy. A green
stage log is not evidence of a working pipeline, so every check here compares
two independent things that must agree, and shouts when they do not.

  python3 healthcheck.py            # local + live
  python3 healthcheck.py --local    # skip network (for pre-deploy gating)
  python3 healthcheck.py --quiet    # only print problems
"""
import argparse, glob, json, os, re, sys, urllib.error, urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
HIST = os.path.join(DATA, "history")
SITE = "https://whatagentsbuy.com"
UA = {"User-Agent": "whatagentsbuy-healthcheck/1.0"}

BURN = {"0x0000000000000000000000000000000000000000",
        "0x000000000000000000000000000000000000dead"}

# How stale the newest TRUSTED day may be before it is a problem. The sweep runs
# daily and reads a trailing window, so one full day of lag is normal and two is
# the outside of normal. Three means something has been quietly failing.
MAX_TRUSTED_LAG_DAYS = 3

_problems, _checks = [], 0


def ok(name, cond, detail=""):
    global _checks
    _checks += 1
    if not cond:
        _problems.append(f"{name}" + (f" :: {detail}" if detail else ""))
    return bool(cond)


def get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def today_utc():
    return datetime.now(timezone.utc).date()


# --- local -----------------------------------------------------------------

def check_market_json():
    p = os.path.join(DATA, "market.json")
    if not ok("market.json exists", os.path.exists(p), p):
        return None
    m = json.load(open(p))

    days = m.get("series", [])
    ok("market.json has days", len(days) > 0, f"{len(days)}")

    latest = m.get("latest_trusted_day")
    if ok("a trusted day exists", bool(latest),
          "no day passed the sweep-clean + splittable bar; headlines will be blank"):
        d = datetime.strptime(latest["date"], "%Y-%m-%d").date()
        lag = (today_utc() - d).days
        ok(f"newest trusted day is recent ({latest['date']}, {lag}d old)",
           lag <= MAX_TRUSTED_LAG_DAYS,
           f"the tape has not produced a publishable day in {lag} days")

    # Buckets must reconstruct the reported total, or the page contradicts its
    # own arithmetic in public.
    for d in days:
        parts = (d["organic_usdc"] + d["concentrated_usdc"] + d["unclassified_usdc"])
        if abs(parts - d["reported_usdc"]) > 0.05:
            ok(f"{d['date']}: buckets sum to reported", False,
               f"{parts:.2f} vs {d['reported_usdc']:.2f}")
    ok("all days reconcile", True)

    # A share above 1 or below 0 means a divide-by-something-wrong.
    for d in days:
        for k in ("organic_share", "organic_tx_share", "circular_share"):
            v = d.get(k)
            if v is not None and not (0.0 <= v <= 1.0):
                ok(f"{d['date']}: {k} in range", False, f"{v}")
    ok("all shares in [0,1]", True)
    return m


def check_tape():
    files = sorted(f for f in glob.glob(os.path.join(HIST, "settlements_*.json"))
                   if "solana" not in os.path.basename(f))
    ok("settlement tape present", len(files) > 0, f"{len(files)} files")

    dates = []
    for f in files:
        d = json.load(open(f))
        by = d.get("by_address") or {}
        date = d.get("date")
        dates.append(date)

        # A completed sweep with nothing in it is always an RPC failure on Base.
        ok(f"{date}: not an empty day", len(by) > 0,
           "zero addresses recorded; this is a phantom, not a real $0")

        # The 8/18 incident: a seller advertised 0x0, so every USDC burn on chain
        # mapped to it and the file archived a $138,564,409 phantom. The pipeline
        # excludes burns now, but an archived file can still carry one.
        bad = [a for a in by if a.lower() in BURN]
        ok(f"{date}: no burn address in tape", not bad, f"{bad}")

    # Gaps are legitimate (a missed sweep cannot be recovered from a rolling
    # window) but they must be visible, because a chart with a hole in it and a
    # chart of a market that stopped look identical.
    if dates:
        span = [datetime.strptime(x, "%Y-%m-%d").date() for x in dates if x]
        want = {span[0] + timedelta(days=i) for i in range((span[-1] - span[0]).days + 1)}
        missing = sorted(str(x) for x in (want - set(span)))
        ok("tape has no gaps", not missing,
           f"missing {', '.join(missing)} (known and acceptable if named on the page)")


def check_inputs_fresh():
    """Input snapshots must actually be refreshing.

    x402scan_sellers.json sat frozen for 17 days because fetch_sellers.py was
    written and never wired into the daily job. Nothing failed. probe.py used
    the stale file as both half of its universe and its ranking key, so every
    seller that appeared afterwards had no recorded dollars, sorted to the
    bottom, fell past the probe cap, was never probed, never yielded a payTo,
    and so never entered the tape -- which kept it at the bottom. A stale input
    is invisible from the inside; only its mtime tells you.
    """
    limits = {
        "cdp_resources_raw.json": 3,      # refreshed daily
        "x402scan_sellers.json": 3,       # refreshed daily
        "latest.json": 3,                 # written by every probe run
        "leaderboard.json": 3,
        "market.json": 3,
    }
    for name, max_days in limits.items():
        p = os.path.join(DATA, name)
        if not os.path.exists(p):
            ok(f"input {name} exists", False, p)
            continue
        age = (datetime.now(timezone.utc)
               - datetime.fromtimestamp(os.path.getmtime(p), timezone.utc)).days
        ok(f"input {name} is fresh ({age}d old)", age <= max_days,
           f"stale by {age - max_days}d: the job that writes it is not running")


def check_probe_coverage():
    """Every origin in the universe should eventually be probed.

    An origin that is never probed contributes no payTo, so its settlement is
    invisible to the tape no matter how well the sweep runs. That is a coverage
    undercount, and it is entirely separate from the range-loss undercount the
    sweep guards against.
    """
    p = os.path.join(DATA, "latest.json")
    if not os.path.exists(p):
        return
    origins = json.load(open(p)).get("origins", [])
    ok("probe covered a real universe", len(origins) > 1000, f"only {len(origins)} origins")
    with_addr = sum(1 for o in origins
                    if (o.get("payto_addresses") or [])
                    or any(str(c.get("live_payto") or c.get("adv_payto") or "").startswith("0x")
                           for c in o.get("checked", [])))
    # Roughly half of probed origins yield no address (dead hosts, unparsed
    # challenges). Worth watching: a sharp drop means the challenge parser broke.
    share = with_addr / len(origins) if origins else 0
    ok(f"probed origins yielding a payTo ({with_addr}/{len(origins)}, {share:.0%})",
       share > 0.25, "too few origins yield an address; the challenge parser may be broken")


def check_build_output():
    p = os.path.join(HERE, "public", "x402", "index.html")
    if not ok("/x402 was built", os.path.exists(p), p):
        return
    s = open(p).read()
    # The dashboard is a JS-rendered page, so "it built" is a weak claim. What
    # can be checked statically is that the pieces render() needs survived.
    ok("/x402 has the hero section", 'id="p-hero"' in s)
    ok("/x402 has the null-element shim", "document.getElementById = id => real(id)" in s,
       "without it one absent panel throws and the whole page paints nothing")
    ok("/x402 fetches the live API", "/api/dashboard" in s)
    for _id in ("hv", "ht", "cv", "ct", "cmv", "cmt", "bsf", "btx"):
        ok(f"/x402 keeps element #{_id}", f'id="{_id}"' in s)
    # A public page must not carry the private dashboard's wallet panels.
    ok("/x402 has no wallet panel", 'id="p-wallets"' not in s and 'id="p-sales"' not in s)
    import re as _re
    ok("/x402 leaks no wallet address", not _re.search(r"0x[a-fA-F0-9]{40}", s))
    ok("og-x402 card was rendered",
       os.path.exists(os.path.join(HERE, "public", "og-x402.png")))


# --- live ------------------------------------------------------------------

def check_live(m):
    try:
        st, body = get(f"{SITE}/x402")
    except Exception as e:
        ok("live /x402 reachable", False, f"{type(e).__name__}: {e}")
        return
    ok("live /x402 returns 200", st == 200, f"got {st}")
    ok("live /x402 shipped the hero", 'id="p-hero"' in body)

    # The dashboard renders client-side, so the page being up proves nothing:
    # the API behind it is what actually fills every card.
    try:
        st2, raw = get(f"{SITE}/api/dashboard", timeout=45)
        live = json.loads(raw)
    except Exception as e:
        ok("live /api/dashboard parses", False, f"{type(e).__name__}: {e}")
        return
    ok("live /api/dashboard returns 200", st2 == 200, f"got {st2}")

    for k in ("hero", "circ", "momentum", "baseSeq"):
        ok(f"live {k} present", k in live and live[k] is not None)
        if isinstance(live.get(k), dict) and live[k].get("error"):
            ok(f"live {k} has no upstream error", False, live[k]["error"])

    h = live.get("hero") or {}
    ok("live 24h volume is a number", isinstance(h.get("vol24"), (int, float)))
    ok("live 24h transactions is a number", isinstance(h.get("tx24"), (int, float)))
    ok("live daily series has 7 closed days", len(h.get("daily7") or []) == 7,
       f"got {len(h.get('daily7') or [])}")
    ok("live partial day present", bool(h.get("today")),
       "without it the running bar paints as a completed day")

    mo = ((live.get("momentum") or {}).get("history") or {}).get("monthly") or []
    ok("live monthly history present", len(mo) > 0,
       "the two monthly-since-launch cards would render empty")
    bs = live.get("baseSeq") or {}
    ok("live base sequencer present", isinstance(bs.get("fees24h"), (int, float)),
       "the Base sequencer cards would render empty")

    # IDENTITY, not just shape (G3): a structurally valid page can serve last
    # week's dataset — a mock with 1999 dates once passed every check here. So
    # compare what the LIVE site serves against what THIS RUN intends to
    # publish: the local tape's newest day and totals must be the ones live.
    try:
        lb_local = json.load(open(os.path.join(DATA, "leaderboard.json")))
        st4, raw4 = get(f"{SITE}/api/leaderboard.json", timeout=30)
        lb_live = json.loads(raw4)
        ok("live leaderboard serves the LOCAL tape's newest day",
           lb_live.get("as_of") == lb_local.get("last_day"),
           f"live as_of={lb_live.get('as_of')} vs local last_day={lb_local.get('last_day')} "
           f"(the deploy did not land, or landed an older build)")
        _lt = lb_live.get("total_usdc")
        _ll = (lb_local.get("windows", {}).get("1d", {}) or {}).get("total_usdc")
        ok("live 24h total matches the local tape's",
           _lt is not None and _ll is not None and abs(_lt - _ll) < 0.01,
           f"live={_lt} local={_ll}")
    except Exception as e:
        ok("live-vs-local leaderboard identity comparable", False, f"{type(e).__name__}: {e}")
    if m:
        try:
            st5, raw5 = get(f"{SITE}/api/market.json", timeout=30)
            mk_live = json.loads(raw5)
            _dloc = ((m.get("latest_trusted_day") or {}).get("date"))
            _dliv = ((mk_live.get("latest_trusted_day") or {}).get("date"))
            ok("live market series carries the local latest trusted day",
               _dloc is not None and _dliv == _dloc,
               f"live={_dliv} local={_dloc}")
        except Exception as e:
            ok("live-vs-local market identity comparable", False, f"{type(e).__name__}: {e}")

    # Contract, live: the deployed page must not read a key the deployed API
    # stopped sending. This is the failure that blanks cards silently.
    refs = set(re.findall(r"\bd\.([a-zA-Z_][a-zA-Z0-9_]*)", body))
    # `error` is read to DETECT a failure payload, not expected as a field.
    # Anything the page checks for absence is not part of the contract.
    not_payload = {"error",
                   "bind", "toFixed", "toLocaleString", "getTime", "getDay",
                   "getDate", "getMonth", "getFullYear", "v", "tx", "vol",
                   "days", "now", "circulating", "date", "partial", "through",
                   "label", "length", "map", "filter", "slice", "push", "forEach"}
    missing = sorted(k for k in refs if k not in not_payload and k not in live)
    ok("live page reads only keys the live API sends", not missing,
       f"page reads d.{', d.'.join(missing)} which the API does not return")


CHROME = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
          "/Applications/Chromium.app/Contents/MacOS/Chromium",
          "/usr/bin/google-chrome", "/usr/bin/chromium")

# Placeholder glyphs the template ships with. A card still showing one of these
# after render() has run did not receive data.
_EMPTY = {"", "-", "\u2014", "\u2013", "None", "NaN", "$NaN", "undefined"}


def check_renders():
    """Load the LIVE page in a real browser and confirm the cards filled.

    Everything else here is static or data-shaped: the HTML contains the right
    element ids, the API returns the right keys. None of that proves the page
    PAINTS. render() runs every card in one pass, so a JavaScript error anywhere
    in it leaves the cards after the throw showing their placeholder dash while
    the markup and the API both look perfect. That is the exact failure this
    project has shipped twice, and it is invisible to every other check.
    """
    exe = next((c for c in CHROME if os.path.exists(c)), None)
    if not exe:
        return                       # no browser here; not a failure
    import subprocess, re as _re
    try:
        dom = subprocess.run(
            [exe, "--headless", "--disable-gpu", "--no-sandbox",
             "--virtual-time-budget=20000", "--dump-dom", f"{SITE}/x402"],
            capture_output=True, text=True, timeout=90).stdout
    except Exception as e:
        ok("live page renders", False, f"headless run failed: {type(e).__name__}")
        return
    if not dom:
        ok("live page renders", False, "headless returned nothing")
        return

    # The headline figure of every card the page is supposed to fill.
    for eid, what in (("hv", "x402 volume 24h"), ("ht", "x402 transactions 24h"),
                      ("hu", "USDC circulating"), ("hs", "all stablecoins"),
                      ("bsf", "Base sequencer fees"), ("btx", "Base transactions")):
        m = _re.search(r'id="%s"[^>]*>([^<]*)<' % eid, dom)
        val = (m.group(1).strip() if m else None)
        ok(f"card #{eid} painted ({what})", bool(val) and val not in _EMPTY,
           f"still showing {val!r} after render: this card is blank on the live site")

    # Charts are SVG rects. Zero means the series never reached the chart code
    # even though the numbers above it may have painted.
    ok("charts drew bars", dom.count("<rect") > 50,
       f"only {dom.count('<rect')} rects; the charts are empty")

    # The old home of this dashboard must keep pointing at the new one. A
    # redirect that quietly breaks strips every link and bookmark that URL
    # earned, which was the entire reason for consolidating.
    try:
        req = urllib.request.Request("https://x402.neilkpatel.com/", headers=UA)
        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(_NoRedirect)
        try:
            r = op.open(req, timeout=20)
            code, loc = r.status, r.headers.get("location", "")
        except urllib.error.HTTPError as e:
            code, loc = e.code, e.headers.get("location", "")
        ok("x402.neilkpatel.com still redirects here", code in (301, 308),
           f"got {code}, expected a permanent redirect")
        ok("redirect points at /x402", loc.rstrip("/").endswith("/x402"),
           f"points at {loc!r}")
    except Exception as e:
        ok("x402.neilkpatel.com reachable", False, f"{type(e).__name__}: {e}")

    # And the page must not be showing its own failure banner.
    ok("no failure banner on the live page",
       'class="notice err"' not in dom,
       "the page is telling visitors live data is unavailable")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", action="store_true", help="skip live checks")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    m = check_market_json()
    check_tape()
    check_inputs_fresh()
    check_probe_coverage()
    check_build_output()
    if not args.local:
        check_live(m)
        check_renders()

    if _problems:
        print(f"HEALTHCHECK FAILED: {len(_problems)} of {_checks} checks\n")
        for p in _problems:
            print(f"  - {p}")
        return 1
    if not args.quiet:
        print(f"healthcheck ok: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
