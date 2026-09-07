#!/usr/bin/env python3
"""Publish a post to the Touchstone feed.

The workflow this exists for: Neil drops a thought (or a link plus what he
learned from it) in chat; Claude shapes it in Neil's voice, runs this, and it's
live in under a minute.

  python3 post.py --text "..." [--tags a,b] [--link URL] [--link-label "..."]
                  [--receipt "..."] [--id slug] [--no-deploy]
"""
import argparse, datetime, json, os, re, subprocess, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

HERE = os.path.dirname(os.path.abspath(__file__))
FEED = os.path.join(HERE, "data", "feed.json")


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return "-".join(s.split("-")[:5]) or f"post-{int(time.time())}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text")
    ap.add_argument("--text-file", help="read body from a file; use this to avoid the shell eating $ signs")
    ap.add_argument("--tags", default="")
    ap.add_argument("--link")
    ap.add_argument("--link-label")
    ap.add_argument("--receipt")
    ap.add_argument("--id")
    ap.add_argument("--image", help="image src, e.g. /img/thing.jpg (put the file in public/img/)")
    ap.add_argument("--image-alt", default="")
    ap.add_argument("--image-caption")
    ap.add_argument("--stats", help='receipt card, "Key=Value" pairs separated by |. '
                                    'Append >>URL to any value to link it. '
                                    'e.g. "Paid=$0.01|Tx=0xabc…>>https://basescan.org/tx/0xabc"')
    ap.add_argument("--no-deploy", action="store_true")
    a = ap.parse_args()
    if a.text_file:
        a.text = open(a.text_file).read().strip()
    if not a.text:
        ap.error("need --text or --text-file")
    if "$" in a.text and re.search(r"\$\d", a.text) is None and "," in a.text:
        print("warning: body has a bare $ with no digits after it; the shell may have eaten a number")

    feed = json.load(open(FEED))
    pid = a.id or slugify(a.text)
    if any(p["id"] == pid for p in feed):
        pid = f"{pid}-{int(time.time()) % 10000}"

    post = {
        "id": pid,
        # Stamp real Eastern time regardless of where the laptop is. Using
        # machine-local time with a hardcoded "ET" label silently published
        # Pacific timestamps as Eastern.
        "ts": datetime.datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
        "ts_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tags": [t.strip() for t in a.tags.split(",") if t.strip()],
        "text": a.text,
        "receipt": a.receipt,
    }
    if a.link:
        post["link"] = {"url": a.link, "label": a.link_label}
    if a.image:
        post["image"] = {"src": a.image, "alt": a.image_alt}
        if a.image_caption:
            post["image"]["caption"] = a.image_caption
    if a.stats:
        cells = []
        for pair in a.stats.split("|"):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            cell = {"k": k.strip(), "v": v.strip()}
            if ">>" in cell["v"]:                      # Value>>URL makes it a link
                cell["v"], cell["href"] = (x.strip() for x in cell["v"].split(">>", 1))
            cells.append(cell)
        if cells:
            post["stats"] = cells

    feed.insert(0, post)
    json.dump(feed, open(FEED, "w"), indent=1)
    print(f"post added: #{pid}")

    subprocess.run(["python3", os.path.join(HERE, "build.py")], check=True)
    if not a.no_deploy:
        r = subprocess.run(["vercel", "--prod", "--yes"], cwd=HERE,
                           capture_output=True, text=True, timeout=600)
        ok = "Production:" in (r.stdout + r.stderr)
        print("deployed" if ok else f"deploy issue:\n{(r.stdout + r.stderr)[-400:]}")
        if ok:
            print(f"live: https://touchstone.neilkpatel.com/#{pid}")


if __name__ == "__main__":
    main()
