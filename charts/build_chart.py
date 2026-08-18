#!/usr/bin/env python3
"""Build charts/who-agents-pay.html from the current x402scan seller snapshot.

Re-runnable: `python3 charts/build_chart.py` after `python3 fetch_sellers.py`.
Reads data/x402scan_sellers.json, dedupes origin aliases by (tx, usd, buyers) —
one seller can list many origins (twit.sh and x402.twit.sh are one business) —
and writes a self-contained page. No network calls.
"""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SNAP = os.path.join(ROOT, "data", "x402scan_sellers.json")
OUT = os.path.join(HERE, "who-agents-pay.html")

snap = json.load(open(SNAP))
AS_OF = snap.get("date") or snap.get("generated", "")[:10]

# --- dedupe origin aliases -> distinct sellers -------------------------------
seen = {}
for r in snap["origins"]:
    k = (r["tx"], round(r["usd"], 4), r["buyers"])
    if k not in seen or len(r["host"]) < len(seen[k]["host"]):
        seen[k] = r
U = list(seen.values())
ALIASES = len(snap["origins"])
SELLERS = len(U)
TOT_TX = sum(r["tx"] for r in U)
TOT_USD = sum(r["usd"] for r in U)

ranked = sorted(U, key=lambda r: -r["tx"])[:10]
TOP1_SHARE = ranked[0]["tx"] / TOT_TX * 100

pts = [{"h": r["host"], "tx": r["tx"], "usd": round(r["usd"], 2), "b": r["buyers"],
        "t": round(r["usd"] / max(r["tx"], 1), 6)}
       for r in U if r["tx"] >= 20 and r["usd"] > 0]

BLURB = {
    "blockrun.ai": "LLM router — one endpoint, every frontier model, no API key",
    "claw402.ai": "Router sibling — 732K calls for $1,479 total",
    "api.vishwalab.com": "147K calls, $147 earned — a tenth of a cent each",
    "x402.ottoai.services": "Crypto news feed",
    "api.clusterprotocol.ai": "#2 by dollars — and absent from Coinbase's registry",
    "twit.sh": "X/Twitter search — the most-called single resource",
    "api.syraa.fun": "Trading bot tooling",
    "stableenrich.dev": "Data enrichment — Exa, LinkedIn, Maps resold per call",
    "api.onesource.io": "Widest buyer base in the market. $0.11 per buyer.",
    "x402.sniperx.fun": "33K calls from 17 wallets",
}

# Editorial picks: outliers on price, concentration, what's sold, or what
# directories miss. Each carries the reason it earned the slot.
INTERESTING = [
    ("laso.finance", "$565 a call",
     "Highest average ticket in the entire market by 70x. Sells prepaid and gift cards — real money buying real things."),
    ("api.clusterprotocol.ai", "$84K, invisible",
     "Second-biggest earner on x402 and it is not in Coinbase's discovery registry at all. Any directory built on CDP alone cannot see it."),
    ("api.onesource.io", "1,328 buyers, $152",
     "The widest buyer base in the market earns eleven cents per buyer. This is airdrop farming wearing the costume of adoption."),
    ("x402.dtelecom.org", "26 buyers, $30K",
     "Decentralised telecom. Fourth-biggest earner in the market from twenty-six wallets."),
    ("api.bitrefill.com", "Actual goods",
     "Gift cards, eSIMs and phone top-ups. One of the only places an agent buys something a human can hold or redeem."),
    ("scvd.store", "A human-run store",
     "“Sean-Claude Van Damme's General Store.” Sells a signed certificate that, in its own words, entitles the buyer to nothing whatsoever."),
    ("x402.sniperx.fun", "17 wallets, 33K calls",
     "A sniping bot almost certainly paying itself. The shape of self-traffic: enormous call count, almost no counterparties."),
    ("api.purch.xyz", "Buys Amazon for you",
     "Agents place real Amazon and Shopify orders through it, paying in USDC."),
]
BY = {r["host"]: r for r in U}
HL = [h for h, _, _ in INTERESTING if h in BY]


def usd(v):
    """Money never rounds to $0.00 — sub-cent tickets are the whole point."""
    if v == 0:
        return "$0"
    if v < 0.01:
        return f"${v:.4f}"
    if v < 1:
        return f"${v:.3f}"
    return f"${v:,.2f}"


maxtx = ranked[0]["tx"]
bars = "".join(
    f'''<div class="brow" tabindex="0" data-tip="{r['host']} &middot; {r['tx']:,} transactions &middot; ${r['usd']:,.0f} earned &middot; {r['buyers']:,} buyers &middot; {usd(r['usd']/max(r['tx'],1))} average">
<div class="blabel"><span class="rank">{i}</span><span class="bname">{r['host']}</span></div>
<div class="btrack"><div class="bfill" style="width:{max(r['tx']/maxtx*100,0.35):.3f}%"></div><span class="bval">{r['tx']:,}</span></div>
<div class="bshare">{r['tx']/TOT_TX*100:.1f}%</div></div>'''
    for i, r in enumerate(ranked, 1))

cards = "".join(
    f'''<article class="card"><div class="ctag">{tag}</div><h3>{h}</h3><p>{note}</p>
<dl><div><dt>calls</dt><dd>{BY[h]['tx']:,}</dd></div><div><dt>earned</dt><dd>${BY[h]['usd']:,.0f}</dd></div>
<div><dt>buyers</dt><dd>{BY[h]['buyers']:,}</dd></div><div><dt>avg</dt><dd>{usd(BY[h]['usd']/max(BY[h]['tx'],1))}</dd></div></dl>
</article>''' for h, tag, note in INTERESTING if h in BY)

rows = "".join(
    f"<tr><td>{i}</td><td>{r['host']}</td><td class=n>{r['tx']:,}</td><td class=n>{r['tx']/TOT_TX*100:.1f}%</td>"
    f"<td class=n>${r['usd']:,.0f}</td><td class=n>{r['buyers']:,}</td><td class=n>{usd(r['usd']/max(r['tx'],1))}</td></tr>"
    for i, r in enumerate(ranked, 1))

CSS = """
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--surface-2:#f2f2f0;--line:#e4e4e0;
--text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#83827c;
--series-1:#2a78d6;--series-2:#eb6834;--ctx:#c9c9c4;}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;
--surface-1:#1a1a19;--surface-2:#232322;--line:#33332f;--text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#8f8e86;
--series-1:#3987e5;--series-2:#d95926;--ctx:#4a4a45;}}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;--surface-2:#232322;--line:#33332f;
--text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#8f8e86;--series-1:#3987e5;--series-2:#d95926;--ctx:#4a4a45;}
*{box-sizing:border-box}body{margin:0;background:var(--surface-1);color:var(--text-primary);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
.viz-root{background:var(--surface-1);min-height:100vh}
.wrap{max-width:980px;margin:0 auto;padding:44px 22px 80px}
h1{font-size:30px;letter-spacing:-.02em;margin:0 0 6px;line-height:1.15}
.sub{color:var(--text-secondary);font-size:15px;margin:0 0 4px}
.stamp{color:var(--text-muted);font-size:12.5px;margin-top:10px;display:inline-block;
background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:5px 10px}
h2{font-size:20px;margin:52px 0 4px;letter-spacing:-.01em}
.note{color:var(--text-secondary);font-size:14px;margin:0 0 18px;max-width:70ch}
.hero{display:flex;gap:26px;flex-wrap:wrap;margin:26px 0 0;padding:18px 20px;
background:var(--surface-2);border:1px solid var(--line);border-radius:12px}
.hero div{min-width:120px}
.hero .v{font-size:30px;font-weight:650;letter-spacing:-.02em;line-height:1.1}
.hero .k{font-size:12px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.06em;margin-top:3px}
.brow{display:grid;grid-template-columns:210px 1fr 46px;gap:12px;align-items:center;padding:5px 0;outline:none}
.brow:hover .bfill,.brow:focus-visible .bfill{filter:brightness(1.08)}
.blabel{display:flex;gap:8px;align-items:baseline;min-width:0}
.rank{color:var(--text-muted);font-size:12px;width:15px;text-align:right;flex:none}
.bname{font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.btrack{position:relative;height:22px;display:flex;align-items:center}
.bfill{height:22px;background:var(--series-1);border-radius:0 4px 4px 0;transition:filter .12s}
.bval{font-size:12px;color:var(--text-secondary);margin-left:8px;white-space:nowrap;font-variant-numeric:tabular-nums}
.bshare{font-size:12.5px;color:var(--text-muted);text-align:right;font-variant-numeric:tabular-nums}
.chart{margin-top:8px;background:var(--surface-2);border:1px solid var(--line);border-radius:12px;padding:18px 20px;overflow-x:auto}
svg{display:block;max-width:100%;height:auto}
.grid line{stroke:var(--line);stroke-width:1}
.axl{fill:var(--text-muted);font-size:10.5px}
.axt{fill:var(--text-secondary);font-size:11px;font-weight:600}
.dot{fill:var(--ctx)}
.dot.hl{fill:var(--series-2);stroke:var(--surface-2);stroke-width:2}
.dlab{fill:var(--text-primary);font-size:10.5px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(288px,1fr));gap:14px;margin-top:8px}
.card{background:var(--surface-2);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.card h3{font-size:15px;margin:6px 0 6px;word-break:break-word}
.card p{font-size:13.5px;color:var(--text-secondary);margin:0 0 12px}
.ctag{display:inline-block;font-size:11px;font-weight:650;letter-spacing:.03em;color:var(--series-2);
border:1px solid currentColor;border-radius:99px;padding:2px 9px}
.card dl{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:0;padding-top:11px;border-top:1px solid var(--line)}
.card dt{font-size:10px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em}
.card dd{margin:1px 0 0;font-size:13px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--text-muted);background:var(--surface-2)}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
details{margin-top:16px}summary{cursor:pointer;font-size:13px;color:var(--text-secondary)}
#tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .1s;background:var(--text-primary);
color:var(--surface-1);font-size:12px;padding:6px 9px;border-radius:6px;max-width:300px;z-index:9}
.foot{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);font-size:12.5px;color:var(--text-muted)}
"""

JS = """
const W=900,H=460,M={t:16,r:22,b:44,l:62};
const xs=v=>M.l+(Math.log10(v)-1)/(Math.log10(1e7)-1)*(W-M.l-M.r);
const ys=v=>H-M.b-(Math.log10(v)+4)/(Math.log10(1e3)+4)*(H-M.t-M.b);
let s='<g class="grid">';
for(const gx of [10,100,1e3,1e4,1e5,1e6,1e7]) s+=`<line x1="${xs(gx)}" y1="${M.t}" x2="${xs(gx)}" y2="${H-M.b}"/>`;
for(const gy of [1e-4,1e-3,1e-2,0.1,1,10,100,1e3]) s+=`<line x1="${M.l}" y1="${ys(gy)}" x2="${W-M.r}" y2="${ys(gy)}"/>`;
s+='</g>';
const fx=v=>v>=1e6?(v/1e6)+'M':v>=1e3?(v/1e3)+'K':v;
for(const gx of [10,100,1e3,1e4,1e5,1e6,1e7]) s+=`<text class="axl" x="${xs(gx)}" y="${H-M.b+15}" text-anchor="middle">${fx(gx)}</text>`;
for(const gy of [1e-4,1e-3,1e-2,0.1,1,10,100,1e3]) s+=`<text class="axl" x="${M.l-8}" y="${ys(gy)+3}" text-anchor="end">$${gy<1?gy.toFixed(gy<0.01?4:2):gy}</text>`;
s+=`<text class="axt" x="${(M.l+W-M.r)/2}" y="${H-6}" text-anchor="middle">calls in 30 days &#8594;</text>`;
s+=`<text class="axt" transform="translate(14,${(M.t+H-M.b)/2}) rotate(-90)" text-anchor="middle">average price per call &#8594;</text>`;
const money=v=>v<0.01?'$'+v.toFixed(4):v<1?'$'+v.toFixed(3):'$'+v.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
for(const p of PTS){ if(HL.includes(p.h)) continue;
  s+=`<circle class="dot" cx="${xs(p.tx)}" cy="${ys(p.t)}" r="4" data-h="${p.h}" data-tx="${p.tx}" data-usd="${p.usd}" data-b="${p.b}" data-t="${p.t}"/>`;}
for(const p of PTS){ if(!HL.includes(p.h)) continue;
  s+=`<circle class="dot hl" cx="${xs(p.tx)}" cy="${ys(p.t)}" r="6" data-h="${p.h}" data-tx="${p.tx}" data-usd="${p.usd}" data-b="${p.b}" data-t="${p.t}"/>`;
  s+=`<text class="dlab" x="${xs(p.tx)+10}" y="${ys(p.t)-8}">${p.h.replace(/^(api|www|x402)\\./,'')}</text>`;}
document.getElementById('sc').innerHTML=s;
const tip=document.getElementById('tip');
function show(e,t){tip.innerHTML=t;tip.style.opacity=1;const r=tip.getBoundingClientRect();
 tip.style.left=Math.min(e.clientX+14,innerWidth-r.width-10)+'px';
 tip.style.top=Math.max(e.clientY-r.height-12,8)+'px';}
document.querySelectorAll('#sc .dot').forEach(c=>{
 c.addEventListener('mousemove',e=>show(e,`<b>${c.dataset.h}</b><br>${(+c.dataset.tx).toLocaleString()} calls &middot; $${(+c.dataset.usd).toLocaleString()}<br>${(+c.dataset.b).toLocaleString()} buyers &middot; ${money(+c.dataset.t)} avg`));
 c.addEventListener('mouseleave',()=>tip.style.opacity=0);});
document.querySelectorAll('.brow').forEach(r=>{
 r.addEventListener('mousemove',e=>show(e,r.dataset.tip));
 r.addEventListener('mouseleave',()=>tip.style.opacity=0);});
"""

html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Who agents actually pay — x402, 30 days to {AS_OF}</title>
<style>{CSS}</style></head><body><div class="viz-root"><div class="wrap">

<h1>Who agents actually pay</h1>
<p class="sub">Every x402 seller that took money in the last 30 days, ranked — and the ones worth a second look.</p>
<div class="stamp">Data as of {AS_OF} &middot; x402scan settlement (Base + Solana) &middot; {SELLERS} sellers deduped from {ALIASES:,} origin aliases</div>

<div class="hero">
<div><div class="v">{TOT_TX/1e6:.1f}M</div><div class="k">transactions</div></div>
<div><div class="v">${TOT_USD/1000:.0f}K</div><div class="k">total paid</div></div>
<div><div class="v">{SELLERS}</div><div class="k">sellers with revenue</div></div>
<div><div class="v" style="color:var(--series-2)">{TOP1_SHARE:.1f}%</div><div class="k">is one seller</div></div>
</div>

<h2>The ten most-used endpoints</h2>
<p class="note">By transaction count. The bars are on a linear scale on purpose &mdash; the fact that nine of them are
slivers is the finding. BlockRun, an LLM router, is {TOP1_SHARE:.1f}% of every paid call on the network, at 1.8 cents each.</p>
<div class="chart">{bars}</div>

<h2>Volume is not money</h2>
<p class="note">Each dot is a seller: calls across, average price per call up. Both axes are logarithmic, which is the only
way 20 calls and 9.4 million fit on one page. The eight highlighted below sit far off the crowd &mdash;
that is what makes them interesting. Everything grey is the long tail selling sub-cent data.</p>
<div class="chart"><svg id="sc" viewBox="0 0 900 460" role="img" aria-label="Scatter of transactions versus average price per call for {len(pts)} x402 sellers"></svg></div>

<h2>The eight most interesting</h2>
<p class="note">Not the biggest &mdash; the ones that tell you something. Outliers on price, on concentration,
on what is actually being sold, or on what the directories fail to see.</p>
<div class="cards">{cards}</div>

<details><summary>Table view &mdash; top ten by transactions</summary>
<table><thead><tr><th>#</th><th>Seller</th><th class=n>Calls</th><th class=n>Share</th><th class=n>Earned</th><th class=n>Buyers</th><th class=n>Avg</th></tr></thead>
<tbody>{rows}</tbody></table></details>

<div class="foot">Source: x402scan <code>public.sellers.bazaar.list</code>, 30-day window pulled {AS_OF}, deduplicated by
recipient wallet (one seller can list many origin aliases &mdash; twit.sh and x402.twit.sh are one business, not two).
Covers Base and Solana; Polygon is not indexed here. &ldquo;Average&rdquo; is total USDC divided by call count, not a list price.
Rebuild: <code>python3 fetch_sellers.py &amp;&amp; python3 charts/build_chart.py</code></div>
</div></div>
<div id="tip"></div>
<script>const PTS={json.dumps(pts)},HL={json.dumps(HL)};{JS}</script></body></html>"""

open(OUT, "w").write(html)
print(f"wrote {OUT}")
print(f"  {SELLERS} sellers ({ALIASES:,} aliases) · {TOT_TX:,} tx · ${TOT_USD:,.0f} · top1 {TOP1_SHARE:.1f}%")
print(f"  {len(pts)} scatter points · {len(HL)} highlighted")
