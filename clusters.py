#!/usr/bin/env python3
"""Find head-to-head candidates: topics where several sellers answer the same
question, ranked by the money actually settling in that topic rather than by how
many listings exist. A category with 20 sellers and no demand is a directory
problem; a category with 6 sellers and real spend is a buyer's question worth
answering.

    python3 clusters.py            # ranked table
    python3 clusters.py --topic whois   # every seller in one topic, with prices
"""
import argparse
import collections
import json
import re
import statistics
import sys
from urllib.parse import urlparse

REG = "data/cdp_resources_raw.json"
LB = "data/leaderboard.json"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

# Ordered: the first pattern that matches wins, so put the specific before the
# general. Written against the descriptions actually in the registry.
TOPICS = [
    ("email validation",  r"\bemail\b.*(valid|verif|check|deliverab)|(valid|verif|check).*\bemail\b|mx record"),
    ("whois / domain",    r"\bwhois\b|domain (age|info|lookup|record|registra)|\bdns\b.*lookup|nameserver"),
    ("gas price",         r"\bgas\b.*(price|fee|estimat|oracle)|(price|fee|estimat).*\bgas\b|gwei"),
    ("token price",       r"\b(token|coin|crypto|asset)\b.*\bprice\b|price.*\b(token|coin|ticker)\b|ohlc|candle"),
    ("stock / equity",    r"\bstock\b|\bequit(y|ies)\b|\bticker\b.*\b(quote|price)\b|earnings|sec filing"),
    ("wallet balance",    r"wallet.*(balance|holding|portfolio)|(balance|holding|portfolio).*wallet|erc20 balance"),
    ("web search",        r"\bweb search\b|\bsearch the web\b|\bserp\b|google search|\bexa\b|search engine"),
    ("web scrape",        r"scrape|crawl|extract.*(page|html|url)|markdown.*url|url.*markdown|firecrawl"),
    ("social data",       r"\btwitter\b|\bx\.com\b|instagram|tiktok|reddit|youtube|linkedin|social"),
    ("news",              r"\bnews\b|headline|press release|rss"),
    ("weather",           r"weather|forecast|temperature|precipitation"),
    ("geocode / maps",    r"geocod|\bmaps?\b|lat.*lon|address.*coordinat|places api|reverse geo"),
    ("image generation",  r"generat.*image|image.*generat|text.to.image|\bdall|stable diffusion|flux"),
    ("text / llm",        r"\bllm\b|completion|chat model|summariz|translat|gpt|claude|inference"),
    # \bvoice\b matters: without the boundary this matched "invoice" and filed
    # every gift card seller under speech.
    ("speech / audio",    r"text.to.speech|\btts\b|transcri|whisper|\bvoice\b|\baudio"),
    ("gift cards / topup", r"gift ?card|\besim\b|mobile top.?up|prepaid|redemption code"),
    ("sanctions / aml",   r"\bofac\b|sanction|\baml\b|\bkyc\b|screening|watchlist|compliance"),
    ("reputation / trust", r"reputation|trust ?score|risk score|credibility|attest"),
    ("settlement proof",  r"settlement|payment.*(verif|proof|receipt)|(verif|proof).*payment|tx.*verif"),
    ("onchain data",      r"\bblock\b|\bchain\b|transaction|\bevm\b|\brpc\b|\bnft\b|\bdefi\b|\btvl\b"),
    ("identity / people", r"\bperson\b|people search|enrich|contact.*(find|lookup)|phone.*lookup"),
    ("random / novelty",  r"random|\bjoke\b|\bmeme\b|\bquote\b|fortune|dice|coin flip|trivia"),
]


def topic_of(text):
    t = text.lower()
    for name, pat in TOPICS:
        if re.search(pat, t):
            return name
    return None


def host_of(url):
    try:
        return (urlparse(url).hostname or "").lower().lstrip(".")
    except Exception:
        return ""


def price_of(res):
    """Cheapest USDC price this resource quotes, in dollars."""
    best = None
    for a in res.get("accepts") or []:
        if (a.get("asset") or "").lower() != USDC:
            continue
        try:
            v = int(a["amount"]) / 1e6
        except Exception:
            continue
        best = v if best is None else min(best, v)
    return best


def load():
    reg = json.load(open(REG))
    try:
        lb = json.load(open(LB))
    except FileNotFoundError:
        lb = None
    money, demand = {}, {}
    if lb:
        rows = lb["windows"].get("30d", lb["windows"]["1d"])["rows"]
        for r in rows:
            h = (r.get("host") or "").lower()
            if h:
                money[h] = money.get(h, 0.0) + (r.get("usdc") or 0.0)
                demand[h] = {"label": r.get("demand"), "ods": r.get("organic_score"),
                             "payers": r.get("payers") or 0,
                             "circular": bool(r.get("circular"))}
    return reg, money, demand


def build():
    """A host is counted once. Revenue is attributed to the host's PRIMARY topic
    (where most of its endpoints sit), because many hosts sell several things and
    adding their revenue into every topic they touch inflates all of them toward
    the same number. `sellers` still counts every host with an endpoint in the
    topic, since that is what a head-to-head would actually compare."""
    reg, money, demand = load()
    seen = collections.defaultdict(lambda: collections.defaultdict(list))
    for res in reg:
        url = res.get("resource") or ""
        h = host_of(url)
        if not h:
            continue
        t = topic_of((res.get("description") or "") + " " + url)
        if not t:
            continue
        p = price_of(res)
        if p is None:
            continue
        seen[t][h].append((p, url, res.get("description") or ""))

    # primary topic per host: most endpoints, ties to the rarer topic
    by_host = collections.defaultdict(dict)
    for t, hosts in seen.items():
        for h, v in hosts.items():
            by_host[h][t] = len(v)
    breadth = {t: len(hosts) for t, hosts in seen.items()}
    primary = {h: max(ts, key=lambda t: (ts[t], -breadth[t]))
               for h, ts in by_host.items()}

    out = []
    for t, hosts in seen.items():
        # per-host cheapest offer in this topic, which is what a buyer compares
        prices = sorted(p for p in (min(v)[0] for v in hosts.values() if v) if p > 0)
        if not prices:
            continue
        mine = [h for h in hosts if primary.get(h) == t]
        rev = sum(money.get(h, 0.0) for h in mine)
        earners = [h for h in mine if money.get(h, 0.0) > 0]
        # Demand shape across the whole topic. A head-to-head is worth writing
        # where several sellers each have independent buyers, not where one
        # wallet is funding the whole category.
        dd = [demand[h] for h in mine if h in demand]
        buyers = sum(d["payers"] for d in dd)
        real = [d for d in dd if (d["ods"] or 0) >= 60 and not d["circular"]]
        odss = sorted(d["ods"] for d in dd if d["ods"] is not None)
        lo = prices[max(0, int(len(prices) * 0.10) - 1)] if len(prices) >= 10 else prices[0]
        hi = prices[min(len(prices) - 1, int(len(prices) * 0.90))] if len(prices) >= 10 else prices[-1]
        out.append({
            "topic": t,
            "sellers": len(hosts),
            "primary_sellers": len(mine),
            "endpoints": sum(len(v) for v in hosts.values()),
            "cheapest": prices[0],
            "dearest": prices[-1],
            "p10": lo,
            "p90": hi,
            "spread": (hi / lo) if lo else None,
            "median": statistics.median(prices),
            "usdc_30d": rev,
            "earning_sellers": len(earners),
            "buyers": buyers,
            "real_sellers": len(real),
            "median_ods": statistics.median(odss) if odss else None,
            "hosts": sorted(hosts, key=lambda h: -money.get(h, 0.0)),
            "demand": {h: demand.get(h) for h in earners},
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic")
    ap.add_argument("--min-sellers", type=int, default=3)
    a = ap.parse_args()
    rows = build()

    if a.topic:
        r = next((x for x in rows if x["topic"].startswith(a.topic.lower())), None)
        if not r:
            print("no such topic. try one of:")
            for x in sorted(rows, key=lambda x: x["topic"]):
                print("   ", x["topic"])
            return 1
        reg, money, demand = load()
        print(f'\n{r["topic"]}: {r["sellers"]} sellers, {r["endpoints"]} endpoints\n')
        print(f'  {"cheapest":>9}  {"seller":<38} {"30d USDC":>11}  demand')
        print("  " + "-" * 78)
        for h in r["hosts"]:
            ps = [price_of(x) for x in reg
                  if host_of(x.get("resource") or "") == h
                  and topic_of((x.get("description") or "") + " " + (x.get("resource") or "")) == r["topic"]]
            ps = [p for p in ps if p]
            if not ps:
                continue
            print(f'  ${min(ps):>8.4f}  {h:<38} {money.get(h,0):>11,.2f}  {demand.get(h) or ""}')
        return 0

    rows = [r for r in rows if r["sellers"] >= a.min_sellers]
    # Rank by buyers, not by dollars. Revenue ranks whichever category one large
    # wallet happens to fund; buyers rank what a market actually wants.
    rows.sort(key=lambda r: (-r["real_sellers"], -r["buyers"], -r["usdc_30d"]))
    print(f'\n{"topic":<21}{"sellers":>8}{"paid":>6}{"real":>6}{"buyers":>8}'
          f'{"30d USDC":>12}{"median":>9}{"spread":>8}')
    print("-" * 80)
    for r in rows:
        sp = f'{r["spread"]:.0f}x' if r["spread"] and r["spread"] >= 1.5 else "flat"
        print(f'{r["topic"]:<21}{r["sellers"]:>8}{r["earning_sellers"]:>6}'
              f'{r["real_sellers"]:>6}{r["buyers"]:>8}{r["usdc_30d"]:>12,.0f}'
              f'{r["median"]:>9.4f}{sp:>8}')
    print("\nsellers = hosts with at least one endpoint in the topic")
    print("paid    = hosts whose PRIMARY topic is this one and that took USDC in 30d")
    print("real    = of those, how many score 60+ on Organic Demand and are not circular")
    print("buyers  = distinct wallets that paid those hosts")
    print("spread  = 10th to 90th percentile of each seller's cheapest offer")
    return 0


if __name__ == "__main__":
    sys.exit(main())
