#!/usr/bin/env python3
"""Render the /x402 share card: a picture of the dashboard's two hero tiles.

Why the design is what it is. ~88% of this site's visitors arrive with no
referrer, so links spread through DMs, Slack, Discord and X, where the only
thing selling a click is the unfurled preview. The first version put two figures
on the left, left the right half empty, and drew a row of disconnected blocks
that did not read as a chart. In a timeline that looks like a placeholder.

So the card now reproduces the two panels the page leads with: bordered,
labelled, each with its own bar chart carrying value labels and day labels. It
fills the frame, it is dense enough to be worth stopping for, and it is
recognisable as the page it opens.

Palette is the dashboard's light theme value for value, so the card reads as a
screenshot of the destination and sits correctly on the white surround that X
and LinkedIn put behind it.

Regenerated on every build, so the card is never staler than the page.

  python3 og_x402.py                 # uses data/dashboard_snapshot.json
  python3 og_x402.py --out /tmp/c.png
"""
import argparse, json, os, sys
from datetime import datetime, timezone
# PIL is imported lazily inside the render functions, NOT here: tests.py imports
# this module for its pure helpers (fmt_usd, fmt_n), and CI is dependency-free by
# design. A top-level `from PIL import ...` made the entire test suite crash with
# ModuleNotFoundError before a single test ran, on every push. Only real rendering
# needs Pillow, and the machine that renders (build.py) has it.

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
W, H = 1200, 630

BG = (238, 241, 246)         # --bg, the cool grey the cards sit on
CARD = (255, 255, 255)       # --card
HEADBAR = (249, 250, 252)    # card header bar
INK = (16, 24, 40)           # --ink
INK2 = (102, 112, 133)       # --muted
LINE = (223, 228, 236)       # --line
VOL = (37, 99, 235)          # --series-vol
TX = (4, 120, 87)            # --series-tx
GREEN = (4, 120, 87)
RED = (220, 38, 38)

FONTS = {True: "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
         False: "/System/Library/Fonts/Supplemental/Arial.ttf"}


def font(size, bold=True):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(FONTS[bold], size)
    except Exception:
        return ImageFont.load_default()


def fmt_usd(v):
    if v is None:
        return "-"
    if v >= 1_000_000:
        return f"${v/1_000_000:,.2f}M"
    if v >= 10_000:
        return f"${round(v/1000):,}K"
    return f"${v:,.0f}"


def fmt_n(v):
    if v is None:
        return "-"
    if v >= 1_000_000:
        return f"{v/1_000_000:,.2f}M"
    if v >= 10_000:
        return f"{round(v/1000):,}K"
    return f"{v:,.0f}"


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def panel(d, x, y, w, h, label, value, delta, series, key, color, fmt):
    """One dashboard tile: label, big number, delta, bar chart.

    The last bar is the running UTC day and is drawn lighter, exactly as the
    page draws it. A partial day at full strength reads as a completed day and
    overstates the latest number all morning.
    """
    # Card header bar, ruled off from the figure, same as the page.
    d.rounded_rectangle([x, y, x + w, y + h], radius=14, fill=CARD,
                        outline=LINE, width=2)
    d.rounded_rectangle([x + 1, y + 1, x + w - 1, y + 47], radius=13, fill=HEADBAR)
    d.rectangle([x + 1, y + 34, x + w - 1, y + 47], fill=HEADBAR)
    d.line([x + 1, y + 47, x + w - 1, y + 47], fill=LINE, width=2)
    px = x + 24
    d.text((px, y + 17), label, font=font(18), fill=INK2)
    d.text((px, y + 66), value, font=font(58), fill=INK)

    if delta is not None:
        up = delta >= 0
        d.text((px, y + 138), f"{'▲' if up else '▼'} {abs(delta):.0f}% vs last complete day",
               font=font(19, bold=False), fill=GREEN if up else RED)

    if not series:
        return
    top = max(s[key] for s in series) or 1
    bx, by, bh = px, y + 182, h - 182 - 56
    bw = w - 48
    n = len(series)
    gap = 9
    barw = max(8.0, (bw - (n - 1) * gap) / n)
    peak = max(range(n), key=lambda i: series[i][key])
    for i, s in enumerate(series):
        cx = bx + i * (barw + gap)
        rh = max(3.0, (s[key] / top) * bh)
        fill = _mix(color, CARD, 0.55) if s.get("partial") else color
        d.rounded_rectangle([cx, by + bh - rh, cx + barw, by + bh], radius=4, fill=fill)
        # Value above each bar, bolder on the peak, mirroring the page.
        lab = fmt(s[key])
        f = font(15, bold=(i == peak))
        lw = d.textlength(lab, font=f)
        d.text((cx + barw / 2 - lw / 2, by + bh - rh - 21), lab,
               font=f, fill=INK if i == peak else INK2)
        # Day label beneath, so it reads as seven days and not as decoration.
        try:
            dt = datetime.strptime(s["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            day = "now" if s.get("partial") else dt.strftime("%a")
            dnum = dt.strftime("%-m/%-d")
        except Exception:
            day, dnum = "", ""
        for j, t in enumerate((day, dnum)):
            f2 = font(14, bold=False)
            tw = d.textlength(t, font=f2)
            d.text((cx + barw / 2 - tw / 2, by + bh + 8 + j * 16), t, font=f2, fill=INK2)


def render(snap, out):
    if not snap or snap.get("vol24") is None:
        print("og_x402: no snapshot, card not regenerated")
        return False

    from PIL import Image, ImageDraw
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    d.text((48, 34), "x402 Agentic Payments Dashboard", font=font(38), fill=INK)
    d.text((48, 84), "Live volume and transactions across Base and Solana, read straight from chain",
           font=font(21, bold=False), fill=INK2)

    # Seven closed days plus the running one, which the page marks lighter.
    series = list(snap.get("daily7") or [])
    if snap.get("today"):
        series = series + [dict(snap["today"], partial=True)]

    py, ph = 126, 396
    pw = (W - 48 * 2 - 26) / 2
    panel(d, 48, py, pw, ph, "X402 VOLUME · 24H", fmt_usd(snap["vol24"]),
          snap.get("vol24DeltaPct"), series, "vol", VOL, fmt_usd)
    panel(d, 48 + pw + 26, py, pw, ph, "X402 TRANSACTIONS · 24H", fmt_n(snap.get("tx24")),
          snap.get("tx24DeltaPct"), series, "tx", TX, fmt_n)

    d.text((48, 556), "whatagentsbuy.com/x402", font=font(25), fill=INK)
    last = series[-1]["date"] if series else ""
    stamp = f"UTC days  ·  updated {last}"
    f = font(20, bold=False)
    d.text((W - 48 - d.textlength(stamp, font=f), 560), stamp, font=f, fill=INK2)

    im.save(out, "PNG", optimize=True)
    print(f"og_x402: wrote {out} ({fmt_usd(snap['vol24'])} / {fmt_n(snap.get('tx24'))} txs)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "public", "og-x402.png"))
    ap.add_argument("--snapshot", default=os.path.join(DATA, "dashboard_snapshot.json"))
    a = ap.parse_args()
    if not os.path.exists(a.snapshot):
        print("og_x402: no dashboard snapshot; skipping")
        return 0
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    render(json.load(open(a.snapshot)), a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
