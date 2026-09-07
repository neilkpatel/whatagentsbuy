#!/usr/bin/env python3
"""Preflight verdict logic, pure and testable.

The pre-payment oracle collapses payment-safety, delivery receipts and demand
into one light: green / yellow / red / gray = CLEAR / HOLD / ABORT / UNRATED.
This module holds the DECISION rules as pure functions so the most public-facing
logic on the site, a verdict that can tell an agent "do not pay this named
seller", is locked by tests instead of trusting build.py to read correctly.
build.py looks up the per-host inputs and calls verdict(); the tests call it with
fixtures.

THE INVARIANT THAT MATTERS MOST: a red (ABORT) fires ONLY from evidence that
money or goods actually went wrong, a payTo that disagrees with the listing, a
phantom paywall, or a reverified severe underdeliver. A soft signal (weak demand,
reselling free data, a price mismatch on its own) never turns a light red. That
is verify-before-accusing, and test_preflight_red_only_from_hard_evidence
enforces it.
"""

SHAPE_TYPES = {"string", "number", "integer", "boolean", "null", "array", "object", "unknown"}


def shape_is_clean(s):
    """The publish-boundary guard: a response SHAPE (type names only) is safe to
    publish, a real value is not. Every leaf must be a type name or null, so no
    receipt can leak the goods we paid for onto the site."""
    if isinstance(s, dict):
        return all(shape_is_clean(v) for v in s.values())
    if isinstance(s, list):
        return all(shape_is_clean(v) for v in s)
    return s is None or s in SHAPE_TYPES


# Free-data flag: the site's signature finding as a per-verdict signal. Specific
# phrases only (not bare words) to keep false positives low; phrased as "looks
# like" and kept at info level, because paying for free data is a business call,
# not a safety failure, so it never moves the light.
FREE_RULES = [
    (("weather", "forecast", "temperature", "open-meteo", "meteo"), "weather", "NWS and Open-Meteo, both free"),
    (("gas price", "gas fee", "gwei", "base fee", "basefee", "gas oracle"), "gas price", "the chain's own base fee, a free read"),
    (("wallet balance", "token balance", "erc20 balance", "erc-20 balance", "balanceof", "address balance"), "wallet balance", "the chain via balanceOf, a free read"),
    (("btc price", "eth price", "crypto price", "token price", "coin price", "spot price", "price feed", "coingecko"), "crypto price", "Coinbase or the chain, free"),
    (("web search", "search the web", "google search", "serp ", "duckduckgo"), "web search", "an agent's own web search, free"),
    (("whois", "domain lookup", "domain whois"), "WHOIS", "public WHOIS, free"),
    (("latest block", "chain height", "block height", "gas estimate"), "chain data", "any public RPC, free"),
]


def free_category(text):
    t = (text or "").lower()
    for kws, label, src in FREE_RULES:
        if any(k in t for k in kws):
            return {"label": label, "source": src}
    return None


def probe_signals(checks, phantom, rotates_payto=None):
    """Collapse a host's live 402 checks into tri-state payment-safety signals.

    True = measured and matching. False = measured and MISmatching. None = not
    measurable: no check captured both the advertised and the live value. The
    old form scored "no detected mismatch" as a match, so a host whose probe
    timed out or never yielded a comparable pair read identically to one whose
    live 402 was actually read and compared; 1,533 of 3,290 origins in the
    2026-09-07 snapshot had no measurable pair at all. Comparisons use
    `is not None`, never truthiness, so an advertised price of zero against a
    real live charge is a measured mismatch, not a silent skip.

    rotates_payto: the probe's own two-quote test (probe.py asks twice; a
    different payTo each time means fresh addresses are minted by design). A
    rotating seller's live payTo is EXPECTED to differ from its listing, so a
    mismatch there is not evidence of misdirection; the signal becomes None
    (unverifiable against a listing) and the rotation travels with the dict.
    Dropping this evidence once made four named sellers, Tavily, Bytemine,
    Allium and Browserbase, wear a false ABORT.
    """
    cs = [c for c in (checks or []) if not c.get("inconclusive")]
    price_pairs = [c for c in cs
                   if c.get("adv_amount") is not None and c.get("live_amount") is not None]
    price_mism = any(
        abs(c["adv_amount"] - c["live_amount"]) / max(c["adv_amount"], c["live_amount"], 1e-12) > 0.02
        for c in price_pairs)
    payto_pairs = [c for c in cs if (c.get("adv_payto") or "") and (c.get("live_payto") or "")]
    payto_mism = any(c["adv_payto"].lower() != c["live_payto"].lower() for c in payto_pairs)
    if rotates_payto:
        payto = None                      # listing comparison does not apply
    else:
        payto = False if payto_mism else (True if payto_pairs else None)
    return {
        "price_ok": False if price_mism else (True if price_pairs else None),
        "payto_ok": payto,
        "phantom": bool(phantom),
        "rotates_payto": bool(rotates_payto),
    }


def _settled(r):
    """Did money actually move for this receipt? payment.paid is a bool on
    delivery receipts and an amount on accuracy receipts; both count only when
    truthy, so a free-tier or failed call is never 'paid' evidence."""
    try:
        return bool((r.get("payment") or {}).get("paid"))
    except Exception:
        return False


def verdict(host, probe, receipts, lb, free_cat=None):
    """The pure Preflight verdict. All inputs already looked up:
      probe:    {price_ok, payto_ok, phantom} or None  (free 402 checks)
      receipts: list of this host's receipt dicts       (paid delivery/accuracy)
      lb:       this host's leaderboard row or None      (on-chain demand)
      free_cat: free_category(text) result or None
    Returns the verdict dict rendered to /api/preflight.json and the seller badge.
    """
    host = (host or "").lower().replace("www.", "").split("/")[0]
    recs = receipts or []
    reasons = []
    # Evidence is split BEFORE any claim is made from it. A conclusive result is
    # one where the paid call produced something gradeable; inconclusive is our
    # failure to measure and supports no claim in either direction. Only
    # receipts whose payment actually settled back a "we paid this seller"
    # statement: a free-tier response is information, never verification.
    CONCLUSIVE = {"delivered", "short", "accurate", "off"}
    paid_recs = [r for r in recs if _settled(r)]
    conclusive_paid = [r for r in paid_recs if r["verdict"]["status"] in CONCLUSIVE]
    inconclusive = [r for r in recs if r["verdict"]["status"] not in CONCLUSIVE]
    free_concl = [r for r in recs if not _settled(r) and r["verdict"]["status"] in CONCLUSIVE]
    shorts = [r for r in paid_recs if r["verdict"]["status"] == "short"]
    severe = [r for r in shorts if r["kind"] == "delivery"
              and len(r["promise"].get("fields") or []) >= 2
              and len(r["delivery"].get("missing") or []) == len(r["promise"]["fields"])]
    delivered = [r for r in paid_recs if r["verdict"]["status"] == "delivered"]
    accurate = [r for r in paid_recs if r["kind"] == "accuracy" and r["verdict"]["status"] == "accurate"]
    offs = [r for r in paid_recs if r["kind"] == "accuracy" and r["verdict"]["status"] == "off"]
    # Probe signals are tri-state (see probe_signals): True and False are
    # measurements, None is "could not be measured" and never becomes a claim.
    probe_measured = bool(probe) and (probe.get("price_ok") is not None
                                      or probe.get("payto_ok") is not None
                                      or probe.get("phantom")
                                      or probe.get("rotates_payto"))

    # RED: money would go somewhere wrong, or paid-for goods did not arrive at all
    if probe and probe.get("payto_ok") is False:
        reasons.append(("red", "Its live payment address does not match its listing. Money would go to an address the directory does not name."))
    if probe and probe["phantom"]:
        reasons.append(("red", "Phantom paywall: it returns a priced 402 for routes that cannot exist, which "
                               "means its payment middleware runs before route validation. A quote is not "
                               "evidence of a real endpoint, and a paid call to a wrong route may settle and "
                               "return nothing."))
    if severe:
        reasons.append(("red", f"Paid in full and returned none of its {len(severe[0]['promise']['fields'])} promised fields, confirmed on two calls."))
    # YELLOW: pay, but verify
    if probe and probe.get("price_ok") is False:
        reasons.append(("yellow", "Its live quote disagrees with its listing. Budget from the live 402, never from the listing."))
    if shorts and not severe:
        reasons.append(("yellow", f"Underdelivered on {len(shorts)} paid call(s): a real response missing promised fields, confirmed on two calls."))
    if offs:
        reasons.append(("yellow", f"Returned a number outside tolerance against a primary source on {len(offs)} paid check(s)."))
    # GOOD: positive, MEASURED evidence only. An unmeasured signal never
    # produces match text; that is the whole point of the tri-state.
    if delivered:
        reasons.append(("good", f"Delivered everything its schema promised on {len(delivered)} paid call(s), each with a verifiable receipt."))
    if accurate:
        reasons.append(("good", f"Returned an accurate number against a primary source on {len(accurate)} check(s)."))
    if probe and probe.get("payto_ok") is True and not probe["phantom"]:
        reasons.append(("good", "Its live payment address matches its listing."))
    if probe and probe.get("price_ok") is True and not probe["phantom"]:
        reasons.append(("good", "Its live quote matches its listed price."))
    # INFO: context that does NOT move the light
    if probe and probe.get("rotates_payto"):
        reasons.append(("info", "It issues a fresh receiving address per request (confirmed across two live "
                                "quotes), so a listing comparison does not apply to its payTo. That is a "
                                "design choice, not misdirection. Read the payTo from the live 402, as always."))
    if probe and not probe["phantom"]:
        _unmeasured = [n for n, k in (("live price", "price_ok"), ("payment address", "payto_ok"))
                       if probe.get(k) is None and not (k == "payto_ok" and probe.get("rotates_payto"))]
        if _unmeasured:
            reasons.append(("info", f"Its {' and '.join(_unmeasured)} could not be measured against its "
                                    f"listing on the free probe, so that is unknown, not verified."))
    if free_cat:
        reasons.append(("info", f"This looks like {free_cat['label']} data, which is available free from "
                                f"{free_cat['source']}. A paid call buys packaging or convenience, not exclusive "
                                f"data, so confirm you need it before paying."))
    if lb and lb.get("demand") == "one wallet":
        reasons.append(("info", "Almost all its revenue comes from a single wallet, so volume is not broad demand."))
    if free_concl:
        reasons.append(("info", f"{len(free_concl)} conclusive result(s) came from calls with no settled "
                                f"payment (free tier or failed settlement); informative, but not paid verification."))
    if not recs:
        reasons.append(("info", "Not yet bought from, so delivery is unverified. Absence of a grade is not a bad grade."))
    elif not conclusive_paid:
        reasons.append(("info", f"{len(recs)} recorded attempt(s), but none settled a payment with a "
                                f"conclusive result, so delivery is unverified. Absence of a grade is not a bad grade."))

    # Payment history: the proof a probe-only safety checker cannot show. Every
    # receipt is one call we PAID for, so these are real spends against this exact
    # seller, not an inspection of its 402 challenge. This is the differentiator to
    # surface everywhere the verdict travels (preflight.json, MCP, preflight-x402).
    history = None
    if recs:
        dates = sorted(r.get("ts") for r in (paid_recs or recs) if r.get("ts"))
        first = dates[0] if dates else None
        last = dates[-1] if dates else None
        span = None
        if first and last and first != last:
            try:
                from datetime import date as _date
                span = (_date.fromisoformat(last) - _date.fromisoformat(first)).days
            except Exception:
                span = None
        latest = max((r for r in (paid_recs or recs) if r.get("ts")), key=lambda r: r["ts"], default=None)
        # Freshness: how recent the newest paid check is. A verified verdict is only
        # as good as it is fresh, so the age travels with the verdict everywhere.
        age_days = None
        if last:
            try:
                from datetime import date as _date
                age_days = (_date.today() - _date.fromisoformat(last)).days
            except Exception:
                age_days = None
        history = {
            "times_paid": len(paid_recs), "attempts": len(recs),
            "inconclusive": len(inconclusive),
            "first_paid": first, "last_paid": last,
            "last_paid_age_days": age_days, "span_days": span,
            "delivered": len(delivered), "accurate": len(accurate), "disputed": len(shorts),
            "last_verdict": (latest or {}).get("verdict", {}).get("status"),
        }
        # Stated in words at good level so it shows in the reasons an agent
        # reads, but ONLY when a payment actually settled: 102 hosts once wore
        # "we have paid this seller" off receipts that never settled a cent.
        # A count of real spends never moves a light on its own, so this is safe.
        if paid_recs:
            _span_txt = (f" over {span} day{'s' if span != 1 else ''}" if span else "")
            reasons.insert(0, ("good",
                f"We have paid this seller {len(paid_recs)} time{'s' if len(paid_recs) != 1 else ''}{_span_txt} with a real "
                f"wallet: {len(delivered)} delivered in full, {len(accurate)} graded accurate, {len(shorts)} "
                f"underdelivered. This verdict is built on spend, not a probe of its paywall."))

    has_data = bool(probe or recs or lb)
    has_red = any(l == "red" for l, _ in reasons)
    has_yellow = any(l == "yellow" for l, _ in reasons)
    has_good = any(l == "good" for l, _ in reasons)
    # Green requires AFFIRMATIVE measured evidence, not merely the absence of a
    # detected problem: a probe that answered but measured nothing, or a host
    # known only from the settlement tape, stays UNRATED rather than wearing a
    # green it never earned. Red and yellow still fire from measurements alone.
    light = ("gray" if not has_data else "red" if has_red else "yellow" if has_yellow
             else ("green" if has_good else "gray"))
    score = None
    if light != "gray":
        score = max(0, 100 - sum(45 if l == "red" else 20 if l == "yellow" else 0 for l, _ in reasons))

    # Confidence: HOW MUCH this verdict is backed by, orthogonal to the light. The
    # site's whole claim is that it PAYS, yet 82% of green verdicts are probe-only
    # and score identically to sellers we paid dozens of times. A green a real
    # wallet has tested is not the same evidence as a green we only glanced at, so
    # the depth is made explicit: "verified" (paid this seller) outranks "checked"
    # (free live probe only) outranks "unproven" (listed, not yet checked). This is
    # the differentiator over probe-only checkers, surfaced instead of averaged away.
    # "verified" is the claim a wallet moved money AND the result was gradeable.
    # 70 hosts once wore it off receipts that were all inconclusive and 102 off
    # receipts that never settled a payment; existence of a record is not
    # evidence, its conclusiveness is.
    if conclusive_paid:
        confidence = "verified"
        _fresh = ""
        if age_days == 0:
            _fresh = ", most recently today"
        elif age_days == 1:
            _fresh = ", most recently yesterday"
        elif age_days:
            _fresh = f", most recently {age_days} days ago"
        confidence_basis = (f"backed by {len(conclusive_paid)} settled payment"
                            f"{'s' if len(conclusive_paid) != 1 else ''} to this seller "
                            f"with a conclusive result{_fresh}")
    elif probe_measured:
        confidence = "checked"
        confidence_basis = "free live checks only (live price and payTo vs the listing); not yet paid"
    elif probe:
        confidence = "unproven"
        confidence_basis = ("answered a live probe, but neither its price nor its payment address "
                            "could be measured against the listing; not yet paid")
    elif recs:
        confidence = "unproven"
        confidence_basis = (f"{len(recs)} recorded attempt{'s' if len(recs) != 1 else ''}, but none "
                            f"settled a payment with a conclusive result")
    else:
        confidence = "unproven"
        confidence_basis = "listed in the directory, but not yet checked live or paid"

    return {"host": host, "light": light, "score": score, "confidence": confidence,
            "confidence_basis": confidence_basis,
            "reasons": [{"level": l, "text": t} for l, t in reasons],
            "checked_live": bool(probe), "receipts": len(recs),
            "settled_payments": len(paid_recs), "disputes": len(shorts),
            "delivered": len(delivered), "accurate": len(accurate),
            "history": history, "free_alternative": free_cat}
