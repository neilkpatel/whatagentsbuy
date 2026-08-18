#!/usr/bin/env python3
"""Classify every x402 endpoint into a category, and flag what is gradeable.

The corpus engine keys off this map. For each category it answers: how many
sellers exist, is there an objective PRIMARY source to grade accuracy against
(stocks->FMP, crypto->exchanges, weather->NWS, balance/gas->the chain, fx->ECB),
and how many declare an output schema so delivery can be field-checked. The
CATEGORIES table below is the single source of truth for the accuracy source per
category; the daily lab reads it too.

Run:  python3 categorize.py                     # coverage summary + write map
      python3 categorize.py --category stock-price   # list one category's sellers
"""
import argparse
import json
import os
from urllib.parse import urlparse

import conform

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# (category, accuracy_source_or_None, positive substrings, negative substrings).
# ORDERED: the first category whose positives hit (and negatives miss) wins, so
# the specific, objective-truth categories come first. accuracy_source is None
# for categories with no clean primary reference (delivery-check / archive only).
CATEGORIES = [
    ("stock-price", "FMP real-time quote",
     ("stock quote", "stock price", "equity quote", "us stock", "share price", "realtime price for"),
     ("filing", "edgar", "earnings", "insider", "screen", "news", "option", "calendar",
      "technical", "indicator", "13f", "congress", "form 4")),
    ("crypto-price", "Coinbase/Kraken median",
     ("btc price", "eth price", "crypto price", "token price", "coin price", "spot price",
      "price feed", "coingecko", "bitcoin price"),
     ("news", "chart", "perp", "forecast", "history", "convert", "swap", "market-cap", "tokenized")),
    ("fx-rate", "ECB reference rates",
     ("forex", "fx rate", "exchange rate", "currency conversion", "reference exchange rate",
      "fiat", "usd to", "eur/", "gbp"),
     ("stock", "crypto")),
    ("weather", "NWS / Open-Meteo",
     ("weather", "forecast", "temperature", "meteo", "precipitation"),
     ()),
    ("wallet-balance", "chain balanceOf",
     ("wallet balance", "token balance", "address balance", "erc20 balance", "balanceof", "holdings"),
     ("gas",)),
    ("gas-price", "chain base fee",
     ("gas price", "gas fee", "gwei", "base fee", "basefee", "gas oracle"),
     ()),
    # ---- delivery/archive only: no clean objective ground truth ----
    ("web-search", None, ("web search", "search the web", "exa search", "serp", "google search", "/search"), ()),
    ("enrichment", None, ("enrich", "people search", "company data", "pdl", "linkedin", "firmographic", "whitepages"), ()),
    ("llm-inference", None, ("llm", "completion", "inference", "chat model", "generate text", "prompt", "gpt-"), ()),
    ("image-video-gen", None, ("image", "text-to-image", "generate an ai image", "video", "render", "audio", "tts", "voice"), ()),
    ("sec-filings", None, ("sec ", "edgar", "10-k", "10-q", "8-k", "filing", "cik"), ()),
    ("news-sentiment", None, ("news", "sentiment", "headline"), ()),
    ("social-data", None, ("twitter", "tweet", "tiktok", "instagram", "reddit", "social media", "youtube"), ()),
    ("prediction-markets", None, ("polymarket", "kalshi", "prediction market"), ()),
    ("onchain-data", None, ("on-chain", "onchain", "block height", "transaction", "chainlink", "rpc", "swap quote"), ()),
    ("whois-domain", None, ("whois", "domain lookup", "dns"), ()),
    ("novelty", None, ("dice", "roast", "horoscope", "fortune", "blessing", "fun fact", "joke", "compliment"), ()),
]


def categorize(url, desc, tags):
    blob = (str(url) + " " + str(desc) + " " + str(tags)).lower()
    for cat, src, pos, neg in CATEGORIES:
        if any(p in blob for p in pos) and not any(n in blob for n in neg):
            return cat, src
    return "other", None


def build_map():
    reg = json.load(open(os.path.join(DATA, "cdp_resources_raw.json")))
    by_cat = {}
    seen = set()                                   # one endpoint per (host, category)
    for r in reg:
        url = r.get("resource") or ""
        host = (urlparse(url).hostname or "").lower()
        if not (host and url.startswith("http")):
            continue
        cat, src = categorize(url, r.get("description") or "", r.get("tags") or "")
        key = (host, cat)
        if key in seen:
            continue
        p = conform.price_of(r)
        c = conform.contract(r)
        seen.add(key)
        by_cat.setdefault(cat, {"accuracy_source": src, "sellers": []})
        by_cat[cat]["sellers"].append({
            "host": host, "url": url, "price_usdc": p,
            "has_schema": bool(c), "description": (r.get("description") or "")[:80]})
    return by_cat


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default=None)
    a = ap.parse_args()
    m = build_map()

    if a.category:
        d = m.get(a.category)
        if not d:
            print(f"no category {a.category!r}. Known: {', '.join(sorted(m))}")
            return
        print(f"{a.category}  (accuracy source: {d['accuracy_source'] or 'none'})")
        for s in sorted(d["sellers"], key=lambda s: (s["price_usdc"] or 9)):
            print(f"  ${s['price_usdc'] or 0:.4f} schema={'Y' if s['has_schema'] else 'n'}  "
                  f"{s['host']:<40} {s['description']}")
        return

    # coverage summary
    rows = []
    for cat, d in m.items():
        sellers = d["sellers"]
        priced = [s for s in sellers if s["price_usdc"]]
        with_schema = sum(1 for s in sellers if s["has_schema"])
        cost = sum(min(s["price_usdc"] or 0, 0.02) for s in priced)   # one cheap-ish call each
        rows.append((cat, d["accuracy_source"], len(sellers), with_schema, cost))
    rows.sort(key=lambda r: (r[1] is None, -r[2]))                    # accuracy cats first, by size

    print(f"{'category':<20} {'acc?':<4} {'sellers':>7} {'schema':>6} {'~$/full sweep':>14}  primary source")
    print("-" * 92)
    acc_hosts = acc_cost = 0
    for cat, src, n, schema, cost in rows:
        acc = "YES" if src else " - "
        if src:
            acc_hosts += n
            acc_cost += cost
        print(f"{cat:<20} {acc:<4} {n:>7} {schema:>6} {cost:>13.3f}   {src or ''}")
    total = sum(r[2] for r in rows)
    print("-" * 92)
    print(f"{'TOTAL':<20} {'':<4} {total:>7} {sum(r[3] for r in rows):>6}")
    print(f"\naccuracy-gradeable categories: {sum(1 for r in rows if r[1])}, "
          f"{acc_hosts} sellers, ~${acc_cost:.2f} to sweep all once")
    print(f"a full daily sweep of EVERY priced endpoint once: "
          f"~${sum(r[4] for r in rows):.2f}  (well under a $5/day budget)")

    out = os.path.join(DATA, "categories_map.json")
    json.dump({"generated": __import__("time").strftime("%Y-%m-%d %H:%M:%S"), "categories": m},
              open(out, "w"), indent=1)
    print(f"wrote {os.path.relpath(out, HERE)}")


if __name__ == "__main__":
    main()
