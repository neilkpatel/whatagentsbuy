#!/usr/bin/env python3
"""What happened today, in one screen.

The other scripts each answer one narrow question. This just prints the day:
who took money, what arrived, what to look at. No flags, no options.

  python3 today.py
"""
import json, os, time

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


def load(name, default=None):
    p = os.path.join(DATA, name)
    return json.load(open(p)) if os.path.exists(p) else default


def money(n):
    return f"${n:,.2f}"


def main():
    lb = load("leaderboard.json")
    services = load("services.json", {})
    newest = load("newest.json", [])

    print()
    if lb:
        w = lb["windows"]["1d"]
        day = w["dates"][-1] if w["dates"] else "?"
        print(f"  MONEY  ({day}, last 24h on Base)")
        print(f"  {money(w['total_usdc'])} received across {w['total_settlements']:,} payments\n")
        for i, r in enumerate(w["rows"][:8], 1):
            desc = (services.get(r["host"]) or {}).get("desc") or ""
            desc = desc[:64] + ("..." if len(desc) > 64 else "")
            flag = "  <- recycles most of it" if r.get("circular") else ""
            print(f"  {i}. {r['service'][:26]:28s} {money(r['usdc']):>11s}  "
                  f"{r['settlements']:>7,} payments  {r['payers']:>4} wallets{flag}")
            if desc:
                print(f"     {desc}")
        print()

    if newest:
        latest_day = max(r["date"] for r in newest)
        todays = [r for r in newest if r["date"] == latest_day]
        eps = sum(len(r["endpoints"]) for r in todays)
        new_svc = [r for r in todays if r["new_service"]]
        print(f"  NEW  ({latest_day})")
        print(f"  {eps} endpoints arrived across {len(todays)} services, "
              f"{len(new_svc)} of them brand new\n")
        for r in sorted(new_svc, key=lambda r: -(r.get("calls30d") or 0))[:5]:
            ep = max(r["endpoints"], key=lambda e: (e.get("calls30d") or 0))
            d = (ep.get("desc") or "")[:70]
            used = f"  ({ep['calls30d']:,} paid calls)" if ep.get("calls30d") else ""
            print(f"  - {r['service'][:30]:32s}{used}")
            if d:
                print(f"    {d}")
        print()

    days = len([f for f in os.listdir(os.path.join(DATA, "history"))
                if f.startswith("settlements_")]) if os.path.isdir(os.path.join(DATA, "history")) else 0
    print(f"  tape: {days} day(s) collected. 7-day view needs 7.")
    print()


if __name__ == "__main__":
    main()
