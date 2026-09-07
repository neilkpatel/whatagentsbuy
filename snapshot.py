#!/usr/bin/env python3
"""Record a Vercel Analytics reading, because Vercel has no API.

Vercel Web Analytics is dashboard-only: every documented REST path 404s and
`vercel logs` only streams runtime logs, which a static site never produces.
So the numbers get read from the dashboard by hand and appended here. That
turns a rolling 7-day window, which hides daily movement, into a real series.

  python3 snapshot.py --visitors 115 --views 208 --bounce 79 \
      --daily 2026-08-10=18,2026-08-11=7 \
      --pages /=100,/p/cloudflare-wallets=13 \
      --referrers t.co=12,chatgpt.com=1 \
      --note "www redirect shipped"

  python3 snapshot.py --show
"""
import argparse, json, os, datetime, glob

HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(HERE, "data", "analytics")


def pairs(s):
    """'a=1,b=2' -> {'a': 1, 'b': 2}. Values stay ints where they parse."""
    out = {}
    for part in (s or "").split(","):
        if "=" not in part:
            continue
        k, v = part.rsplit("=", 1)
        try:
            out[k.strip()] = int(v)
        except ValueError:
            out[k.strip()] = v.strip()
    return out


def show():
    files = sorted(glob.glob(os.path.join(DIR, "*.json")))
    if not files:
        print("  no snapshots yet")
        return
    snaps = [json.load(open(f)) for f in files]
    print(f"\n  {'date':12s} {'visitors':>9s} {'views':>7s} {'bounce':>7s}   window")
    print("  " + "-" * 56)
    prev = None
    for s in snaps:
        d = f'{s["visitors"]-prev:+d}' if prev is not None else "     "
        print(f'  {s["date"]:12s} {s["visitors"]:>9,} {s["views"]:>7,} '
              f'{str(s.get("bounce_pct","-"))+"%":>7s}   7d rolling  {d}')
        prev = s["visitors"]
    # the daily series is the part the rolling window hides
    daily = {}
    for s in snaps:
        daily.update(s.get("daily_visitors") or {})
    if daily:
        print(f"\n  daily visitors (as read off the chart)")
        for d in sorted(daily):
            n = daily[d]
            print(f'  {d}  {n:>4}  {"#" * min(n, 40)}')
    last = snaps[-1]
    if last.get("referrers"):
        known = sum(last["referrers"].values())
        print(f'\n  attribution on {last["date"]}: {known} of {last["visitors"]} '
              f'({known/last["visitors"]*100:.0f}%) had a referrer')
        for k, v in sorted(last["referrers"].items(), key=lambda x: -x[1]):
            print(f'    {v:>4}  {k}')
        print(f'    {last["visitors"]-known:>4}  (none — direct, or a link opened '
              f'in an app that strips it)')


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--show", action="store_true")
    a.add_argument("--date")
    a.add_argument("--visitors", type=int)
    a.add_argument("--views", type=int)
    a.add_argument("--bounce", type=int)
    a.add_argument("--daily", default="")
    a.add_argument("--pages", default="")
    a.add_argument("--referrers", default="")
    a.add_argument("--countries", default="")
    a.add_argument("--devices", default="")
    a.add_argument("--note", default="")
    args = a.parse_args()
    if args.show or args.visitors is None:
        show()
        return
    os.makedirs(DIR, exist_ok=True)
    date = args.date or datetime.date.today().isoformat()
    snap = {"date": date, "source": "Vercel Web Analytics dashboard, read manually",
            "window": "last 7 days", "visitors": args.visitors, "views": args.views,
            "bounce_pct": args.bounce, "daily_visitors": pairs(args.daily),
            "pages": pairs(args.pages), "referrers": pairs(args.referrers),
            "countries": pairs(args.countries), "devices": pairs(args.devices),
            "note": args.note,
            "recorded": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    json.dump(snap, open(os.path.join(DIR, f"{date}.json"), "w"), indent=1)
    print(f"recorded {date}: {args.visitors} visitors, {args.views} views")
    show()


if __name__ == "__main__":
    main()
