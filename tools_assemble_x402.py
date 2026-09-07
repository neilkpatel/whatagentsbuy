#!/usr/bin/env python3
"""Assemble the ported x402 dashboard page from the Sawgrass original.

Faithful port, not a rewrite: the CSS, the SVG bar charts, the tooltip, the
UTC-day/partial-bar handling and the localStorage instant-repaint all come over
verbatim. Only three things change:
  1. title/subline (rename, and the store/census/rails links come out)
  2. everything from <section id="p-x402momentum"> down is dropped
  3. render() is cut after the circulation block, which is where the removed
     sections' element ids start (an untrimmed render throws on the first
     missing element and paints nothing)
"""
import re, os

SRC = os.path.expanduser("~/Desktop/Projects/sawgrass-signals/products/dashboard/index.html")
OUT = os.path.expanduser("~/Desktop/Projects/8_4_26_touchstone/templates_x402.html")
L = open(SRC).read().split("\n")


def seg(a, b):
    """1-indexed inclusive line range."""
    return "\n".join(L[a - 1:b])


def _line_of(marker, start=0, what=""):
    """1-indexed line number of the first line equal to `marker`.

    Boundaries are found by CONTENT, not by hardcoded line numbers. The first
    version carried literal offsets (1,60), (65,153), (270,587); a single line
    added anywhere upstream in sawgrass-signals would have shifted every one of
    them and produced a silently wrong template -- half a stylesheet, a hero
    missing its closing tag, a script cut mid-function. Nothing would have
    failed, the page would just have been subtly broken.
    """
    for i in range(start, len(L)):
        if L[i].strip() == marker:
            return i + 1
    raise SystemExit(
        f"assemble: could not find {what or marker!r} in the source dashboard. "
        f"The upstream page changed shape; re-check the boundaries before building.")


HEAD_END = _line_of("</head>", what="</head>")
HERO_START = _line_of('<section id="p-hero">', what="p-hero opening tag")
HERO_END = _line_of("</section>", start=HERO_START, what="p-hero closing tag")
JS_START = _line_of("<script>", start=HERO_END, what="page script opening tag")
JS_END = _line_of("</script>", start=JS_START, what="page script closing tag")

# Sanity: the ranges must be ordered and non-trivial, or a marker matched
# somewhere unexpected (e.g. a nested </section>).
assert HEAD_END < HERO_START < HERO_END < JS_START < JS_END, (
    f"assemble: boundaries out of order "
    f"(head {HEAD_END}, hero {HERO_START}-{HERO_END}, js {JS_START}-{JS_END})")
assert HERO_END - HERO_START > 40, "assemble: p-hero section looks truncated"
assert JS_END - JS_START > 200, "assemble: page script looks truncated"

head = seg(1, HEAD_END)          # doctype, meta, favicon, full <style>, </head>
hero = seg(HERO_START, HERO_END)  # <section id="p-hero"> ... </section>
# The ENTIRE original script, render() included and unmodified. Cutting it by
# hand was how the monthly-since-launch and Base-sequencer cards ended up blank:
# their paint code sat in the removed span. Instead of surgery on 300 lines of
# working chart code, missing elements are made harmless below.
js_all = seg(JS_START, JS_END)

# 1. rename
# "Agentic payments" is the phrase people actually search and repeat, and x402
# is the protocol keyword. Both belong in the title; the URL carries x402 too.
head = head.replace("<title>Sawgrass x402 Command Center</title>",
                    "<title>x402 Agentic Payments Dashboard: live volume, "
                    "transactions and stablecoin supply</title>")

# SEO + share card head additions. The original was a private dashboard and
# carried none of this; as a public page it needs to be findable and to unfurl.
head = head.replace("</head>", """<meta name="description" content="The live dashboard for agentic payments: daily x402 volume and transaction counts on Base and Solana, USDC and total stablecoin circulation, and net issuance. Read from chain and refreshed continuously.">
<link rel="canonical" href="https://whatagentsbuy.com/x402">
<meta property="og:title" content="x402 Agentic Payments Dashboard">
<meta property="og:description" content="Live agentic-payments volume: daily x402 transactions and dollars, USDC and stablecoin circulation, read straight from chain.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://whatagentsbuy.com/x402">
<meta property="og:image" content="https://whatagentsbuy.com/og-x402.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://whatagentsbuy.com/og-x402.png">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Dataset","name":"x402 Agentic Payments Dashboard","description":"Daily x402 protocol volume and payment counts across Base and Solana, USDC and total stablecoin circulation, and Base sequencer fees. Read from chain and refreshed continuously.","url":"https://whatagentsbuy.com/x402","license":"https://whatagentsbuy.com/about","isAccessibleForFree":true,"creator":{"@type":"Person","name":"Neil K. Patel","url":"https://neilkpatel.com"},"publisher":{"@type":"Organization","name":"What Agents Buy","url":"https://whatagentsbuy.com"},"temporalCoverage":"2025-05/..","measurementTechnique":"On-chain settlement logs, UTC calendar days","variableMeasured":[{"@type":"PropertyValue","name":"x402 volume, 24h","unitText":"USD"},{"@type":"PropertyValue","name":"x402 payments, 24h","unitText":"count"},{"@type":"PropertyValue","name":"USDC in circulation","unitText":"USD"},{"@type":"PropertyValue","name":"Base sequencer fees, 24h","unitText":"USD"}],"distribution":{"@type":"DataDownload","encodingFormat":"application/json","contentUrl":"https://whatagentsbuy.com/api/dashboard"}}</script>
<script defer src="/_vercel/insights/script.js"></script>
<script defer src="/_vercel/speed-insights/script.js"></script>
</head>""")

body_head = '''<body>
  <h1 id="title">x402 Agentic Payments Dashboard</h1>
  <div class="sub"><span class="livedot"></span>Live on-chain data &middot; auto-refresh <span id="rs"></span>s &middot; <span id="gen"></span> &middot; <a href="/">What Agents Buy</a> &middot; <a href="/leaderboard">who gets paid</a></div>
'''

foot = '''
  <div class="foot">Sources: x402scan API (protocol stats) &middot; DefiLlama (stablecoin supply) &middot; Base mainnet RPC &middot; cached ~5 min server-side. UTC days throughout. Part of <a href="/">What Agents Buy</a>, independent reviews of the x402 APIs behind agentic commerce &middot; by <a href="/about">Neil K. Patel</a>.</div>
  <div id="tip"></div>
'''

# 3. Make elements from dropped sections harmless.
#
# render() is one long function that paints every panel. Dropping sections from
# the HTML leaves it calling .textContent / .innerHTML on nulls, and the FIRST
# such call throws, so the page paints nothing at all. Rather than edit
# render(), getElementById falls back to a detached div: a real element, so
# textContent, innerHTML, classList and setAttribute all behave, and
# clientWidth reads 0 which the chart code already handles (`||300`).
#
# This keeps the original render() byte-for-byte, so future sections can be
# added back by pasting their markup in, with no JS changes.
shim = """
// Panels that are not on this page must not break the ones that are: render()
// paints every panel in one pass, and a single null would abort all of it.
// A detached div satisfies every property render() touches.
(function(){
  const real = document.getElementById.bind(document);
  document.getElementById = id => real(id) || document.createElement("div");
})();
"""
js = js_all.replace("<script>", "<script>" + shim, 1)

# A dashboard whose credibility is its numbers must never show a number without
# saying how old it is, and must never fail silently. The stock init did both:
# a failed first fetch left every card reading "—" with no explanation and a
# mute 120s retry, and a stale upstream rendered old figures as confidently as
# fresh ones. Replaced wholesale rather than edited into render().
OLD_INIT = """applyHidden();
// paint the last-seen numbers instantly; the live fetch replaces them when it lands
try{const c=JSON.parse(localStorage.dashCache||'null');if(c){render(c);document.getElementById('gen').textContent+=' \\u00b7 refreshing\\u2026';}}catch{}
tick().then(d=>setInterval(tick,((d&&d.refreshSeconds)||120)*1000))
  .catch(()=>setInterval(tick,120000));"""

NEW_INIT = """applyHidden();

// One banner for both failure modes, because to a reader they are the same
// question: can I trust what is on screen right now?
function notice(kind, msg){
  // querySelector, NOT getElementById: the shim above makes getElementById
  // return a fresh detached div for anything missing, so the "does it exist
  // yet" check never failed, the banner was written to a throwaway element and
  // never entered the page. The shim protects render(); it must not be used by
  // code that needs to know whether a node actually exists.
  let el=document.querySelector('#notice');
  if(!el){
    el=document.createElement('div'); el.id='notice';
    document.querySelector('.sub').insertAdjacentElement('afterend', el);
  }
  if(!kind){ el.remove(); return; }
  el.className='notice '+kind;
  el.textContent=msg;
}

// How old the DATA is, not how long ago we fetched it. A successful fetch of a
// stale upstream is the failure that looks healthiest.
function freshness(d){
  const gen = Date.parse((d&&d.generated)||'');
  if(!gen) return;
  const mins = (Date.now()-gen)/60000;
  const days = (d&&d.hero&&d.hero.daily7||[]).slice(-1)[0];
  const lastClosed = days ? (Date.now()-Date.parse(days.date+'T00:00:00Z'))/86400000 : null;
  if(lastClosed!=null && lastClosed>3){
    notice('warn','Upstream data looks stale: the newest closed day is '+days.date+
      '. Figures below may not reflect the last '+Math.floor(lastClosed)+' days.');
  } else if(mins>90){
    notice('warn','These numbers were last rebuilt '+Math.round(mins/60)+
      'h ago. The feed may be degraded.');
  } else {
    notice(null);
  }
}

let painted=false;
// Paint the last-seen numbers instantly, clearly labelled as not-yet-live.
try{
  const c=JSON.parse(localStorage.dashCache||'null');
  if(c){ render(c); painted=true;
    document.getElementById('gen').textContent+=' \\u00b7 refreshing\\u2026'; }
}catch{}

// Does its own fetch rather than calling tick(), because tick() cannot detect
// this failure: a 5xx from the API still carries a JSON body, so `await
// r.json()` resolves happily and tick() hands render() an error object. The
// page then paints dashes and "Invalid Date" with no banner, which is exactly
// what a broken-but-silent dashboard looks like. A non-2xx status, a missing
// `hero`, or an `error` field are all failures no matter that JSON parsed.
async function safeTick(){
  try{
    const r=await fetch('/api/dashboard');
    if(!r.ok) throw new Error('HTTP '+r.status);
    const d=await r.json();
    if(!d||d.error||!d.hero||typeof d.hero.vol24!=='number'){
      throw new Error((d&&d.error)||'malformed payload');
    }
    try{localStorage.dashCache=JSON.stringify(d)}catch{}
    render(d);
    painted=true;
    freshness(d);
    return d;
  }catch(e){
    // Never leave a first-time visitor staring at dashes with no explanation.
    notice('err', painted
      ? 'Live refresh is failing, so these are the last good numbers, not current ones.'
      : 'Live data is unavailable right now. Retrying every 2 minutes.');
    return null;
  }
}

safeTick().then(d=>setInterval(safeTick,((d&&d.refreshSeconds)||120)*1000));"""

assert OLD_INIT in js, "init block not found; the upstream page changed"
js = js.replace(OLD_INIT, NEW_INIT, 1)

# --- light theme -------------------------------------------------------------
# The dashboard was a private dark tool. As a public page under an editorial
# site that is light throughout, dark read as a different product, and it
# embeds badly on Twitter and LinkedIn where the surrounding page is white.
#
# Swapped by value, not by rewriting the CSS: every colour below is either a
# :root variable or a literal passed into a chart call, so remapping the exact
# strings preserves the layout completely.
#
# Chart hues keep their MEANING across the swap (volume blue, transactions
# green, USDC gold, stablecoins purple, negative red) but move to values that
# hold contrast on white; the dark originals were tuned for a #0d0f13 ground
# and look washed out on it.
PALETTE = {
    # structure. The first light pass set --bg #fbfbfc against --card #ffffff:
    # a 4/255 difference, so the panels dissolved into the page and the whole
    # thing read as one flat white field. The ground is now a proper cool grey
    # so white cards actually sit on top of something.
    "#0d0f13": "#eef1f6",   # --bg          page ground
    "#161a21": "#ffffff",   # --card        panel
    "#252b35": "#dfe4ec",   # --line        borders
    "#e7ecf3": "#101828",   # --ink         primary text
    "#8592a3": "#667085",   # --muted       secondary text
    "#12151b": "#f7f9fc",   # inset cells + table head
    "#0b0d11": "#ffffff",   # tooltip ground
    # semantic
    "#3ddc97": "#047857",   # --accent      "up" / hot
    "#6aa7ff": "#2563eb",   # --blue        links
    "#ff6b6b": "#dc2626",   # "down"
    "#e66767": "#dc2626",   # negative bars
    # series. The dark originals were tuned for a #0d0f13 ground; dropped onto
    # white the gold went muddy brown and the purple went fluorescent.
    "#3987e5": "#2563eb",   # volume blue
    "#199e70": "#047857",   # transactions green
    "#f4c04e": "#d97706",   # USDC amber
    "#c98500": "#b45309",   # USDC amber, darker variant
    "#9085e9": "#7c3aed",   # stablecoins violet
}

def relight(txt):
    # Longest-first so no key is a prefix of another mid-replacement.
    for k in sorted(PALETTE, key=len, reverse=True):
        txt = txt.replace(k, PALETTE[k]).replace(k.upper(), PALETTE[k])
    return txt

head = relight(head)
js = relight(js)

head = head.replace("box-shadow:0 4px 14px rgba(0,0,0,.5)",
                    "box-shadow:0 8px 24px rgba(16,24,40,.14)")
head = head.replace("color-scheme:dark", "color-scheme:light")

# Depth. On a dark ground a 1px hairline is enough to separate a panel, because
# the panel is LIGHTER than what surrounds it. On a light ground the panel is
# white on near-white, so it needs a shadow to read as a card at all. Two-layer
# shadow (tight contact + soft ambient) is what stops it looking like flat CSS.
head = head.replace("</style>", """
  /* Depth. On a dark ground a hairline separates a panel because the panel is
     LIGHTER than its surround. On white it is white-on-near-white, so it needs
     a shadow to read as a card. Two layers: tight contact + soft ambient. */
  .htile, .card, table{box-shadow:0 1px 2px rgba(16,24,40,.04),0 6px 16px -4px rgba(16,24,40,.07)}
  .htile{padding:0 0 12px;border-radius:13px;transition:box-shadow .16s ease}
  .htile:hover{box-shadow:0 1px 2px rgba(16,24,40,.05),0 10px 26px -6px rgba(16,24,40,.11)}

  /* A real card header: the label sits in its own bar, ruled off from the
     figure. Without it the label floats above the number and the card has no
     top edge of its own. `:first-child` matters because .k is reused for the
     footnote at the bottom of the same tile. */
  .htile > .k:first-child{padding:13px 18px 11px;margin:0 0 13px;
    border-bottom:1px solid var(--line);background:#f9fafc;
    border-radius:12px 12px 0 0;font-weight:670;color:var(--muted);
    font-size:11px;letter-spacing:.075em;text-transform:uppercase}
  .htile > *:not(.k:first-child){margin-left:18px;margin-right:18px}
  .htile .v{letter-spacing:-.028em;font-weight:760}
  .htile .d{margin-bottom:6px}

  /* Section rules. Eight identical tiles in a stack have no hierarchy; these
     say what each pair of rows is measuring. */
  .sect{display:flex;align-items:center;gap:12px;margin:26px 0 11px;
    font-size:11px;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
    color:var(--muted)}
  .sect::after{content:"";flex:1;height:1px;background:var(--line)}
  .sect:first-of-type{margin-top:4px}

  h1{font-weight:770;letter-spacing:-.022em;font-size:23px}
  .sub{padding-bottom:16px;border-bottom:1px solid var(--line);margin-bottom:4px}
  /* Live dot: the page auto-refreshes, and nothing else on it says so. */
  .livedot{display:inline-block;width:7px;height:7px;border-radius:50%;
    background:#16a34a;margin-right:7px;vertical-align:middle;
    animation:lp 2.4s ease-in-out infinite}
  @keyframes lp{0%,100%{opacity:1}50%{opacity:.35}}
  .qcell{background:#f7f9fc;box-shadow:none}
  .notice{margin:0 0 14px;padding:10px 14px;border-radius:9px;font-size:13px;line-height:1.45;border:1px solid}
  .notice.warn{background:#fffbeb;border-color:#fde68a;color:#92400e}
  .notice.err{background:#fef2f2;border-color:#fecaca;color:#991b1b}
  table{border-radius:12px}
  body{padding-bottom:44px}
</style>""")

# Section rules, inserted before the row each one introduces.
SECTIONS = [
    ('        <div class="k">x402 volume · 24h</div>', "x402 protocol activity"),
    ('        <div class="k">USDC in circulation</div>', "Stablecoin supply"),
    ('        <div class="k">Base sequencer fees · COIN\'s chain toll</div>', "Base, the chain underneath"),
]
for marker, label in SECTIONS:
    i = hero.index(marker)
    row = hero.rindex('<div class="hero">', 0, i)
    hero = hero[:row] + f'<h2 class="sect">{label}</h2>\n    ' + hero[row:]


page = head + "\n" + body_head + "\n" + hero + "\n" + foot + "\n" + js + "\n</body>\n</html>\n"

# Safety: no element id from a dropped section may survive in the JS, or the
# first paint throws and the page renders empty.
dropped = set(re.findall(r"getElementById\('([a-z0-9_-]+)'\)", "\n".join(L[154:265])))
present = set(re.findall(r'id="([a-z0-9_-]+)"', hero)) | {"title", "rs", "gen", "tip"}
used = set(re.findall(r"getElementById\('([a-z0-9_-]+)'\)", js))
orphans = sorted(u for u in used if u not in present)
open(OUT, "w").write(page)
print(f"wrote {OUT} ({len(page):,} bytes)")
print(f"ids referenced but not present: {orphans if orphans else 'none'}")
