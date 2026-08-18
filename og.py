#!/usr/bin/env python3
"""Render the share card from live data, so it cannot go stale.

The old og.png was hand-made, dark on a light site, and still showed a grade that
had changed days earlier. This builds a 1200x630 card from data/feed.json using the
site's own stamp, then screenshots it with headless Chrome.

  python3 og.py          # writes public/og.png
"""
import json, os, subprocess, collections

HERE = os.path.dirname(os.path.abspath(__file__))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ORDER = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"]


def pick(feed):
    """Best, worst, and one in between: the range is the message."""
    graded = [p for p in feed if p.get("grade") in ORDER]
    graded.sort(key=lambda p: ORDER.index(p["grade"]))
    if len(graded) < 3:
        return graded
    best, worst = graded[0], graded[-1]
    mid = graded[len(graded) // 2]
    return [best, mid, worst]


def tone(g):
    return {"A": "#15803d", "B": "#2f7d3f", "C": "#b45309", "D": "#c2410c", "F": "#b42318"}[g[0]]


def main():
    feed = json.load(open(os.path.join(HERE, "data", "feed.json")))
    lb_path = os.path.join(HERE, "data", "leaderboard.json")
    lb = json.load(open(lb_path)) if os.path.exists(lb_path) else None
    picks = pick(feed)
    n_graded = len([p for p in feed if p.get("grade")])
    n_svc = len({(p.get("api") or {}).get("name") for p in feed if p.get("grade")})

    stamps = "".join(f'''
      <div class="col">
        <div class="stamp" style="color:{tone(p["grade"])}">
          <span class="t">GRADE</span><span class="l">{p["grade"]}</span><span class="b">VERIFIED</span>
        </div>
        <div class="svc">{(p.get("api") or {}).get("name","")}</div>
        <div class="on">{p.get("graded","")}</div>
      </div>''' for p in picks)

    stat = ""
    if lb:
        w = lb["windows"]["1d"]
        stat = (f'<b>${w["total_usdc"]:,.0f}</b> settled on Base in 24h &nbsp;·&nbsp; '
                f'<b>{n_graded}</b> endpoints bought and graded')

    html = f'''<meta charset="utf-8"><style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:1200px;height:630px;background:#fbfbfc;color:#15171a;
 font:400 20px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
 display:flex;flex-direction:column;justify-content:space-between;padding:58px 64px;
 -webkit-font-smoothing:antialiased}}
h1{{font-size:62px;letter-spacing:-.035em;font-weight:800}}
h1 .dot{{color:#1d5fd0}}
.sub{{font-size:26px;color:#5c6470;margin-top:14px;max-width:22em;line-height:1.35}}
.row{{display:flex;gap:74px;justify-content:center;align-items:flex-start}}
.col{{display:flex;flex-direction:column;align-items:center;width:280px}}
.stamp{{width:150px;height:150px;border-radius:50%;border:5px double currentColor;
 display:flex;flex-direction:column;align-items:center;justify-content:center;
 transform:rotate(-9deg);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
.stamp .t{{font-size:13px;letter-spacing:.16em}}
.stamp .l{{font-size:58px;font-weight:800;line-height:1;margin:4px 0}}
.stamp .b{{font-size:11px;letter-spacing:.13em}}
.svc{{margin-top:20px;font-size:20px;font-weight:680;font-family:ui-monospace,Menlo,monospace;
 text-align:center;word-break:break-all;line-height:1.25}}
.on{{font-size:14px;color:#5c6470;text-transform:uppercase;letter-spacing:.07em;margin-top:5px;text-align:center}}
.foot{{display:flex;justify-content:space-between;align-items:flex-end;
 border-top:1px solid #e3e6ea;padding-top:20px;font-size:19px;color:#5c6470}}
.foot b{{color:#15171a}}
.url{{font-weight:700;color:#15171a;font-size:21px}}
</style>
<h1>What Agents Buy<span class="dot">.</span></h1>
<div class="sub">I give an AI agent a real wallet, buy from the APIs agents pay for, and publish every receipt.</div>
<div class="row">{stamps}</div>
<div class="foot"><span>{stat}</span><span class="url">whatagentsbuy.com</span></div>'''

    tmp = os.path.join(HERE, ".og.html")
    open(tmp, "w").write(html)
    out = os.path.join(HERE, "public", "og.png")
    subprocess.run([CHROME, "--headless", "--disable-gpu", f"--screenshot={out}",
                    "--window-size=1200,630", "--hide-scrollbars",
                    "--virtual-time-budget=2000", f"file://{tmp}"],
                   check=False, capture_output=True)
    os.remove(tmp)
    kb = os.path.getsize(out) / 1024 if os.path.exists(out) else 0
    print(f"og.png rebuilt: {kb:.0f}KB · {' '.join(p['grade'] for p in picks)} · "
          f"{n_graded} grades across {n_svc} services")


if __name__ == "__main__":
    main()
