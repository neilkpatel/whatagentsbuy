#!/usr/bin/env python3
"""The daily brief: what arrived, and the few worth looking at.

Drafts a post from the day's registry diff. It does not publish on its own,
because "interesting" is a judgement and the picks deserve a human read before
they go out. Review the draft, edit the file if you like, then publish it.

  python3 daily.py                 # draft today and print it
  python3 daily.py --publish       # move today's draft into the feed
  python3 daily.py --date 2026-08-06
"""
import argparse, datetime, json, os, re, time
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DRAFTS = os.path.join(DATA, "drafts")
ET = ZoneInfo("America/New_York")

# Things that make an arrival worth a reader's attention. Deliberately biased
# toward the strange and the already-used, because a competent-but-ordinary API
# is not news.
CURIOUS = re.compile(
    r"\b(game|play|bet|wager|predict|meme|joke|fortune|horoscope|tarot|dream|"
    r"human|humanity|proof of|identity|reputation|trust|scam|fraud|honeypot|"
    r"phone|call|sms|voice|email|letter|mail|ship|deliver|physical|print|"
    r"gift ?card|top-?up|prepaid|payroll|invoice|escrow|lend|loan|insur|"
    r"weather|space|satellite|flight|traffic|court|legal|patent|lobby|census|"
    r"agent|autonomous|swarm|simulat)", re.I)


def score(row):
    """Rank an arrival by how much a reader would care."""
    s = 0.0
    why = []
    if row.get("new_service"):
        s += 3
        why.append("new service, not another endpoint on a known one")
    calls = row.get("calls30d") or 0
    if calls:
        s += 2 + min(calls / 100, 3)
        why.append(f"already taking paid calls ({calls:,} in 30d)")
    descs = " ".join((e.get("desc") or "") for e in row.get("endpoints", []))
    hits = set(m.group(0).lower() for m in CURIOUS.finditer(descs))
    if hits:
        s += 1.5 * min(len(hits), 3)
        why.append("sells something unusual: " + ", ".join(sorted(hits)[:4]))
    n = len(row.get("endpoints", []))
    if n >= 5:
        s += 1
        why.append(f"arrived with {n} endpoints at once")
    if not descs.strip():
        s -= 2
        why.append("no description published")
    return s, why


def pick(rows, k=3):
    scored = sorted(((score(r)[0], r) for r in rows), key=lambda t: -t[0])
    return [r for _, r in scored[:k]]


def draft(date):
    newest = json.load(open(os.path.join(DATA, "newest.json")))
    todays = [r for r in newest if r["date"] == date]
    if not todays:
        return None, f"no arrivals recorded for {date}"

    total_eps = sum(len(r["endpoints"]) for r in todays)
    new_svcs = sum(1 for r in todays if r["new_service"])
    picks = pick(todays, 3)

    def tidy(text, limit=150):
        t = re.sub(r"\s+", " ", (text or "")).strip()
        t = re.split(r"\s*[?&][A-Za-z_]+=", t)[0].strip()   # drop query-param examples
        if len(t) > limit:
            cut = t[:limit]
            end = max(cut.rfind(". "), cut.rfind("; "), cut.rfind(" — "))
            t = (cut[:end] if end > 60 else cut.rsplit(" ", 1)[0]) + "..."
        return t.rstrip(" .;—-")

    bullets = []
    for r in picks:
        ep = max(r["endpoints"], key=lambda e: (e.get("calls30d") or 0))
        d = tidy(ep.get("desc"))
        name = r["service"]
        label = name if name.lower() == r["host"].lower() else f"{name} ({r['host']})"
        used = f" Already taking money: {ep['calls30d']:,} paid calls." if ep.get("calls30d") else ""
        bullets.append(f"{label} — {d or 'no description published'}.{used}")

    prev_label = ""
    try:
        idx = sorted(f for f in os.listdir(os.path.join(DATA, "index")) if f.startswith("index_"))
        prior = [f[6:16] for f in idx if f[6:16] < date]
        if prior:
            prev_label = prior[-1]
    except Exception:
        pass
    window = f"since {prev_label}" if prev_label else "today"

    post = {
        "id": f"new-{date}",
        "ts": datetime.datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        "ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tags": ["daily", "new-endpoints"],
        "title": (f"{total_eps} endpoints appeared {window}, {new_svcs} from services that did not exist"
                  if new_svcs else f"{total_eps} endpoints appeared {window}"),
        "lede": (f"{total_eps} new paid endpoints landed in the public registry {window}, "
                 f"across {len(todays)} services. Three worth a look."),
        "bullets": bullets[:3],
        "receipt": (f"Registry diff for {date} against the {prev_label or 'previous'} snapshot, Coinbase CDP discovery API. "
                    f"Descriptions are the seller's own words. Nothing here has been bought or verified yet."),
    }
    return post, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--publish", action="store_true")
    a = ap.parse_args()

    os.makedirs(DRAFTS, exist_ok=True)
    path = os.path.join(DRAFTS, f"daily_{a.date}.json")

    if a.publish:
        if not os.path.exists(path):
            raise SystemExit(f"no draft at {path}; run without --publish first")
        post = json.load(open(path))
        fp = os.path.join(DATA, "feed.json")
        feed = json.load(open(fp))
        if any(x["id"] == post["id"] for x in feed):
            raise SystemExit(f"{post['id']} is already in the feed")
        feed.insert(0, post)
        json.dump(feed, open(fp, "w"), indent=1)
        print(f"published {post['id']}: {post['title']}")
        return

    post, err = draft(a.date)
    if err:
        raise SystemExit(err)
    json.dump(post, open(path, "w"), indent=2)

    print(f"draft saved to {os.path.relpath(path, HERE)}\n")
    print(post["title"])
    print("-" * len(post["title"]))
    print(post["lede"] + "\n")
    for b in post["bullets"]:
        print("  * " + b)
    print(f"\n{post['receipt']}")
    print(f"\nedit the file if you want, then: python3 daily.py --date {a.date} --publish")


if __name__ == "__main__":
    main()
