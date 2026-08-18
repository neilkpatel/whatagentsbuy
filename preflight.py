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
    shorts = [r for r in recs if r["verdict"]["status"] == "short"]
    severe = [r for r in shorts if r["kind"] == "delivery"
              and len(r["promise"].get("fields") or []) >= 2
              and len(r["delivery"].get("missing") or []) == len(r["promise"]["fields"])]
    delivered = [r for r in recs if r["verdict"]["status"] == "delivered"]
    accurate = [r for r in recs if r["kind"] == "accuracy" and r["verdict"]["status"] == "accurate"]

    # RED: money would go somewhere wrong, or paid-for goods did not arrive at all
    if probe and not probe["payto_ok"]:
        reasons.append(("red", "Its live payment address does not match its listing. Money would go to an address the directory does not name."))
    if probe and probe["phantom"]:
        reasons.append(("red", "Phantom paywall: it quotes a price for a route that cannot exist, so the price is not evidence of a real endpoint."))
    if severe:
        reasons.append(("red", f"Paid in full and returned none of its {len(severe[0]['promise']['fields'])} promised fields, confirmed on two calls."))
    # YELLOW: pay, but verify
    if probe and not probe["price_ok"]:
        reasons.append(("yellow", "Its live quote disagrees with its listing. Budget from the live 402, never from the listing."))
    if shorts and not severe:
        reasons.append(("yellow", f"Underdelivered on {len(shorts)} paid call(s): a real response missing promised fields, confirmed on two calls."))
    # GOOD: positive, verifiable evidence
    if delivered:
        reasons.append(("good", f"Delivered everything its schema promised on {len(delivered)} paid call(s), each with a verifiable receipt."))
    if accurate:
        reasons.append(("good", f"Returned an accurate number against a primary source on {len(accurate)} check(s)."))
    if probe and probe["payto_ok"] and probe["price_ok"] and not probe["phantom"]:
        reasons.append(("good", "Live price and payment address both match its listing."))
    # INFO: context that does NOT move the light
    if free_cat:
        reasons.append(("info", f"This looks like {free_cat['label']} data, which is available free from "
                                f"{free_cat['source']}. A paid call buys packaging or convenience, not exclusive "
                                f"data, so confirm you need it before paying."))
    if lb and (lb.get("sends_back_pct") or 0) >= 90:
        reasons.append(("info", f"Sends {lb['sends_back_pct']}% of receipts straight back out, so its volume may be recycling rather than demand."))
    if lb and lb.get("demand") == "one wallet":
        reasons.append(("info", "Almost all its revenue comes from a single wallet, so volume is not broad demand."))
    if not recs:
        reasons.append(("info", "Not yet bought from, so delivery is unverified. Absence of a grade is not a bad grade."))

    has_data = bool(probe or recs or lb)
    has_red = any(l == "red" for l, _ in reasons)
    has_yellow = any(l == "yellow" for l, _ in reasons)
    light = ("gray" if not has_data else "red" if has_red else "yellow" if has_yellow else "green")
    score = None
    if light != "gray":
        score = max(0, 100 - sum(45 if l == "red" else 20 if l == "yellow" else 0 for l, _ in reasons))
    return {"host": host, "light": light, "score": score,
            "reasons": [{"level": l, "text": t} for l, t in reasons],
            "checked_live": bool(probe), "receipts": len(recs), "disputes": len(shorts),
            "delivered": len(delivered), "accurate": len(accurate),
            "free_alternative": free_cat}
