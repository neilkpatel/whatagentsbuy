#!/usr/bin/env python3
"""Build touchstone.neilkpatel.com: Neil's live feed on the agent economy.

Feed-first. The graded directory survives as the receipts tab (frozen snapshot
until the sweep is revived). RSS at /feed.xml.
"""
import json, os, html, re, email.utils, time, datetime, collections, glob, subprocess, sys

RSS_ITEMS = 50
from urllib.parse import urlparse
import preflight

HERE = os.path.dirname(os.path.abspath(__file__))

# Gate the build on the test suite. Too many silent data bugs shipped this way,
# from overwritten files to renamed fields, each of which a test now catches. A
# failing test must stop the build rather than deploy broken data. Skippable only
# with WAB_SKIP_TESTS=1 for a deliberate emergency rebuild.
if not os.environ.get("WAB_SKIP_TESTS"):
    _t = subprocess.run([sys.executable, os.path.join(HERE, "tests.py")],
                        capture_output=True, text=True)
    if _t.returncode != 0:
        sys.stderr.write(_t.stdout + _t.stderr +
                         "\nBUILD ABORTED: tests failed. Fix them or set WAB_SKIP_TESTS=1.\n")
        raise SystemExit(1)
    print(_t.stdout.strip().splitlines()[-1] + " (tests)")
D = json.load(open(os.path.join(HERE, "data", "latest.json")))
FEED = json.load(open(os.path.join(HERE, "data", "feed.json")))
NOTES = json.load(open(os.path.join(HERE, "data", "notes.json")))
_sv = os.path.join(HERE, "data", "services.json")
SERVICES = json.load(open(_sv)) if os.path.exists(_sv) else {}
_lb = os.path.join(HERE, "data", "leaderboard.json")
LB = json.load(open(_lb)) if os.path.exists(_lb) else None
_np = os.path.join(HERE, "data", "newest.json")
NEWEST = json.load(open(_np)) if os.path.exists(_np) else []
O = D["origins"]

# Agent usage of the MCP, loaded early so the homepage counter and
# /api/agent-usage.json share one source. Honest: curl/wget/unknown and our own
# probe UAs are tooling and our own testing, not agents, so they are excluded from
# the "agents" numbers rather than inflating them.
_ALOG_DIR = os.path.expanduser("~/Automation/whatagentsbuy-agentlogs/data")
_AGENT_HITS = []
if os.path.isdir(_ALOG_DIR):
    for _hf in sorted(__import__("glob").glob(os.path.join(_ALOG_DIR, "hits-*.jsonl"))):
        try:
            for _hl in open(_hf):
                _hl = _hl.strip()
                if _hl:
                    _AGENT_HITS.append(json.loads(_hl))
        except Exception:
            pass
_TOOLING_CLIENTS = {"curl", "wget", "unknown", "python-urllib", "python-requests",
                    "whatagentsbuy-x402", "touchstone-probe", "vercel", "go-http-client", "-", None}
def _is_agent_hit(h):
    return bool(h.get("tool")) and (h.get("client") or "unknown") not in _TOOLING_CLIENTS
_agent_week = [h for h in _AGENT_HITS
               if _is_agent_hit(h) and (h.get("ts") or 0) / 1000 >= __import__("time").time() - 7 * 86400]
AGENT_WEEK_HITS = len(_agent_week)
AGENT_WEEK_CLIENTS = len({h.get("client") for h in _agent_week})

SITE = "https://whatagentsbuy.com"
# Authored under a real, accountable identity: the E-E-A-T credibility signal
# Google and the AI crawlers read. Contact is LinkedIn; no email. The @crowdturtle
# pseudonym is retired from the page now that the site is not anonymous.
AUTHOR = "Neil K. Patel"
AUTHOR_URL = "https://www.neilkpatel.com/"
CONTACT = "https://www.linkedin.com/in/neilkiranpatel/"   # LinkedIn: the sole contact link
HANDLE = AUTHOR                                            # back-compat for f-strings
# One reusable schema.org author. sameAs (LinkedIn + personal site) is what tells
# Google which Neil Patel this is, and lifts the author's authority onto the site.
PERSON = {"@type": "Person", "name": AUTHOR, "url": f"{SITE}/about",
          "sameAs": [CONTACT, AUTHOR_URL],
          "knowsAbout": ["x402", "agentic commerce", "AI agent payments",
                         "stablecoin micropayments", "API quality"]}
NOW_ISO = datetime.datetime.now(datetime.timezone.utc).isoformat()
TODAY = NOW_ISO[:10]

# Every list of grades on the site sorts through this, so the ticker, the rail,
# the ratings table and the leaderboard can never disagree about what beats what.
GRADE_ORDER = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"]


def is_host(n):
    """A real origin, not a label like '220 origins'."""
    return bool(n) and " " not in n and "." in n


def grade_rank(g):
    """Best first. Anything unrecognised sorts to the bottom."""
    g = (g or "").strip()
    return GRADE_ORDER.index(g) if g in GRADE_ORDER else len(GRADE_ORDER)


# ---------------- directory data prep (frozen snapshot) ----------------
WINDOW = D.get("settlement_window_hours", 24)


def demand(r):
    s = r.get("settled")
    if not s:
        return ("quiet", "no settlement on chain in the last %gh" % WINDOW)
    tx, payers = s["settlements"], s["unique_payers"]
    if payers >= 10:
        return ("established", f"{payers} distinct wallets paid it on chain")
    if tx >= 500 and payers <= 2:
        return ("concentrated", f"{tx:,} settlements from {payers} wallet(s)")
    if payers >= 3:
        return ("traction", f"{payers} distinct wallets paid it on chain")
    return ("thin", f"{tx:,} settlement(s) from {payers} wallet(s)")


parent = {}


def find(x):
    parent.setdefault(x, x)
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def union(a, b):
    ra, rb = find(a), find(b)
    if ra != rb:
        parent[ra] = rb


for r in O:
    k = "svc:" + r["origin"]
    find(k)
    for a in (r.get("payto_addresses") or []):
        union(k, "addr:" + a.lower())


def canonical_score(r):
    d = r["origin"].lower()
    svc = (r.get("service") or "").lower().replace(" ", "").replace(".", "")
    brand = 0 if (svc and svc[:6] and svc[:6] in d.replace("-", "").replace(".", "")) else 1
    noise = sum(t in d for t in ("vercel.app", "run.app", "onrender.com", "railway.app",
                                 "workers.dev", "-git-", "preview", "staging"))
    return (brand, noise, d.count("-"), len(d))


groups = {}
for r in O:
    groups.setdefault(find("svc:" + r["origin"]), []).append(r)
primary = []
for members in groups.values():
    members.sort(key=canonical_score)
    head = members[0]
    head["also_listed"] = len(members) - 1
    head["also_names"] = [m["origin"] for m in members[1:6]]
    best = max((m for m in members if m.get("settled")),
               key=lambda m: m["settled"]["settlements"], default=None)
    head["settled"] = best["settled"] if best else None
    head["dem"], head["dem_why"] = demand(head)
    seen_f = []
    for m in members:
        for f in m["flags"]:
            if f not in seen_f:
                seen_f.append(f)
    head["flags"] = seen_f
    primary.append(head)
O = primary
O.sort(key=lambda r: (-(r.get("settled") or {}).get("settlements", 0),
                      -(r.get("settled") or {}).get("unique_payers", 0),
                      {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}.get(r.get("grade"), 5)))

settled = [r for r in O if r.get("settled")]
seen_addr, uniq_tx, uniq_usd = set(), 0, 0.0
for r in O:
    key = tuple(sorted(a.lower() for a in (r.get("payto_addresses") or [])))
    if r.get("settled") and key and key not in seen_addr:
        seen_addr.add(key)
        uniq_tx += r["settled"]["settlements"]
        uniq_usd += r["settled"]["usdc"]
top = max(settled, key=lambda r: r["settled"]["settlements"], default=None)
top_share = round(100 * top["settled"]["settlements"] / max(1, uniq_tx)) if top else 0
quiet_n = len([r for r in O if r["dem"] == "quiet"])

TIER_LABEL = {"established": "Established", "traction": "Traction", "thin": "Thin",
              "concentrated": "Concentrated", "quiet": "Quiet"}


def money(r):
    ps = sorted({c["live_amount"] or c["adv_amount"] for c in r["checked"]
                 if (c["live_amount"] or c["adv_amount"])})
    if not ps:
        return "n/a"
    return f"${min(ps):g}" if len(ps) == 1 else f"${min(ps):g}&ndash;${max(ps):g}"


dir_rows = []
for r in O:
    flags = "".join(f'<li class="bad">{html.escape(f)}</li>' for f in r["flags"]) or '<li class="ok">passed every check</li>'
    s = r.get("settled")
    setl = (f'{s["settlements"]:,}<span class="sub">${s["usdc"]:,.2f} &middot; {s["unique_payers"]} wallets</span>'
            if s else '<span class="dim">none</span>')
    n = r.get("also_listed") or 0
    also = (f'<span class="also">also listed as {", ".join(html.escape(x) for x in r.get("also_names", [])[:3])}'
            f'{f" and {n-3} more" if n > 3 else ""}</span>') if n else ""
    hay = (r["service"] + " " + r["origin"] + " " + " ".join(r["tags"])).lower()
    dir_rows.append(f'''<tr class="row" data-s="{html.escape(hay)}">
<td class="c-g"><span class="g g{r['grade'] if r.get('grade') in 'ABCDF' else 'U'}">{r['grade'] if r.get('grade') in 'ABCDF' else '&middot;'}</span></td>
<td class="c-o"><span class="oname">{html.escape(r['service'])}</span><span class="dom">{html.escape(r['origin'])}</span>{also}</td>
<td class="c-t"><span class="t t-{r['dem']}">{TIER_LABEL[r['dem']]}</span><span class="sub">{html.escape(r['dem_why'])}</span></td>
<td class="c-u num">{setl}</td>
<td class="c-p num">{money(r)}</td>
<td class="c-f"><ul class="ev">{flags}</ul></td>
</tr>''')

# ---------------- feed ----------------
def _warn_unnamed_subject(posts):
    """Warn when a post about a named service never says its name in the title or
    lede. The <title> tag always carries it, but the lede is what a person reads
    first and what a search snippet shows, and half the ledger once named its
    subject nowhere a human would look."""
    strip = {"api", "mcp", "www", "pro", "x402", "app", "com", "io", "ai", "dev", "xyz",
             "net", "org", "cloud", "info", "report", "zeabur", "surewhynot", "workers",
             "onrender"}
    for p in posts:
        name = (p.get("api") or {}).get("name", "")
        if not name or " " in name or "." not in name:
            continue                                  # a study label, not a host
        tokens = [t for t in re.split(r"[.\-]", name.lower()) if t not in strip and len(t) > 2]
        blob = (p.get("title", "") + " " + p.get("lede", "")).lower()
        if tokens and not any(t in blob for t in tokens):
            print(f"  WARNING: {p['id']} never names {name} in its title or lede")


def stamp_html(p):
    """The seal, in one place. A graded purchase gets its letter; a post that was
    never a purchase says which kind it is in the same spot, so the difference
    reads as deliberate rather than as a missing grade."""
    g = p.get("grade")
    if g:
        tone = {"A": "s-a", "B": "s-b", "C": "s-c", "D": "s-d", "F": "s-f"}.get(g[0], "s-c")
        return (f'<div class="stamp {tone}" role="img" aria-label="Graded {html.escape(g)}">'
                f'<span class="stamp-top">GRADE</span>'
                f'<span class="stamp-letter">{html.escape(g)}</span>'
                f'<span class="stamp-bot">VERIFIED</span></div>')
    _k = {"study": ("KEY", "INSIGHT", "", "s-insight"),
          "news":  ("", "NEWS", "not graded", "s-news")}.get(p.get("kind"))
    if not _k:
        return ""
    return (f'<div class="stamp {_k[3]}" role="img" aria-label="{_k[1]}, not a graded purchase">'
            + (f'<span class="stamp-top">{_k[0]}</span>' if _k[0] else "")
            + f'<span class="stamp-letter stamp-word">{_k[1]}</span>'
            + (f'<span class="stamp-bot">{_k[2]}</span>' if _k[2] else "") + '</div>')


def post_summary(p):
    """What the feed shows: enough to decide whether to read it, and nothing more.

    The full text lives on the permalink. Printing every post in full made the
    front page one enormous article where a visitor met a single finding per
    screen, and duplicated every word between / and /p/<id>.
    """
    pid = html.escape(p["id"])
    tags = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in p.get("tags", [])[:4])
    subject = ""
    if p.get("api", {}).get("name"):
        subject = f'<div class="sumsub">{html.escape(p["api"]["name"])}</div>'
    v = p.get("verdict") or {}
    badge = ""
    if v:
        call = v.get("call", "")
        cls = {"Honest": "ok", "Overcharged": "bad", "No goods": "bad"}.get(call, "warn")
        badge = (f'<div class="verdict v-{cls}"><span class="vcall">{html.escape(call)}</span>'
                 f'<span class="vdetail">quoted {html.escape(v.get("quoted", "?"))}'
                 f' &middot; charged {html.escape(v.get("charged", "?"))}</span></div>')
    lede = f'<p class="lede">{html.escape(p["lede"])}</p>' if p.get("lede") else ""
    return (f'<article class="post sum" id="{pid}">'
            f'<div class="phead"><a class="pdate" href="/p/{pid}">{html.escape(p.get("ts", ""))}</a>{tags}</div>'
            f'{stamp_html(p)}'
            f'<h2 class="ptitle"><a href="/p/{pid}">{html.escape(p.get("title", ""))}</a></h2>'
            f'{subject}{lede}{badge}<div class="clearfix"></div>'
            f'<a class="more" href="/p/{pid}">Read the full finding and receipts</a>'
            f'</article>')


def post_html(p, first=False):
    tags = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in p.get("tags", []))
    receipt = (f'<div class="receipt">Receipts: {html.escape(p["receipt"])}</div>'
               if p.get("receipt") else "")
    link = ""
    if p.get("link"):
        u = p["link"]["url"]
        lbl = p["link"].get("label") or re.sub(r"^https?://(www\.)?", "", u).split("/")[0]
        link = f'<div class="src">Source: <a href="{html.escape(u)}" rel="noopener">{html.escape(lbl)}</a></div>'
    # A receipt card: the machine-checkable facts of a payment, laid out so a
    # reader can verify every one of them without taking our word for it.
    stats = ""
    if p.get("stats"):
        cells = "".join(
            f'<div><dt>{html.escape(s["k"])}</dt><dd>'
            + (f'<a href="{html.escape(s["href"])}" rel="noopener">{html.escape(s["v"])}</a>'
               if s.get("href") else html.escape(s["v"]))
            + '</dd></div>'
            for s in p["stats"])
        stats = f'<dl class="statcard">{cells}</dl>'
    figure = ""
    if p.get("image"):
        im = p["image"]
        cap = (f'<figcaption>{html.escape(im["caption"])}</figcaption>'
               if im.get("caption") else "")
        figure = (f'<figure class="pfig"><img src="{html.escape(im["src"])}" '
                  f'alt="{html.escape(im.get("alt", ""))}" loading="lazy">{cap}</figure>')
    pull = ""
    if p.get("quote"):
        cite = p["quote"].get("cite", "")
        pull = (f'<blockquote class="pull"><p>{html.escape(p["quote"]["text"])}</p>'
                f'<cite>{html.escape(cite)}</cite></blockquote>')
    api_bar = ""
    if p.get("api"):
        _a = p["api"]
        # Only a real vendor gets a link. A study's subject is a label like
        # "6 trust services", and linking that anywhere sends the reader
        # somewhere that is not what the words say.
        _host = (f'<a href="{html.escape(_a["url"])}">{html.escape(_a["name"])}</a>'
                 if _a.get("url") and is_host(_a.get("name")) else
                 f'<span class="apisubject">{html.escape(_a.get("name", ""))}</span>')
        _path = f'<code>{html.escape(_a["path"])}</code>' if _a.get("path") else ""
        _purp = (f'<span class="apipurpose">{html.escape(p["endpoint"])}</span>'
                 if p.get("endpoint") else "")
        api_bar = f'<div class="apibar"><div class="apiline">{_host}{_path}</div>{_purp}</div>'
    req_block = ""
    if p.get("request"):
        _rq = p["request"]
        _who = f'<span class="rwho">{html.escape(_rq["chose"])}</span>' if _rq.get("chose") else ""
        req_block = (f'<div class="reqbox"><div class="reqhead">What I sent{_who}</div>'
                     f'<pre><code>{html.escape(_rq["raw"])}</code></pre></div>')

    # Payment log: one standard shape for every purchase, so entries can be
    # compared at a glance and the verdict is never buried in prose.
    if p.get("type") == "payment":
        v = p.get("verdict") or {}
        call = v.get("call", "")
        cls = {"Honest": "ok", "Overcharged": "bad", "No goods": "bad"}.get(call, "warn")
        badge = (f'<div class="verdict v-{cls}"><span class="vcall">{html.escape(call)}</span>'
                 f'<span class="vdetail">quoted {html.escape(v.get("quoted", "?"))}'
                 f' &middot; charged {html.escape(v.get("charged", "?"))}'
                 f' &middot; {"goods delivered" if v.get("delivered") else "nothing delivered"}</span></div>'
                 if v else "")
        if len(p.get("bullets", [])) > 3:
            print(f"  WARNING: {p['id']} has {len(p['bullets'])} bullets; hard cap is 3, extras dropped")
        bullets = ("<ul class=\"blist\">" + "".join(f"<li>{html.escape(b)}</li>"
                   for b in p.get("bullets", [])[:3]) + "</ul>") if p.get("bullets") else ""
        title = (f'<h2 class="ptitle"><span class="pnum">Payment #{p.get("n", "?"):02d}</span>'
                 f'{html.escape(p.get("title", ""))}</h2>')
        stamp = stamp_html(p)
        api = api_bar
        lede = f'<p class="lede">{html.escape(p["lede"])}</p>' if p.get("lede") else ""
        req = req_block
        repro = ""
        if p.get("repro"):
            r = p["repro"]
            note = f'<p class="rnote">{html.escape(r["note"])}</p>' if r.get("note") else ""
            repro = (f'<details class="repro"><summary>Run this payment yourself</summary>'
                     f'{note}<pre><code>{html.escape(r["cmd"])}</code></pre></details>')
        return f'''<article class="post pay" id="{html.escape(p["id"])}">
<div class="phead"><a class="pdate" href="/p/{html.escape(p["id"])}">{html.escape(p["ts"])}</a>{tags}</div>
{stamp}{title}{api}{lede}{badge}{bullets}<div class="clearfix"></div>
<details class="detail"{" open" if first else ""}><summary><span class="dlabel">Receipts and detail</span><span class="dhint">{"click to hide" if first else "click to expand"}</span></summary>
{req}{figure}{stats}{repro}{link}{receipt}
</details>
</article>'''

    body = p.get("text", "")
    paras = "".join(f"<p>{html.escape(t)}</p>" for t in body.split("\n\n") if t)
    if pull:
        # drop the quote in after the opening paragraph
        chunks = body.split("\n\n")
        paras = f"<p>{html.escape(chunks[0])}</p>" + pull + "".join(
            f"<p>{html.escape(t)}</p>" for t in chunks[1:])
    stamp2 = stamp_html(p)
    heading = f'<h2 class="ptitle">{html.escape(p["title"])}</h2>' if p.get("title") else ""
    lede_html = f'<p class="lede">{html.escape(p["lede"])}</p>' if p.get("lede") else ""
    blist = ""
    if p.get("bullets"):
        if len(p["bullets"]) > 3:
            print(f"  WARNING: {p['id']} has {len(p['bullets'])} bullets; hard cap is 3, extras dropped")
        blist = ("<ul class=\"blist\">"
                 + "".join(f"<li>{html.escape(b)}</li>" for b in p["bullets"][:3])
                 + "</ul>")
    tail = f"{req_block}{figure}{stats}{link}{receipt}"
    if tail.strip():
        hint = "click to hide" if first else "click to expand"
        tail = (f'<details class="detail"{" open" if first else ""}>'
                f'<summary><span class="dlabel">Receipts and detail</span>'
                f'<span class="dhint">{hint}</span></summary>{tail}</details>')
    return f'''<article class="post" id="{html.escape(p["id"])}">
<div class="phead"><a class="pdate" href="/p/{html.escape(p["id"])}">{html.escape(p["ts"])}</a>{tags}</div>
{stamp2}{heading}{api_bar}{lede_html}{paras}{blist}<div class="clearfix"></div>
{tail}
</article>'''


_warn_unnamed_subject(FEED)
posts_html = "".join(post_summary(p) for p in FEED)

_n_graded = len([x for x in FEED if x.get('grade') and x['grade'].upper() != 'UNGRADED'])

# The heroline explains what the numbers are and points at the newest rating.
# Generated, never hardcoded: it must not outlive the post it refers to.
_latest = next((x for x in FEED if x.get("grade") and x["grade"].upper() != "UNGRADED"), None)
_heroline = ("Every USDC payment agents make on Base and Solana in a day, read straight off the chain, "
             "plus the APIs I buy and grade myself.")

# The ledger is the reason the site exists, so it runs across the top rather than
# sitting behind a tab. Track is duplicated so the -50% loop is seamless.
# One item per service. A service graded twice shows both chips rather than
# appearing twice, which without a scope label would just read as a duplicate.
_tk_order, _tk_by = [], {}
for _p in FEED:
    _g = _p.get("grade")
    if not _g or _g.upper() == "UNGRADED":
        continue
    _n = (_p.get("api") or {}).get("name") or _p.get("title", "")
    if _n not in _tk_by:
        _tk_order.append(_n)
        _tk_by[_n] = {"href": _p["id"], "grades": []}
    _tk_by[_n]["grades"].append(_g)

_tk = []
for _n in _tk_order:
    _e = _tk_by[_n]
    _chips = "".join(f'<span class="tkg g{_g[0]}">{html.escape(_g)}</span>'
                      for _g in sorted(_e["grades"], key=grade_rank))
    _tk.append(
        f'<a class="tkitem" href="/p/{html.escape(_e["href"])}">'
        f'{_chips}<span class="tkn">{html.escape(_n)}</span></a>')
_ticker = ""
if _tk:
    _run = "".join(_tk)
    _ticker = (
        '<div class="ticker" aria-label="Ratings so far">'
        f'<div class="tkrail">{_run}{_run}</div></div>')

# The page should say what this market actually is before it says anything else.
_hero = ""
if LB:
    _w1 = LB["windows"]["1d"]
    _top = _w1["rows"][0] if _w1["rows"] else None
    _paid = len([r for r in FEED if (r.get("verdict") or {}).get("call")])
    # Real agent usage of the MCP, shown ONLY once it exists. A "0 agents" counter
    # would undercut the whole pitch, so it stays hidden until real (non-tooling)
    # agents actually call, then it becomes live social proof.
    _agentstat = (f'<div class="heronum"><b>{AGENT_WEEK_HITS:,}</b><span>agent calls this week</span></div>'
                  if AGENT_WEEK_HITS > 0 else "")
    # Two real columns: the measured numbers with their own provenance stamp, then
    # the sentence. The stamp belongs to the numbers, so it lives inside their column.
    _hero = (
        '<div class="hero">'
        '<div class="herostats">'
        '<div class="heronums">'
        f'<div class="heronum"><b>${_w1["total_usdc"]:,.0f}</b><span>settled in 24h</span></div>'
        f'<div class="heronum"><b>{_w1["total_settlements"]:,}</b><span>payments</span></div>'
        f'<div class="heronum"><b>{len(O):,}</b><span>services tracked</span></div>'
        f'<div class="heronum"><b>{_n_graded}</b><span>bought and graded</span></div>'
        f'{_agentstat}'
        '</div>'
        f'<div class="herostamp">measured on Base and Solana &middot; {LB["last_day"]}</div>'
        '</div>'
        f'<div class="heroline"><p>{_heroline} '
        '<a class="heromcp" href="#mcp">Building an agent? Free MCP server &rarr;</a></p></div>'
        '</div>')

# The agent-facing surface still lives on the front page, but at the bottom now,
# after a human has seen the actual findings, rather than intimidating them with a
# shell command up top. The quiet hero link jumps down to it.
_mcpbar = (
    '<div class="mcpbar" id="mcp">'
    '<div class="mcptext"><b>Using an agent?</b> Query every grade, price and '
    'demand score as <b>MCP tools</b>, so it can check a service before it pays. '
    'Free, no key, nothing to install.</div>'
    '<code class="mcpcmd">claude mcp add --transport http whatagentsbuy https://whatagentsbuy.com/mcp</code>'
    '<a class="mcplink" href="/api">The five tools, and the raw JSON</a>'
    '</div>')

compact_rows = []
for _p in FEED:
    _g = _p.get("grade")
    if _g and _g.upper() == "UNGRADED":
        _chip = '<span class="cg cg-u">&mdash;</span>'
    elif _g:
        _chip = f'<span class="cg g{_g[0]}">{html.escape(_g)}</span>'
    else:
        _chip = '<span class="cg cg-u"></span>'
    _v = _p.get("verdict") or {}
    _call = (f'<span class="cverdict v-{ {"Honest":"ok","Overcharged":"bad","No goods":"bad"}.get(_v.get("call"), "warn") }">'
             f'{html.escape(_v.get("call",""))}</span>') if _v.get("call") else ""
    _api = (_p.get("api") or {}).get("name", "")
    compact_rows.append(
        f'<a class="crow" href="/p/{html.escape(_p["id"])}">'
        f'{_chip}'
        f'<span class="ctitle">{html.escape(_p.get("title") or _p.get("text","")[:70])}</span>'
        f'<span class="capi">{html.escape(_api)}</span>'
        f'{_call}'
        f'<span class="cdate">{html.escape(_p["ts"].split(" ")[0])}</span>'
        '</a>')
compact_html = "".join(compact_rows)



# ---------------- ratings ledger ----------------
# Every GRADE belongs in the ratings table, not only the ones that completed a
# payment. Filtering on type=="payment" was silently hiding the F and the B-.
pays = [p for p in FEED if p.get("grade") and p["grade"].upper() != "UNGRADED"]
pays_sorted = sorted(pays, key=lambda p: (grade_rank(p.get("grade")), p.get("ts", "")))

# One grading system on the site. The leaderboard used to print probe scores in the
# same A-F letters as the buy grades, so x402.boats showed B there and D in the feed.
# A grade now means one thing: what happened when we paid.
BOUGHT_GRADE = {}
for _p in pays:
    _n = (_p.get("api") or {}).get("name")
    if not _n:
        continue
    BOUGHT_GRADE.setdefault(_n, []).append(_p["grade"])
for _n in BOUGHT_GRADE:
    BOUGHT_GRADE[_n] = sorted(BOUGHT_GRADE[_n], key=grade_rank)
CALL_CLS = {"Honest": "ok", "Overcharged": "bad", "No goods": "bad", "Partial": "warn"}
led_rows = []
for p in pays_sorted:
    v = p.get("verdict") or {}
    g = p.get("grade", "")
    letter = g[0] if g else "?"
    # api.name is the reliable source; the stats grid does not always carry an "Api" key,
    # and falling through to the headline printed a whole sentence in the Service column.
    svc = ((p.get("api") or {}).get("name")
           or next((s["v"] for s in (p.get("stats") or []) if s["k"].lower() == "api"), "")
           or p.get("title", ""))
    scope = p.get("graded", "")
    if v.get("call"):
        verdict = f'<span class="vtag v-{CALL_CLS.get(v["call"], "warn")}">{html.escape(v["call"])}</span>'
        goods = "yes" if v.get("delivered") else "no"
    elif p.get("free"):
        # Called and answered, just never billed. Not the same as never bought.
        verdict = '<span class="dim">free, no charge</span>'
        goods = "yes"
    else:
        verdict = '<span class="dim">not purchased</span>'
        goods = '<span class="dim">&mdash;</span>'
    led_rows.append(f'''<tr>
<td class="l-g"><span class="g g{letter}">{html.escape(g)}</span></td>
<td class="l-s"><a href="/p/{html.escape(p["id"])}">{html.escape(svc)}</a>
<span class="sub">{html.escape(p.get("endpoint") or p.get("title", ""))}</span></td>
<td class="l-sc">{html.escape(scope)}</td>
<td class="l-v">{verdict}</td>
<td class="num">{html.escape(v["quoted"]) if v.get("quoted") else "&mdash;"}</td>
<td class="num">{html.escape(v["charged"]) if v.get("charged") else "&mdash;"}</td>
<td class="num">{goods}</td>
<td class="num dim">{html.escape(p["ts"].split(" ")[0])}</td>
</tr>''')
_calls = [(p.get("verdict") or {}).get("call") for p in pays]
honest = _calls.count("Honest")
_failed = len([c for c in _calls if c and c != "Honest"])
_freeb = len([p for p in pays if not (p.get("verdict") or {}).get("call") and p.get("free")])
_nobuy = len([p for p in pays if not (p.get("verdict") or {}).get("call") and not p.get("free")])
_bits = [f'{len(pays)} rating{"s" if len(pays) != 1 else ""} across '
         f'{len({(p.get("api") or {}).get("name") for p in pays})} services']
if honest:
    _bits.append(f'{honest} paid and honest')
if _failed:
    _bits.append(f'{_failed} failed on delivery or price')
if _freeb:
    _bits.append(f'{_freeb} free to call')
if _nobuy:
    _bits.append(f'{_nobuy} graded without a purchase')
tally = " &middot; ".join(_bits)

# Structured data. Two things are being published here and search engines treat
# them differently: a measured Dataset, and a series of reviews with verdicts.
_ld_items = []
for _i, _p in enumerate(pays_sorted[:20], 1):
    _v = _p.get("verdict") or {}
    _svc = (_p.get("api") or {}).get("name") or _p.get("title", "")
    _item = {
        "@type": "Review",
        "position": _i,
        "url": f'{SITE}/p/{_p["id"]}',
        "name": _p.get("title", ""),
        "datePublished": _p["ts"].split(" ")[0],
        "author": PERSON,
        "itemReviewed": {"@type": "WebAPI", "name": _svc,
                         "description": _p.get("endpoint") or ""},
    }
    if _p.get("grade"):
        _item["reviewRating"] = {"@type": "Rating", "ratingValue": _p["grade"],
                                 "bestRating": "A+", "worstRating": "F"}
    if _v.get("call"):
        _item["reviewBody"] = f'{_v["call"]}. Quoted {_v.get("quoted")}, charged {_v.get("charged")}.'
    _ld_items.append(_item)

_ld = {"@context": "https://schema.org", "@graph": [
    {"@type": "Dataset",
     "name": "What Agents Buy — x402 settlement tape",
     "description": ("Daily USDC settlement on Base for x402 endpoints, swept directly from chain "
                     "logs and joined to the seller address each service asks buyers to pay."),
     "url": SITE,
     "creator": PERSON,
     "license": "https://creativecommons.org/licenses/by/4.0/",
     "isAccessibleForFree": True,
     "measurementTechnique": "Base JSON-RPC USDC Transfer logs, swept per seller address",
     "temporalCoverage": (f'{LB["first_day"]}/{LB["last_day"]}' if LB else ""),
     "variableMeasured": ["USDC settled", "settlement count", "distinct paying wallets",
                          "average ticket", "share paid back out"]},
    {"@type": "ItemList", "name": "Endpoint ratings",
     "numberOfItems": len(_ld_items), "itemListElement": _ld_items},
]}
_jsonld = json.dumps(_ld, separators=(",", ":"))

# --- new endpoints, newest first ---------------------------------------------
_by_day = {}
for _r in NEWEST:
    _by_day.setdefault(_r["date"], []).append(_r)

_new_days = []
for _d in sorted(_by_day, reverse=True):
    _rows = sorted(_by_day[_d], key=lambda r: (not r["new_service"], -len(r["endpoints"])))
    _cards = []
    for _r in _rows:
        _eps = sorted(_r["endpoints"], key=lambda e: -(e.get("calls30d") or 0))
        _lines = []
        for _e in _eps[:6]:
            _txt = (_e.get("desc") or "").strip()
            _txt = (_txt[:118] + "...") if len(_txt) > 118 else (_txt or "No description published.")
            _c = f'<span class="epuse">{_e["calls30d"]:,}</span>' if _e.get("calls30d") else ""
            _lines.append(
                '<li><code>' + html.escape(_e["path"][:56]) + '</code>'
                '<span class="epdesc">' + html.escape(_txt) + '</span>' + _c + '</li>')
        _more = ('<li class="epmore">and ' + str(len(_eps) - 6) + ' more</li>') if len(_eps) > 6 else ""
        _badge = '<span class="nbadge">new service</span>' if _r["new_service"] else ""
        _n = len(_eps)
        # The service and what it sells is the scannable part. The per-endpoint
        # paths and parameters go behind a disclosure so the list stays readable.
        _svcdesc = (SERVICES.get(_r["host"], {}) or {}).get("desc") or ""
        if not _svcdesc and _eps:
            _svcdesc = (_eps[0].get("desc") or "")
        _svcdesc = _svcdesc[:180] + ("..." if len(_svcdesc) > 180 else "")
        _cards.append(
            '<article class="ncard">'
            '<div class="nhead"><span class="nsvc">' + html.escape(_r["service"]) + '</span>' + _badge +
            '<span class="ncount">' + str(_n) + ' paid endpoint' + ("s" if _n != 1 else "") + '</span></div>'
            '<div class="nhost">' + html.escape(_r["host"]) + '</div>'
            + ('<p class="ndesc">' + html.escape(_svcdesc) + '</p>' if _svcdesc else "")
            + '<details class="neps"><summary>Endpoints</summary>'
              '<ul class="eplist">' + "".join(_lines) + _more + '</ul></details>'
            '</article>')
    _new_days.append(f'<h3 class="nday">{html.escape(_d)}</h3>' + "".join(_cards))

newest_html = ("".join(_new_days) if _new_days
               else '<p class="note">Nothing recorded yet. Run whatsnew.py.</p>')
_tot_new = sum(len(r["endpoints"]) for r in NEWEST)
_tot_svc = sum(1 for r in NEWEST if r["new_service"])

# A missed run cannot be backfilled: sweep.py reads a rolling 24h window. Name any
# gap on the page instead of printing a range that implies unbroken coverage.
import datetime as _dt
_tape_have = sorted(
    f[len("settlements_"):-len(".json")]
    for f in os.listdir(os.path.join(HERE, "data", "history"))
    if f.startswith("settlements_") and "solana" not in f) if os.path.isdir(os.path.join(HERE, "data", "history")) else []
_tape_days = len(_tape_have)
_tape_gap_note = ""
if len(_tape_have) >= 2:
    _d0 = _dt.date.fromisoformat(_tape_have[0]); _d1 = _dt.date.fromisoformat(_tape_have[-1])
    _want = {(_d0 + _dt.timedelta(n)).isoformat() for n in range((_d1 - _d0).days + 1)}
    _missing = sorted(_want - set(_tape_have))
    if _missing:
        _tape_gap_note = (", with no sweep on " + ", ".join(_missing) +
                          " (a missed day cannot be recovered, the sweep reads a rolling 24 hours)")

# --- revenue leaderboard ------------------------------------------------------
def _lb_table(win):
    rows = win["rows"]
    if not rows:
        return '<p class="note">No settlement recorded in this window.</p>'
    out = []
    for i, r in enumerate(rows[:40], 1):
        # Only grades earned by paying appear here. A probe score is a different
        # measurement and printing it in the same letters made the site contradict itself.
        _bg = BOUGHT_GRADE.get(r["host"]) or []
        grade = ("".join(f'<span class="g g{g[0]}">{html.escape(g)}</span>' for g in _bg)
                 if _bg else '<span class="lbdash">&mdash;</span>')
        shared = ' <span class="lbshare" title="several listings share this wallet">shared wallet</span>' if r.get("shared_wallet") else ""
        tick = f'${r["avg_ticket"]:,.4f}' if r.get("avg_ticket") else "&mdash;"
        # the shape of the money in one word, with the concentration behind it
        _d = r.get("demand")
        _t1 = r.get("top_payer_share")
        _dt = f' title="top buyer holds {_t1*100:.0f}% of the dollars"' if _t1 is not None else ""
        dem = (f'<span class="dem d-{_d.replace(chr(32), chr(45))}"{_dt}>{html.escape(_d)}</span>'
               if _d else '<span class="dim">&mdash;</span>')
        # Organic Demand Score: one number for the shape of the demand, with the
        # three components in the tooltip so it can always be taken apart.
        _os, _op = r.get("organic_score"), (r.get("organic_parts") or {})
        if _os is None:
            ods = '<span class="dim">&mdash;</span>'
        else:
            _oc = "ods-hi" if _os >= 70 else ("ods-mid" if _os >= 40 else "ods-lo")
            _ot = (f'breadth {_op.get("breadth")}/40 &middot; spread {_op.get("dispersion")}/40 '
                   f'&middot; repeat {_op.get("repeat")}/20 &middot; '
                   f'{r.get("organic_confidence") or "?"} confidence')
            ods = f'<span class="ods {_oc}" title="{_ot}">{_os}</span>'
        if r.get("payout_pct") is None:
            payout = '<span class="dim">&mdash;</span>'
        elif r.get("circular"):
            payout = f'<span class="lbcirc">{r["payout_pct"]:.0f}%</span>'
        else:
            payout = f'<span class="dim">{r["payout_pct"]:.0f}%</span>' 
        # A Solana-only row has no Base address, so there is no Basescan wallet to
        # link and r["address"] is None. Guard both the label and the link.
        _addr = r.get("address")
        _wallet = (f' &middot; <a href="https://basescan.org/address/{html.escape(_addr)}">wallet</a>'
                   if _addr else '')
        _label = r["host"] or (_addr[:14] if _addr else r["service"])
        out.append(
            '<tr>'
            f'<td class="lbrank">{i}</td>'
            f'<td class="lbg">{grade}</td>'
            f'<td class="lbsvc"><span class="lbname">{html.escape(r["service"])}</span>'
            f'<span class="sub">{html.escape(_label)}{shared}{_wallet}</span>'
            f'<span class="lbdesc">{html.escape((SERVICES.get(r.get("host"), {}) or {}).get("desc") or "")}</span></td>'
            f'<td class="num lbrev">${r["usdc"]:,.2f}</td>'
            f'<td class="num">{r["payers"]}</td>'
            f'<td class="num">{r.get("repeat_payers", 0)}</td>'
            f'<td class="num">{dem}</td>'
            f'<td class="num">{ods}</td>'
            f'<td class="num">{payout}</td>'
            '</tr>')
    return ('<div class="scroll"><table class="tbl lbtbl"><thead><tr>'
            '<th></th><th>Grade</th><th>Service</th><th>Received</th>'
            '<th>Buyers</th><th>Repeat</th><th>Demand</th>'
            '<th title="Organic Demand Score, 0 to 100">Organic</th><th>Sends back</th>'
            '</tr></thead><tbody>' + "".join(out) + '</tbody></table></div>')


if LB:
    _panels, _btns = [], []
    for _lab, _title in (("1d", "24 hours"), ("7d", "7 days"), ("30d", "30 days")):
        _w = LB["windows"][_lab]
        _sel = "true" if _lab == "1d" else "false"
        _btns.append(f'<button class="chip" data-w="{_lab}" aria-pressed="{_sel}">{_title}</button>')
        if _w["complete"]:
            _note = (f'<p class="note">${_w["total_usdc"]:,.2f} settled across '
                     f'{_w["total_settlements"]:,} payments in the last {_title}.</p>')
        else:
            _note = (f'<p class="note lbfill">{_w["days_have"]} of {_w["days_wanted"]} days collected. '
                     f'This window is still filling, so it currently shows '
                     f'{_w["days_have"]} day{"s" if _w["days_have"] != 1 else ""} of data '
                     f'(${_w["total_usdc"]:,.2f} across {_w["total_settlements"]:,} payments), not a full {_title}.</p>')
        _panels.append(f'<div class="lbpanel" data-w="{_lab}"{"" if _lab == "1d" else " hidden"}>'
                       + _note + _lb_table(_w) + '</div>')
    leaderboard_html = ('<p class="note"><b>This ranks money, not quality.</b> Services are ordered by USDC '
                        'actually received, so appearing at the top says a service is busy, not that it is good. '
                        'Grades come from buying, and a dash simply means we have not bought from that one yet.</p>'
                        '<p class="note dim"><b>Demand</b> is the shape of the money, not its size. '
                        '<span class="dem d-broad">broad</span> means 20 or more buyers with no single one above '
                        '35% of the dollars. <span class="dem d-one-wallet">one wallet</span> means a single wallet '
                        'holds 90% or more. <span class="dem d-thin">thin</span> means five buyers or fewer. '
                        'Hover a label for the top buyer&rsquo;s exact share. This is a flag, not an accusation: one '
                        'large genuine customer looks identical to a wallet paying itself, and this measurement '
                        'cannot tell them apart. These are counts of <b>wallets</b>, not of people or companies: '
                        'one wallet may be one customer, a fleet, or a seller paying itself.</p>'
                        '<p class="note dim"><b>Organic Demand Score</b> is that shape as one number from 0 to 100, '
                        'built from three things and nothing else: <b>breadth</b>, how many distinct wallets paid, '
                        'up to 40 points on a log scale that tops out at 100 wallets; <b>spread</b>, how evenly the '
                        'dollars fell across them, up to 40 points from the Herfindahl index; and <b>repeat</b>, what '
                        'share of buyers came back, up to 20 points, topping out at 30%. Hover any score to see the '
                        'three parts and how much data stands behind it. '
                        'Revenue is deliberately excluded, because a large number is not evidence that anyone wanted '
                        'the product. So is outflow, because money leaving is as often cost of goods as it is '
                        'recycling; that stays in <b>Sends back</b> as its own flag. '
                        '<b>A high score is not a clean bill of health.</b> These are the exact quantities a wash '
                        'trader would optimise, and the highest scoring service on this board currently pays out more '
                        'than it takes in. Read the score next to that column, never on its own.</p>'
                        + '<div class="ctrl">' + "".join(_btns) + '</div>' + "".join(_panels)
                        + f'<p class="note dim">Revenue is USDC that actually landed on Base and Solana at the address each '
                          f'service asks buyers to pay, swept daily. Tape covers {_tape_days} day'
                          f'{"s" if _tape_days != 1 else ""} between {LB["first_day"]} and {LB["last_day"]}'
                          f'{_tape_gap_note}. '
                          f'Every grade shown anywhere on this site was earned by paying the service and recording '
                          f'what happened; nothing here is scored by an automated probe.</p>')
else:
    leaderboard_html = '<p class="note">No leaderboard data yet. Run leaderboard.py.</p>'

# One row per service, carrying every grade it holds. Listing each grade on its
# own row needed a scope label to explain the repeat, and the labels read as noise.
_rail_order, _rail_by = [], {}
for _p in FEED:
    if not _p.get("grade"):
        continue
    _nm = (_p.get("api") or {}).get("name") or _p.get("title", "")
    if _nm not in _rail_by:
        _rail_order.append(_nm)
        _rail_by[_nm] = {"href": _p["id"], "grades": []}
    _rail_by[_nm]["grades"].append(_p["grade"])

_rail_items = []
for _nm in _rail_order:
    _e = _rail_by[_nm]
    _chips = "".join(
        f'<span class="rg {"cg-u" if _g.upper() == "UNGRADED" else f"g{_g[0]}"}">'
        f'{"&mdash;" if _g.upper() == "UNGRADED" else html.escape(_g)}</span>'
        for _g in sorted(_e["grades"], key=grade_rank))
    _rail_items.append(
        f'<a class="railrow" href="/p/{html.escape(_e["href"])}">'
        f'<span class="rgs">{_chips}</span><span class="rn">{html.escape(_nm)}</span></a>')
rail_ledger = "".join(_rail_items) or '<p class="railempty">nothing graded yet</p>'

rail_notes = "".join(
    f'''<a class="railrow" href="/notes/{html.escape(n["id"])}"><span class="rn">{html.escape(n["trap"])}</span></a>'''
    for n in NOTES[:6])

notes_html = "".join(
    f'''<article class="fnote" id="note-{html.escape(n["id"])}">
<h3><a href="/notes/{html.escape(n["id"])}" style="color:inherit;text-decoration:none">{html.escape(n["trap"])}</a></h3>
<p class="fbite">{html.escape(n["bite"])}</p>
<p class="fdo"><b>What to do:</b> {html.escape(n["do"])}</p>
<p class="fev">{html.escape(n["evidence"])}</p>
</article>''' for n in NOTES)

ledger_html = (f'<p class="note">{tally}. Where money changed hands the charge is checkable on Base; '
               'nothing is graded that was not called. Click a service for the receipt.</p>'
               '<div class="scroll"><table class="tbl"><thead><tr><th>Grade</th><th>Service</th>'
               '<th>Graded on</th><th>Verdict</th><th>Quoted</th><th>Charged</th><th>Goods</th>'
               '<th>Date</th></tr></thead>'
               f'<tbody>{"".join(led_rows)}</tbody></table></div>') if pays else '<p class="note">No payments logged yet.</p>'

# ---------------- rss ----------------
def rss():
    items = []
    # RSS carries the most recent RSS_ITEMS posts. The cap is real, so
    # audit_coverage() checks the same slice rather than the whole feed; before
    # this was named, crossing thirty posts silently dropped the oldest out of
    # the feed and the audit caught it on the next build.
    for p in FEED[:RSS_ITEMS]:
        try:
            import datetime as _dt
            pub = email.utils.format_datetime(_dt.datetime.fromisoformat(p["ts_utc"]))
        except Exception:
            pub = email.utils.formatdate()
        # The headline, not the first sentence of the lede. This used to fall
        # through to `body.split(". ")[0][:90]` for anything that was not a
        # numbered payment, so every study arrived in a reader as a truncated
        # fragment of its own opening paragraph.
        title = p.get("title") or (p.get("lede", "") or "").split(". ")[0][:90]
        if p.get("type") == "payment" and p.get("n"):
            title = f'Payment #{p["n"]:02d}: {title}'
        # The permalink, not a homepage anchor. The front page carries summaries
        # now, so an anchor sends a subscriber somewhere the full text is not.
        url = f"{SITE}/p/{p['id']}"
        summary = p.get("lede") or (p.get("text") or "").split("\n\n")[0]
        items.append(f"""<item>
<title>{html.escape(title)}</title>
<link>{url}</link>
<guid isPermaLink="true">{url}</guid>
<pubDate>{pub}</pubDate>
<description>{html.escape(summary)}</description>
</item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>What Agents Buy — the agent economy, measured</title>
<link>{SITE}</link>
<description>Live notes on the agent economy, measured on-chain. Original settlement data, entity mapping, and reads on the market as it forms.</description>
{''.join(items)}
</channel></rss>"""


# <meta charset> must come first and inside the first 1024 bytes. Without it the
# browser guesses a legacy encoding and every UTF-8 character on the page renders
# as mojibake — the em-dash below included. (Found 2026-08-05: the stat card's
# ellipses were showing as "â€¦".)
CSS = f'''
/* Light by default, on purpose. This reads as a publication rather than a tool,
   and it no longer flips to dark just because the reader's OS is set that way.
   Dark remains available, but only when explicitly chosen.
   Cool greys rather than warm beige; chip colours are deepened so white text on
   them actually passes contrast, which the brighter originals did not. */
:root{{color-scheme:light;--bg:#fbfbfc;--card:#ffffff;--ink:#15171a;--ink2:#5c6470;--line:#e7e9ed;
--good:#15803d;--warn:#b45309;--ser:#c2410c;--crit:#b42318;--accent:#1d5fd0;}}
:root[data-theme=dark]{{color-scheme:dark;--bg:#0d0d0d;--card:#1a1a19;--ink:#fff;--ink2:#c3c2b7;--line:#2e2e2c;
--good:#22a94e;--warn:#fab219;--ser:#ec835a;--crit:#e05252;--accent:#5c9dff;}}
*{{box-sizing:border-box}}
html,body{{max-width:100%;overflow-x:hidden}}
pre,code,table,img{{max-width:100%}}
.post,.ask,.method,.find{{min-width:0}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}}
.wrap{{max-width:1240px;margin:0 auto;padding:24px 20px 60px}}
header{{margin-bottom:10px}}
h1{{font-size:27px;line-height:1;margin:0 0 5px;letter-spacing:-.025em}}
h1 .dot{{color:var(--accent)}}
.tagline{{margin:0;font-size:14px;line-height:1.45;color:var(--ink2);max-width:44em}}
.subline{{margin:4px 0 0;font-size:13px;line-height:1.45;color:var(--ink2);opacity:.85;max-width:44em}}
.subline b{{color:var(--ink);font-weight:640}}
/* Masthead: prose keeps its readable measure on the left, and the right column
   carries the standing facts, so the header spans the page instead of hugging
   the left edge while everything below it runs full width. */
.masthead{{display:flex;flex-wrap:wrap;gap:12px 40px;align-items:flex-start;justify-content:space-between;
padding-bottom:12px;border-bottom:1px solid var(--line)}}
.mh-l{{flex:1 1 auto;min-width:0}}
.mh-r{{flex:0 0 auto;max-width:260px;text-align:right;padding-top:7px}}
.mh-r p{{margin:0 0 4px;font-size:12.5px;color:var(--ink2);line-height:1.5}}
.mh-by b{{color:var(--ink);font-weight:650}}
.mh-note{{opacity:.72;font-size:11.5px!important;margin-top:8px!important}}
@media(max-width:820px){{.masthead{{flex-direction:column;gap:14px}}
.mh-r{{max-width:none;text-align:left;padding-top:0}}}}
.searchbar{{position:relative;margin:20px 0 8px}}
#q{{width:100%;padding:14px 16px 14px 48px;border:2px solid #4361d0;border-radius:12px;
background:var(--card) url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='18' height='18' viewBox='0 0 24 24' fill='none' stroke='%235b6bb5' stroke-width='2.2' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cline x1='21' y1='21' x2='16.5' y2='16.5'/%3E%3C/svg%3E") no-repeat 17px center;
background-size:19px;color:var(--ink);font-size:16px;box-shadow:0 1px 3px rgba(41,82,204,.10);
transition:border-color .18s ease, box-shadow .22s ease, transform .18s ease, background-color .18s ease}}
#q::placeholder{{color:var(--ink2);opacity:.85}}
#q:hover{{box-shadow:0 2px 10px rgba(41,82,204,.16)}}
#q:focus{{outline:none;border-color:#2952cc;box-shadow:0 0 0 4px rgba(41,82,204,.15)}}
.searchbar.searching #q{{border-color:#2952cc;transform:translateY(-1px);
box-shadow:0 0 0 4px rgba(41,82,204,.16), 0 10px 26px rgba(41,82,204,.16);
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='18' height='18' viewBox='0 0 24 24' fill='none' stroke='%232952cc' stroke-width='2.4' stroke-linecap='round'%3E%3Ccircle cx='11' cy='11' r='7'/%3E%3Cline x1='21' y1='21' x2='16.5' y2='16.5'/%3E%3C/svg%3E")}}
.qresults{{position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:40;background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:0 12px 34px rgba(41,82,204,.13),0 2px 8px rgba(0,0,0,.06);max-height:64vh;overflow-y:auto;animation:qpop .17s cubic-bezier(.2,.7,.3,1)}}
@keyframes qpop{{from{{opacity:0;transform:translateY(-7px)}}to{{opacity:1;transform:translateY(0)}}}}
.qrow{{display:flex;align-items:center;gap:11px;padding:10px 14px;cursor:pointer;border-bottom:1px solid var(--line);text-decoration:none;color:inherit;transition:background .12s ease}}
.qrow:last-child{{border-bottom:none}}
.qrow:hover,.qrow.sel{{background:rgba(41,82,204,.06)}}
.qmain{{flex:1 1 auto;min-width:0;display:flex;gap:9px;align-items:baseline}}
.qname{{flex:0 0 auto;font-weight:650;font-size:14px;max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.qsub{{flex:1 1 auto;min-width:0;font-size:12.5px;color:var(--ink2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.qbadge{{flex:0 0 auto;font-size:10px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:3px 7px;border-radius:5px}}
.qb-graded{{background:#e7f6ec;color:#15803d}}
.qb-measured{{background:#e8eefb;color:#2952cc}}
.qb-listed{{background:#ededed;color:#666}}
.qb-post{{background:#f3ecfb;color:#7a3ecc}}
.qb-note{{background:#f7f0e6;color:#9a6a1a}}
.qmeta{{flex:0 0 auto;font-size:11.5px;color:var(--ink2);font-variant-numeric:tabular-nums;white-space:nowrap}}
.qempty{{padding:14px;color:var(--ink2);font-size:13px}}
.qmore{{padding:8px 14px;font-size:12px;font-weight:600;color:var(--accent);border-top:1px solid var(--line);background:rgba(41,82,204,.04)}}
.qhint{{padding:8px 13px;font-size:11px;color:var(--ink2);border-top:1px solid var(--line);background:var(--bg)}}
.guide{{max-width:720px}}
.guide h2{{margin:0 0 10px;font-size:27px;letter-spacing:-.02em}}
.guide h3{{margin:26px 0 8px;font-size:17px;letter-spacing:-.01em}}
.guide p,.guide li{{font-size:15px;line-height:1.62;color:var(--ink2)}}
.guide b{{color:var(--ink)}}
.glede{{font-size:16px!important;color:var(--ink)!important}}
.gsteps,.gtraps{{margin:0;padding-left:20px}}
.gsteps li,.gtraps li{{margin:0 0 9px}}
.guide code{{background:var(--card);border:1px solid var(--line);border-radius:4px;padding:1px 5px;
font-size:13px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
.gclose{{margin-top:28px;padding-top:16px;border-top:1px solid var(--line)}}
.ckh{{margin:26px 0 8px;font-size:11.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--ink2)}}
.cklist{{list-style:none;margin:0;padding:0}}
.cklist li{{display:flex;gap:12px;align-items:baseline;padding:9px 0;border-top:1px solid var(--line)}}
.cknum{{flex:0 0 22px;font-size:12px;font-weight:700;color:var(--ink2);font-variant-numeric:tabular-nums}}
.cktext{{flex:1;font-size:15px;line-height:1.5;color:var(--ink)}}
.cktext a{{font-size:12.5px;text-transform:uppercase;letter-spacing:.05em;text-decoration:none;white-space:nowrap}}
.related{{margin:26px 0 0;padding:16px 18px;border:1px solid var(--line);border-radius:8px;background:var(--card);max-width:860px}}
.related h3{{margin:0 0 9px;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink2)}}
.related ul{{margin:0;padding-left:18px}}
.related li{{margin:0 0 6px;font-size:14px;line-height:1.45}}
.related p{{margin:12px 0 0;font-size:13px;color:var(--ink2)}}
.svcpage{{max-width:820px}}
.svchead{{display:flex;gap:16px;align-items:flex-start;margin:0 0 16px}}
.svcgrades{{display:flex;gap:5px;flex:0 0 auto;padding-top:4px}}
.svchead h2{{margin:0;font-size:26px;letter-spacing:-.02em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
.svcdesc{{margin:5px 0 0;font-size:14.5px;color:var(--ink2);line-height:1.5}}
.svcfacts{{margin:0 0 22px}}
.svcfacts>div{{display:flex;flex-direction:column;gap:2px}}
.svcfacts b{{font-size:16px;font-weight:660;letter-spacing:-.01em;font-variant-numeric:tabular-nums;line-height:1.2}}
.svcfacts span{{font-size:11px;text-transform:uppercase;letter-spacing:.055em;color:var(--ink2)}}
.svch3{{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink2)}}
.svcrow{{display:flex;gap:12px;align-items:center;padding:12px 14px;border:1px solid var(--line);
border-radius:8px;margin:0 0 7px;text-decoration:none;color:var(--ink);background:var(--card)}}
.svcrow:hover{{border-color:var(--accent)}}
.svcmain{{flex:1;min-width:0}}
.svcmain b{{display:block;font-size:14.5px;font-weight:640;line-height:1.35}}
.svcmain .sub{{display:block;font-size:11.5px;color:var(--ink2);text-transform:uppercase;letter-spacing:.05em;margin-top:2px}}
.byline{{font-size:13px;color:var(--ink2);margin:0}}
.byline a{{color:var(--accent);text-decoration:none}}
nav.tabs{{display:flex;gap:2px;border-bottom:1px solid var(--line);margin:14px 0 20px;overflow-x:auto}}
nav.tabs a{{border-bottom:2px solid transparent;padding:8px 13px;font-size:14.5px;color:var(--ink2);
white-space:nowrap;font-weight:500;text-decoration:none}}
nav.tabs a:hover{{color:var(--ink)}}
nav.tabs a[aria-current=page]{{color:var(--ink);border-bottom-color:var(--accent);font-weight:650}}
/* On a phone the row used to scroll sideways and cut off after the third tab,
   which hid half the site behind a swipe nobody attempts. Wrap it instead, and
   give each one a border so they read as buttons rather than as quiet labels.
   This block must stay AFTER the desktop rules: same specificity, later wins. */
@media(max-width:820px){{
nav.tabs{{flex-wrap:wrap;overflow-x:visible;gap:7px;border-bottom:0;margin:14px 0 18px}}
nav.tabs a{{border:1px solid var(--line);border-radius:7px;
background:var(--card);padding:8px 12px;font-size:14px;color:var(--ink)}}
nav.tabs a[aria-current=page]{{background:var(--ink);border-color:var(--ink);color:var(--bg);
font-weight:650;border-bottom-color:var(--ink)}}
}}
section[hidden]{{display:none}}
.post{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 20px;margin:0 0 12px;box-shadow:0 1px 2px rgba(20,24,31,.03)}}
.post p{{margin:7px 0 0;font-size:15px}}
.phead{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}}
.pdate{{font-size:12.5px;color:var(--ink2);text-decoration:none;font-variant-numeric:tabular-nums}}
.pdate:hover{{color:var(--accent)}}
.tag{{font-size:11px;color:var(--ink2);border:1px solid var(--line);border-radius:999px;padding:1px 8px}}
.pfig{{margin:14px 0 0;padding:0}}
.pfig img{{display:block;width:100%;max-width:420px;height:auto;border:1px solid var(--line);border-radius:10px}}
.pfig figcaption{{margin-top:7px;font-size:12.5px;color:var(--ink2)}}
.statcard{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1px;margin:14px 0 0;max-width:100%;
padding:0;background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
.statcard>div{{background:var(--card);padding:9px 12px;min-width:0}}
.statcard dt{{font-size:10.5px;text-transform:uppercase;letter-spacing:.055em;color:var(--ink2)}}
.statcard dd{{margin:2px 0 0;font-size:13.5px;font-variant-numeric:tabular-nums;
overflow-wrap:anywhere;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
.statcard dd a{{color:var(--accent);text-decoration:none}}
.statcard dd a:hover{{text-decoration:underline}}
.receipt{{margin-top:10px;font-size:12.5px;color:var(--ink2);border-top:1px dashed var(--line);padding-top:8px}}
.src{{margin-top:10px;font-size:13px;color:var(--ink2)}}
.pull{{margin:14px 0;padding:14px 18px;border-left:3px solid var(--accent);background:rgba(127,127,127,.07);border-radius:0 8px 8px 0}}
.pull p{{margin:0;font-size:17px;line-height:1.5;font-style:italic}}
.pull cite{{display:block;margin-top:7px;font-size:12.5px;color:var(--ink2);font-style:normal}}
.ptitle{{font-size:19px;margin:6px 0 6px;letter-spacing:-.01em;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}}
.pnum{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--ink2);border:1px solid var(--line);border-radius:5px;padding:2px 7px;font-weight:600;letter-spacing:0}}
.post .lede{{margin:0 0 9px;font-size:15.5px}}
.apibar{{margin:0 0 8px;padding-bottom:7px;border-bottom:1px solid var(--line)}}
.apiline{{display:flex;flex-wrap:wrap;align-items:baseline;gap:7px}}
.apisubject{{font-weight:640;color:var(--ink)}}
.apibar a{{font-weight:650;text-decoration:none}}
.apibar a:hover{{text-decoration:underline}}
.apibar code{{font-size:12px;background:rgba(127,127,127,.13);padding:1px 6px;border-radius:4px;color:var(--ink2)}}
.apipurpose{{display:block;font-size:13px;color:var(--ink2);margin-top:4px}}
.verdict{{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;border-radius:7px;padding:7px 11px;margin:0 0 9px;border:1px solid}}
.verdict .vcall{{font-weight:700;font-size:14.5px}}
.verdict .vdetail{{font-size:13px;color:var(--ink2);font-variant-numeric:tabular-nums}}
.v-ok{{border-color:var(--good)}}.v-ok .vcall{{color:var(--good)}}
.v-bad{{border-color:var(--crit)}}.v-bad .vcall{{color:var(--crit)}}
.v-warn{{border-color:var(--warn)}}.v-warn .vcall{{color:var(--warn)}}
.detail{{margin-top:4px}}
.detail>summary{{cursor:pointer;list-style:none;font-size:13.5px;font-weight:600;color:var(--accent);padding:8px 12px;border:1px solid var(--line);border-radius:8px;display:flex;justify-content:space-between;gap:10px;align-items:baseline;background:rgba(127,127,127,.05)}}
.detail>summary:hover{{border-color:var(--accent)}}
.dhint{{font-weight:400;font-size:12px;color:var(--ink2)}}
.detail>summary::-webkit-details-marker{{display:none}}
.dlabel::before{{content:"▸ ";font-size:11px}}
.detail[open] .dlabel::before{{content:"▾ "}}
.blist{{margin:8px 0 10px;padding-left:19px}}
.blist li{{margin:5px 0;font-size:14.5px}}
.stamp{{float:right;width:76px;height:76px;border-radius:50%;border:3px double currentColor;display:flex;flex-direction:column;align-items:center;justify-content:center;transform:rotate(-9deg);margin:2px 0 10px 18px;opacity:.92;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;shape-outside:circle()}}
.clearfix{{clear:both}}
.cols{{display:grid;grid-template-columns:minmax(0,1fr);gap:22px}}
@media(min-width:1080px){{.cols{{grid-template-columns:minmax(0,1fr) 270px}}}}
.rail{{display:none}}
@media(min-width:1080px){{.rail{{display:block;position:sticky;top:18px;align-self:start}}}}
.railbox{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin:0 0 10px}}
.railbox h4{{margin:0 0 8px;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink2)}}
.railrow{{display:flex;gap:9px;align-items:center;padding:5px 0;text-decoration:none;color:var(--ink);font-size:13px;border-top:1px solid var(--line)}}
.railbox .railrow:first-of-type{{border-top:0}}
.railrow:hover .rn{{color:var(--accent)}}
.rgs{{flex:0 0 auto;display:flex;gap:4px}}
.rg{{flex:0 0 auto;min-width:24px;height:20px;padding:0 4px;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:#fff}}
.rn{{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink2)}}
.railempty{{margin:0;font-size:12.5px;color:var(--ink2);opacity:.7}}
.ticker{{position:relative;overflow:hidden;border:1px solid var(--line);border-radius:8px;
background:var(--card);margin:12px 0 6px;opacity:.9;
/* A wide fade swallowed the leading grade chip, so a service appeared ungraded.
   Keep it short enough to soften the cut without hiding content. */
-webkit-mask-image:linear-gradient(90deg,transparent,#000 12px,#000 calc(100% - 26px),transparent);
mask-image:linear-gradient(90deg,transparent,#000 12px,#000 calc(100% - 26px),transparent)}}
.tkrail{{display:flex;width:max-content;animation:tk 78s linear infinite}}
.ticker:hover .tkrail,.ticker:focus-within .tkrail{{animation-play-state:paused}}
@keyframes tk{{from{{transform:translateX(0)}}to{{transform:translateX(-50%)}}}}
.tkitem{{display:flex;align-items:center;gap:5px;padding:6px 18px;white-space:nowrap;
text-decoration:none;color:var(--ink2);border-right:1px solid var(--line)}}
.tkitem .tkn{{margin-left:3px}}
.tkitem:hover .tkn{{color:var(--accent)}}
.tkg{{flex:0 0 auto;min-width:21px;height:16px;padding:0 4px;border-radius:4px;display:flex;align-items:center;
justify-content:center;font-size:9.5px;font-weight:700;color:#fff;opacity:.9}}
.tkn{{font-size:11.5px;font-weight:600;color:var(--ink2)}}
.tks{{font-size:9.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink2);opacity:.6}}
@media(prefers-reduced-motion:reduce){{.tkrail{{animation:none}}.ticker{{overflow-x:auto}}}}
.lbtbl td{{padding:9px 12px}}
.lbrank{{width:34px;color:var(--ink2);font-variant-numeric:tabular-nums;font-size:12.5px}}
.lbg{{width:48px}}
.lbname{{font-weight:600}}
.lbdesc{{display:block;font-size:12.5px;color:var(--ink2);margin-top:3px;max-width:46em}}
.lbrev{{font-weight:650}}
.lbdash{{color:var(--ink2);opacity:.4}}
.lbshare{{font-size:10.5px;color:var(--ink2);border:1px solid var(--line);border-radius:4px;padding:0 4px;margin-left:5px}}
.lbcirc{{color:var(--warn);font-weight:650}}
.dem{{font-size:11.5px;font-weight:650;text-transform:uppercase;letter-spacing:.04em;white-space:nowrap}}
.d-broad{{color:var(--good)}}.d-mixed{{color:var(--ink2)}}
.d-thin{{color:var(--warn)}}.d-one-wallet{{color:var(--crit)}}
.post.sum{{padding-bottom:16px}}
.post.sum .ptitle{{margin-bottom:5px}}
.post.sum .ptitle a{{color:inherit;text-decoration:none}}
.post.sum .ptitle a:hover{{color:var(--accent)}}
.post.sum .lede{{margin:7px 0 0}}
.sumsub{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;
color:var(--ink2);margin:0 0 2px}}
.more{{display:inline-block;margin-top:11px;font-size:13.5px;font-weight:600;
color:var(--accent);text-decoration:none}}
.more:hover{{text-decoration:underline}}
.more::after{{content:" \\2192"}}
.mcpbar{{display:flex;align-items:center;flex-wrap:wrap;gap:9px 18px;background:var(--card);
border:1px solid var(--line);border-radius:12px;padding:13px 20px;margin:9px 0 4px}}
.mcptext{{font-size:14px;color:var(--ink2);line-height:1.45;flex:1;min-width:260px}}
.mcptext b{{color:var(--ink);font-weight:650}}
.mcpcmd{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:8px 11px;
white-space:nowrap;overflow-x:auto;max-width:100%;color:var(--ink)}}
.mcplink{{font-size:13px;font-weight:600;color:var(--accent);text-decoration:none;white-space:nowrap}}
.mcplink:hover{{text-decoration:underline}}
.mcplink::after{{content:" \\2192"}}
@media(max-width:760px){{.mcpcmd{{font-size:11px}}}}
.svcline{{font-size:15px;line-height:1.55;margin:0 0 10px}}
.odsbox{{display:flex;align-items:center;gap:22px;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:15px 20px;margin:0 0 10px;flex-wrap:wrap}}
.odsnum{{font-size:40px;font-weight:750;letter-spacing:-.03em;font-variant-numeric:tabular-nums;line-height:1}}
.odsnum span{{font-size:15px;font-weight:600;opacity:.5}}
.odsbars{{flex:1;min-width:230px;display:flex;flex-direction:column;gap:6px}}
.odsbar{{display:flex;align-items:center;gap:9px;font-size:12px;color:var(--ink2)}}
.odsbar span{{width:52px}}
.odsbar i{{height:7px;background:var(--accent);border-radius:4px;min-width:2px;opacity:.75}}
.odsbar b{{font-variant-numeric:tabular-nums;font-weight:600;margin-left:auto;font-size:11.5px}}
.eplist{{margin:0 0 12px;padding-left:20px;font-size:14px;line-height:1.7}}
.eplist code{{background:var(--bg);border:1px solid var(--line);border-radius:4px;padding:1px 5px;
font-size:12.5px;font-variant-numeric:tabular-nums}}
.tchart{{display:flex;align-items:flex-end;gap:5px;height:92px;background:var(--card);
border:1px solid var(--line);border-radius:12px;padding:12px 16px 8px;margin:0 0 10px}}
.tbar{{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;
height:100%;gap:5px}}
.tbar i{{width:100%;background:var(--accent);border-radius:3px 3px 0 0;opacity:.7}}
.tbar span{{font-size:10px;color:var(--ink2);font-variant-numeric:tabular-nums}}
.svcmeta{{border-top:1px solid var(--line);padding-top:12px;margin-top:16px}}
.dlist{{list-style:none;padding:0;margin:0 0 10px}}
.dlist li{{padding:6px 0;border-bottom:1px solid var(--line);font-size:14px}}
.dgood{{color:var(--good);font-weight:650;text-transform:uppercase;font-size:11px;letter-spacing:.04em;margin-right:6px}}
.dbad{{color:var(--warn);font-weight:650;text-transform:uppercase;font-size:11px;letter-spacing:.04em;margin-right:6px}}
.rcpt-grid{{display:grid;gap:14px}}
@media(min-width:720px){{.rcpt-grid{{grid-template-columns:1fr 1fr}}}}
.rcpt{{border:1px solid var(--line);border-radius:12px;padding:15px 16px;background:var(--card)}}
.rcpt-top{{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px}}
.rid{{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--ink2)}}
.rcpt-h{{font-size:16px;margin:2px 0 6px}}
.rcpt-h a{{color:inherit;text-decoration:none}}
.rcpt-h a:hover{{text-decoration:underline}}
.rcpt-what{{font-size:14px;line-height:1.55;margin:0 0 8px}}
.rcpt-facts{{font-size:13px;color:var(--ink2);margin-bottom:8px}}
.verify3{{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:2px}}
.verify3 span{{font-size:11px;font-weight:650;padding:2px 9px;border-radius:999px;letter-spacing:.02em}}
.vok{{color:var(--good);background:color-mix(in srgb,var(--good) 13%,transparent)}}
.vna{{color:var(--ink2);background:color-mix(in srgb,var(--ink2) 13%,transparent)}}
.vno{{color:var(--crit);background:color-mix(in srgb,var(--crit) 13%,transparent)}}
.rsec{{font-size:19px;margin:28px 0 4px;border-top:1px solid var(--line);padding-top:18px}}
.rnote{{font-size:13px;color:var(--ink2);margin:8px 0;line-height:1.5}}
.speccode{{max-height:520px;overflow:auto}}
.rcptcallout{{display:flex;align-items:center;gap:11px;flex-wrap:wrap;text-decoration:none;
border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:10px;
padding:10px 14px;margin:0 0 16px;background:var(--card)}}
.rcptcallout:hover{{border-color:var(--accent)}}
.rcptnew{{font-size:10px;font-weight:750;text-transform:uppercase;letter-spacing:.05em;
color:#fff;background:var(--accent);padding:2px 8px;border-radius:999px;white-space:nowrap}}
.rcpttext{{flex:1;min-width:220px;font-size:13.5px;color:var(--ink2)}}
.rcpttext b{{color:var(--ink)}}
.rcptarrow{{font-weight:650;font-size:13.5px;color:var(--accent);white-space:nowrap}}
.glpill{{display:inline-block;font-size:11px;font-weight:750;letter-spacing:.04em;padding:2px 9px;
border-radius:999px;color:#fff;white-space:nowrap}}
.gl-green{{background:var(--good)}}.gl-yellow{{background:var(--warn)}}
.gl-red{{background:var(--crit)}}.gl-gray{{background:var(--ink3,#9aa4b2)}}
.glcard{{border:1px solid var(--line);border-radius:12px;padding:13px 15px;background:var(--card)}}
.glcard-top{{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:7px}}
.glhost{{font-weight:650;color:inherit;text-decoration:none}}
.glhost:hover{{text-decoration:underline}}
.glmeta{{font-size:12px;color:var(--ink2);margin-left:auto}}
.gllist{{list-style:none;padding:0;margin:0;font-size:13px;line-height:1.5}}
.gllist li{{padding:3px 0 3px 16px;position:relative;color:var(--ink2)}}
.gllist li:before{{content:"";position:absolute;left:0;top:9px;width:6px;height:6px;border-radius:50%}}
.glr-red:before{{background:var(--crit)}}.glr-yellow:before{{background:var(--warn)}}
.glr-info:before{{background:var(--ink3,#9aa4b2)}}
.pfverdict{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;border:1px solid var(--line);
border-radius:10px;padding:11px 14px;margin:0 0 14px;background:var(--card)}}
.pfverdict .pflabel{{font-size:12px;color:var(--ink2)}}
.pfverdict .pflabel b{{color:var(--ink)}}
.cmd{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:11px 14px;
overflow-x:auto;margin:10px 0 12px}}
.cmd code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;white-space:pre}}
.ods{{font-variant-numeric:tabular-nums;font-weight:750;cursor:help}}
.ods-hi{{color:var(--good)}}.ods-mid{{color:var(--ink2)}}.ods-lo{{color:var(--warn)}}
.lbfill{{color:var(--warn)}}
.viewsel{{display:flex;gap:6px;margin:0 0 12px;align-items:center}}
.vlabel{{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink2);margin-right:2px}}
.hero{{display:flex;align-items:stretch;gap:0;background:var(--card);
border:1px solid var(--line);border-radius:8px;padding:16px 20px;margin:0 0 4px}}
.herostats{{display:flex;flex-direction:column;justify-content:center;gap:10px;flex:0 0 auto}}
.heronums{{display:flex;gap:32px}}
.heronum{{display:flex;flex-direction:column;gap:3px}}
.heronum b{{font-size:26px;font-weight:680;letter-spacing:-.025em;font-variant-numeric:tabular-nums;line-height:1}}
.heronum span{{font-size:11.5px;color:var(--ink2)}}
.herostamp{{font-size:11px;color:var(--ink2);opacity:.65;font-variant-numeric:tabular-nums}}
.heroline{{flex:1;min-width:270px;display:flex;align-items:center;
border-left:1px solid var(--line);margin-left:28px;padding-left:28px}}
.heroline p{{margin:0;font-size:14.5px;color:var(--ink2);line-height:1.5}}
.heroline a{{color:var(--ink);font-weight:640;text-decoration:none;border-bottom:1px solid var(--accent)}}
@media(max-width:860px){{.hero{{flex-direction:column}}
.heroline{{border-left:0;margin-left:0;padding-left:0;border-top:1px solid var(--line);margin-top:14px;padding-top:14px}}}}
.heromcp{{color:var(--accent)!important;font-weight:600;border-bottom:none!important;white-space:nowrap;font-size:13.5px}}
@media(max-width:640px){{.ticker{{display:none}}.mh-note{{display:none}}}}
@media(max-width:520px){{.heronums{{display:grid;grid-template-columns:1fr 1fr;gap:14px 18px}}.heronum b{{font-size:22px}}}}
.crow{{display:flex;gap:11px;align-items:center;padding:9px 12px;border:1px solid var(--line);border-radius:8px;
margin:0 0 5px;text-decoration:none;color:var(--ink);background:var(--card)}}
.crow:hover{{border-color:var(--accent)}}
.cg{{flex:0 0 30px;height:21px;border-radius:5px;display:flex;align-items:center;justify-content:center;
font-size:11px;font-weight:700;color:#fff}}
.cg-u{{background:transparent;border:1px solid var(--line);color:var(--ink2)}}
.ctitle{{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600;font-size:14px}}
.capi{{flex:0 0 auto;font-size:12px;color:var(--ink2);font-family:ui-monospace,SFMono-Regular,Menlo,monospace}}
.cverdict{{flex:0 0 auto;font-size:11.5px;font-weight:650}}
.cverdict.v-ok{{color:var(--good)}}.cverdict.v-bad{{color:var(--crit)}}.cverdict.v-warn{{color:var(--warn)}}
.cdate{{flex:0 0 auto;font-size:11.5px;color:var(--ink2);font-variant-numeric:tabular-nums}}
@media(max-width:700px){{.capi,.cdate{{display:none}}}}
.nday{{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink2);margin:20px 0 8px;font-variant-numeric:tabular-nums}}
.ncard{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 15px;margin:0 0 8px;max-width:820px}}
.nhead{{display:flex;gap:9px;align-items:baseline;flex-wrap:wrap}}
.nsvc{{font-weight:650;font-size:15.5px}}
.nbadge{{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;font-weight:700;color:var(--good);border:1px solid var(--good);border-radius:4px;padding:1px 6px}}
.ncount{{margin-left:auto;font-size:12px;color:var(--ink2);font-variant-numeric:tabular-nums}}
.nhost{{font-size:12px;color:var(--ink2);word-break:break-all;margin-top:1px}}
.ndesc{{margin:7px 0 0;font-size:13.5px;line-height:1.45;color:var(--ink)}}
.neps{{margin-top:9px}}
.neps>summary{{cursor:pointer;list-style:none;font-size:11px;text-transform:uppercase;letter-spacing:.07em;
color:var(--ink2);display:inline-flex;align-items:center;gap:5px;padding:2px 0}}
.neps>summary::-webkit-details-marker{{display:none}}
.neps>summary::before{{content:"+";font-size:13px;line-height:1}}
.neps[open]>summary::before{{content:"\\2212"}}
.neps>summary:hover{{color:var(--accent)}}
.eplist{{list-style:none;margin:8px 0 0;padding:0}}
.eplist li{{display:flex;gap:9px;align-items:baseline;padding:4px 0;border-top:1px solid var(--line);font-size:13px}}
.eplist li:first-child{{border-top:0}}
.eplist code{{flex:0 0 auto;font-size:11.5px;color:var(--ink)}}
.epdesc{{flex:1;min-width:0;color:var(--ink2)}}
.epuse{{flex:0 0 auto;font-size:11.5px;color:var(--ink2);font-variant-numeric:tabular-nums}}
.epuse::after{{content:" calls"}}
.epmore{{color:var(--ink2);opacity:.7;font-size:12px}}
@media(max-width:640px){{.eplist li{{flex-wrap:wrap;gap:2px}}}}
.fnote{{border-left:3px solid var(--warn);background:var(--card);border:1px solid var(--line);border-left-width:3px;border-radius:0 10px 10px 0;padding:15px 18px;margin:0 0 12px;max-width:760px}}
.fnote h3{{margin:0 0 7px;font-size:17px;letter-spacing:-.01em}}
.fnote p{{margin:0 0 7px;font-size:14.5px;color:var(--ink2)}}
.fnote .fdo{{color:var(--ink)}}
.fnote .fev{{margin:0;font-size:13px;opacity:.8;border-top:1px dashed var(--line);padding-top:7px}}
.vtag{{font-weight:650;font-size:13px}}
.vtag.v-ok{{color:var(--good)}}.vtag.v-bad{{color:var(--crit)}}.vtag.v-warn{{color:var(--warn)}}
.l-g{{width:52px}}.l-s a{{font-weight:650;text-decoration:none}}.l-s a:hover{{text-decoration:underline}}
.reqbox{{margin:12px 0;border:1px solid var(--line);border-radius:8px;background:rgba(127,127,127,.05)}}
.reqhead{{padding:8px 13px 0;font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--ink2);font-weight:700;display:flex;justify-content:space-between;gap:10px}}
.rwho{{text-transform:none;letter-spacing:0;font-weight:400;opacity:.85}}
.reqbox pre{{margin:0;padding:7px 13px 12px;overflow-x:auto}}
.reqbox code{{background:none;padding:0;font-size:12.5px;line-height:1.6;white-space:pre;color:var(--ink)}}
.repro{{margin:12px 0 0;border:1px solid var(--line);border-radius:8px;background:rgba(127,127,127,.05)}}
.repro summary{{cursor:pointer;padding:9px 13px;font-size:13.5px;font-weight:600;color:var(--accent);list-style:none}}
.repro summary::-webkit-details-marker{{display:none}}
.repro summary::before{{content:"▸ ";font-size:11px}}
.repro[open] summary::before{{content:"▾ "}}
.repro .rnote{{margin:0;padding:0 13px 8px;font-size:13px;color:var(--ink2)}}
.repro pre{{margin:0;padding:0 13px 13px;overflow-x:auto}}
.repro code{{background:none;padding:0;font-size:12.5px;line-height:1.65;white-space:pre;color:var(--ink2)}}
.stamp-top,.stamp-bot{{font-size:8.5px;letter-spacing:.16em;font-weight:700;line-height:1}}
.stamp-bot{{font-size:7px;letter-spacing:.1em;opacity:.85}}
.stamp-letter{{font-size:29px;font-weight:800;line-height:1;margin:3px 0;letter-spacing:-.02em}}
.s-u{{color:var(--ink2);opacity:.75}}
.stamp-word{{font-size:15px;font-weight:800;letter-spacing:-.01em;line-height:1;margin:3px 0}}
.s-a{{color:var(--good)}}.s-b{{color:#3d9c4d}}.s-c{{color:var(--warn)}}.s-d{{color:var(--ser)}}.s-f{{color:var(--crit)}}
/* studies and news use the identical stamp treatment as a grade, double ring and
   all, and differ only by colour. The shape says "verdict"; the colour says which
   kind. Blue and grey are unused by the A-F palette. */
.s-insight{{color:var(--accent)}}
.s-news{{color:var(--ink2)}}
.stamp-word{{font-size:12.5px;letter-spacing:.04em;font-weight:750;margin:2px 0}}
@media(max-width:600px){{.stamp{{width:68px;height:68px;margin-left:12px}}.stamp-letter{{font-size:26px}}}}
.src a{{color:var(--accent)}}
.ask{{margin:0 0 14px;max-width:none}}
.ask p{{margin:0;font-size:13px;color:var(--ink2)}}
.ask a{{color:var(--accent);font-weight:600;text-decoration:none}}
.ctrl{{margin:0 0 10px}}
input[type=search]{{width:100%;max-width:420px;padding:10px 13px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink);font-size:15px}}
.tbl{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
.tbl th{{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink2);padding:10px 12px;border-bottom:1px solid var(--line);font-weight:600}}
.tbl td{{padding:12px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}}
.tbl tr:last-child td{{border-bottom:0}}
.num{{font-variant-numeric:tabular-nums;white-space:nowrap}}
.c-g{{width:44px}}.c-t{{width:150px}}.c-u{{width:120px}}.c-p{{width:96px}}.c-f{{width:200px}}
.g{{display:inline-flex;align-items:center;justify-content:center;width:27px;height:27px;border-radius:8px;font-weight:700;font-size:13px;color:#fff}}
.gA{{background:var(--good)}}.gB{{background:#2f7d3f}}.gC{{background:var(--warn)}}.gD{{background:var(--ser)}}.gF{{background:var(--crit)}}.gU{{background:var(--line);color:var(--ink2)}}
.t{{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:650;border:1px solid;white-space:nowrap}}
.t-established{{color:var(--good);border-color:var(--good)}}
.t-traction{{color:var(--accent);border-color:var(--accent)}}
.t-thin{{color:var(--ink2);border-color:var(--line)}}
.t-concentrated{{color:var(--ser);border-color:var(--ser)}}
.t-quiet{{color:var(--ink2);border-color:var(--line);opacity:.7}}
.oname{{font-weight:650;display:block}}
.dom{{display:block;font-size:12px;color:var(--ink2);word-break:break-all}}
.also{{display:block;font-size:11px;color:var(--ink2);opacity:.7;margin-top:3px;word-break:break-all}}
.sub{{display:block;font-size:11.5px;color:var(--ink2);margin-top:2px;font-variant-numeric:tabular-nums;white-space:normal}}
ul.ev{{margin:0;padding-left:15px}}
ul.ev li{{margin:2px 0;font-size:12px}}
li.bad{{color:var(--crit)}}li.ok{{color:var(--ink2);opacity:.6}}
.dim{{color:var(--ink2);opacity:.55}}
.method{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 22px;margin-bottom:12px;max-width:760px}}
.method h3{{margin:0 0 8px;font-size:16.5px}}
.method p,.method li{{color:var(--ink2)}}
.method p{{margin:0 0 8px}}
.note{{font-size:13px;color:var(--ink2);margin:0 0 12px}}
code{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em;background:rgba(127,127,127,.13);padding:1px 5px;border-radius:4px}}
.scroll{{overflow-x:auto}}
footer{{margin-top:36px;padding-top:18px;border-top:1px solid var(--line);color:var(--ink2);font-size:13px}}
a{{color:var(--accent)}}
'''

# --- one shell for every top-level page ---------------------------------------
# Each nav destination is now a real URL with its own title, description and
# canonical, instead of five sections hidden behind JavaScript at a single
# address. That makes them separately rankable, separately linkable, and
# separately measurable in analytics, and it cuts each page's weight to the
# section it actually shows.
NAV_ITEMS = [
    ("/", "Feed"),
    ("/studies", "Studies"),
    ("/leaderboard", "Top paid services"),
    ("/preflight", "Preflight"),
    ("/categories", "Categories"),
    ("/delivery", "Delivery checks"),
    ("/receipts", "Receipts"),
    ("/ratings", "Ratings"),
    ("/new-endpoints", "New endpoints"),
    ("/notes", "Field notes"),
    ("/guide", "How to pay"),
    ("/checklist", "Checklist"),
    ("/api", "API"),
    ("/about", "About"),
]


def nav_html(active):
    out = []
    for href, label in NAV_ITEMS:
        cur = ' aria-current="page"' if href == active else ""
        out.append(f'<a href="{html.escape(href)}"{cur}>{html.escape(label)}</a>')
    return '<nav class="tabs">' + "".join(out) + "</nav>"


MASTHEAD = f'''<header class="masthead">
<div class="mh-l">
<h1><a href="/" style="color:inherit;text-decoration:none">What Agents Buy<span class="dot">.</span></a></h1>
<p class="tagline">AI agents can now spend real money on their own. I give mine a wallet, buy things with it, and publish every receipt.</p>
<p class="subline">Independent reviews of the <b>x402</b> APIs behind <b>agentic commerce</b> and agentic payments. New here? Start with the <a href="/checklist">checklist</a>.</p>
</div>
<div class="mh-r">
<p class="mh-by">Written by <b>{AUTHOR}</b></p>
<p class="mh-links"><a href="{CONTACT}">LinkedIn</a> &middot; <a href="/guide">How this works</a> &middot; <a href="/feed.xml">RSS</a></p>
<p class="mh-note">Nothing here is sponsored. No service pays to appear or to be graded.</p>
</div>
</header>'''

SEARCHBAR = ('<div class="searchbar">'
             '<input type="search" id="q" autocomplete="off" spellcheck="false" '
             'aria-label="Search every API and finding" '
             'placeholder="Search any API name or finding, e.g. fortclaw, gas, delivery">'
             '<div id="qresults" class="qresults" role="listbox" hidden></div></div>')

FOOTER = (f'<footer><p>What Agents Buy &middot; by <a href="{CONTACT}">{AUTHOR}</a> &middot; independent, '
          f'no service pays to appear here &middot; every figure carries its source and as-of date &middot; '
          f'feedback and corrections on <a href="{CONTACT}">LinkedIn</a></p></footer>')

PAGE_JS = """<script>
document.querySelectorAll('details.detail').forEach(d=>{
  const hint=d.querySelector('.dhint');
  if(hint) d.addEventListener('toggle',()=>{hint.textContent = d.open ? 'click to hide' : 'click to expand';});
});
const vs=document.querySelectorAll('.viewsel .chip');
vs.forEach(c=>c.addEventListener('click',()=>{
  vs.forEach(x=>x.setAttribute('aria-pressed', x===c));
  document.getElementById('cards').hidden = c.dataset.v !== 'cards';
  document.getElementById('compact').hidden = c.dataset.v !== 'compact';
}));
</script>"""

SEARCH_JS = r"""<script>
(function(){
  var q=document.getElementById('q'), box=document.getElementById('qresults');
  if(!q||!box) return;
  var items=null, rows=[], sel=-1;
  var RANK={graded:0,measured:1,post:2,rating:2,study:2,news:2,note:2,listed:3};
  function esc(s){return (s||'').replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
  function load(){
    if(items) return Promise.resolve();
    return fetch('/api/search-index.json').then(function(r){return r.json();}).then(function(d){items=d.items||[];});
  }
  function meta(it){
    if(it.t==='host'){
      if(it.status==='graded') return 'Grade '+(it.grade||'')+(it.n>1?' · '+it.n+' endpoints':'');
      var bits=[];
      if(it.price!=null) bits.push('from $'+it.price);
      if(it.rel!=null) bits.push('rel '+it.rel);
      if(it.status==='listed') bits.push('not yet tested');
      else if(it.delivered===true) bits.push('delivers');
      else if(it.delivered===false) bits.push('fell short');
      return bits.join(' · ');
    }
    return {post:'study or rating',rating:'rating',study:'study',news:'news',note:'field note'}[it.t]||'';
  }
  function sub(it){ return it.t==='host' ? (it.desc||it.label) : meta(it); }
  function badge(it){ var s=it.t==='host'?it.status:(it.t==='note'?'note':'post'); return {graded:'GRADED',measured:'MEASURED',listed:'LISTED',post:'FINDING',note:'NOTE'}[s]||s.toUpperCase(); }
  function bcls(it){ var s=it.t==='host'?it.status:(it.t==='note'?'note':'post'); return 'qb-'+s; }
  function render(list, total){
    if(!list.length){ box.innerHTML='<div class="qempty">Nothing measured for that yet.</div>'; box.hidden=false; return; }
    var more = total>list.length ? '<div class="qmore">+'+(total-list.length)+' more · keep typing to narrow</div>' : '';
    box.innerHTML=list.map(function(it,i){
      var tag=it.url?'a':'div', href=it.url?(' href="'+it.url+'"'):'';
      var right = it.t==='host' ? '<span class="qmeta">'+esc(meta(it))+'</span>' : '';
      return '<'+tag+' class="qrow" role="option"'+href+' data-i="'+i+'">'
        +'<span class="qbadge '+bcls(it)+'">'+badge(it)+'</span>'
        +'<span class="qmain"><span class="qname">'+esc(it.label)+'</span>'
        +'<span class="qsub">'+esc(sub(it))+'</span></span>'+right+'</'+tag+'>';
    }).join('')+more+'<div class="qhint">graded = we paid and graded it · measured = we swept it · listed = in the registry, untested by us</div>';
    box.hidden=false; sel=-1;
  }
  var bar=q.closest('.searchbar');
  function search(){
    var v=q.value.trim().toLowerCase();
    if(bar) bar.classList.toggle('searching', v.length>=1);
    if(v.length<1){ box.hidden=true; return; }
    load().then(function(){
      var deep=v.length>=3;
      // Match quality: a whole-word or name hit is far more relevant than the
      // query buried in a description ("block" inside "blockchain"). 0 is best.
      function mclass(it){
        var L=(it.label||'').toLowerCase();
        var wb=new RegExp('(^|[^a-z0-9])'+v.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'));
        if(L.indexOf(v)===0) return 0;                 // name starts with it
        if(wb.test(L)) return 1;                        // name, at a word boundary
        if(L.indexOf(v)>=0) return 2;                   // name, mid-word (crownBLOCK)
        if(it.kw&&it.kw.indexOf(v)>=0) return 3;        // a tag or graded subject
        return 4;                                        // only in the description
      }
      rows=items.filter(function(it){
        if((it.label||'').toLowerCase().indexOf(v)>=0) return true;
        if(it.kw&&it.kw.indexOf(v)>=0) return true;
        return deep && it.desc && it.desc.toLowerCase().indexOf(v)>=0;
      }).sort(function(a,b){
        var ma=mclass(a), mb=mclass(b);
        if(ma!==mb) return ma-mb;                        // relevance first
        var ra=RANK[a.t==='host'?a.status:a.t], rb=RANK[b.t==='host'?b.status:b.t];
        if(ra!==rb) return ra-rb;                        // then how much we verified
        return (a.label||'').length-(b.label||'').length;
      });
      render(rows.slice(0,8), rows.length);
    });
  }
  q.addEventListener('input',search);
  q.addEventListener('focus',function(){ load(); if(q.value.trim().length>=1) search(); });
  q.addEventListener('keydown',function(e){
    var opts=box.querySelectorAll('.qrow');
    if(e.key==='ArrowDown'){ e.preventDefault(); sel=Math.min(sel+1,opts.length-1); }
    else if(e.key==='ArrowUp'){ e.preventDefault(); sel=Math.max(sel-1,0); }
    else if(e.key==='Enter'){ if(opts[sel]&&opts[sel].tagName==='A'){ location.href=opts[sel].getAttribute('href'); } return; }
    else if(e.key==='Escape'){ box.hidden=true; q.blur(); return; }
    else return;
    opts.forEach(function(o){o.classList.remove('sel');});
    if(opts[sel]){ opts[sel].classList.add('sel'); opts[sel].scrollIntoView({block:'nearest'}); }
  });
  var pf=new Set();
  box.addEventListener('mouseover',function(e){
    var a=e.target.closest('a.qrow'); if(!a) return;
    var href=a.getAttribute('href'); if(!href||pf.has(href)) return;
    pf.add(href); var l=document.createElement('link'); l.rel='prefetch'; l.href=href; document.head.appendChild(l);
  });
  document.addEventListener('click',function(e){ if(!e.target.closest('.searchbar')) box.hidden=true; });
})();
</script>"""


def site_page(active, title, desc, body, ld=""):
    """Full chrome plus one section. `active` is both the canonical path and the nav state."""
    if not ld:
        # Every top-level page describes itself, so no URL is a blank to a crawler.
        _label = dict(NAV_ITEMS).get(active, "What Agents Buy")
        ld = json.dumps([
            {"@context": "https://schema.org",
             "@type": "CollectionPage" if active != "/" else "WebSite",
             "name": title, "description": desc, "url": SITE + active,
             "isPartOf": {"@type": "WebSite", "name": "What Agents Buy", "url": SITE},
             "publisher": {"@type": "Organization", "name": "What Agents Buy", "url": SITE}},
            json.loads(crumbs(("Home", "/"), (_label, active)) if active != "/"
                       else crumbs(("Home", "/")))], separators=(",", ":"))
    ldtag = f'<script type="application/ld+json">{ld}</script>\n' if ld else ""
    return (
        '<meta charset="utf-8">\n'
        f'<title>{html.escape(title)}</title>\n'
        f'<meta name="description" content="{html.escape(desc)}">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<link rel="canonical" href="{SITE}{active}">\n'
        f'<meta property="og:title" content="{html.escape(title)}">\n'
        f'<meta property="og:description" content="{html.escape(desc)}">\n'
        f'<meta property="og:type" content="{"website" if active == "/" else "article"}">\n'
        f'<meta property="og:url" content="{SITE}{active}">\n'
        f'<meta property="og:image" content="{SITE}/og.png">\n'
        '<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:image" content="{SITE}/og.png">\n'
        + ldtag +
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">\n'
        '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">\n'
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
        '<meta name="theme-color" content="#ffffff">\n'
        '<script defer src="/_vercel/insights/script.js"></script>\n'
        '<script defer src="/_vercel/speed-insights/script.js"></script>\n'
        f'<link rel="alternate" type="application/rss+xml" title="What Agents Buy RSS" href="{SITE}/feed.xml">\n'
        f'<link rel="alternate" type="application/json" title="Ratings as JSON" href="{SITE}/api/ratings.json">\n'
        f'<link rel="alternate" type="text/plain" title="Whole site as text" href="{SITE}/llms-full.txt">\n'
        f'<style>{CSS}</style>\n'
        '<div class="wrap">\n'
        + MASTHEAD + "\n" + _ticker + "\n" + _hero + "\n"
        + nav_html(active) + "\n" + SEARCHBAR + "\n"
        + body + "\n"
        + FOOTER + "\n</div>\n" + PAGE_JS + SEARCH_JS)


# Homepage callout for the dispute receipts. Counts are loaded here because the
# hero and feed are assembled before the full receipts block runs; the ledger
# itself renders on /receipts. One quiet strip, so the decluttered header stays.
_rc_n = _rc_disp = 0
try:
    for _rl in open(os.path.join(HERE, "data", "receipts", "receipts.jsonl")):
        _rl = _rl.strip()
        if not _rl:
            continue
        _rj = json.loads(_rl)
        _rc_n += 1
        if _rj.get("verdict", {}).get("status") == "short":
            _rc_disp += 1
except Exception:
    pass
_receipts_callout = (
    f'<a class="rcptcallout" href="/preflight"><span class="rcptnew">New</span>'
    f'<span class="rcpttext"><b>Preflight</b>: the one check your agent runs before it pays an x402 API, one '
    f'<b>CLEAR / HOLD / ABORT</b> verdict it can gate on, backed by {_rc_disp} underdelivered receipts and '
    f'live payment-safety checks.</span><span class="rcptarrow">See Preflight &rarr;</span></a>'
) if _rc_n else ""

FEED_BODY = f'''<div class="cols"><section id="feed">
{_receipts_callout}
<div class="viewsel"><span class="vlabel">View</span><button class="chip" data-v="cards" aria-pressed="true">Cards</button><button class="chip" data-v="compact" aria-pressed="false">Compact</button></div>
<div id="cards">{posts_html}</div>
<div id="compact" hidden>{compact_html}</div>
<div class="ask"><p><b>Seeing something in this market?</b> Data, corrections and disagreements all welcome. Message <a href="{CONTACT}">{AUTHOR}</a> on LinkedIn, or follow along by <a href="/feed.xml">RSS</a>.</p></div>
</section>
<aside class="rail">
<div class="railbox"><h4>The ledger</h4>{rail_ledger}</div>
<div class="railbox"><h4>Field notes</h4>{rail_notes}</div>
</aside></div>
{_mcpbar}'''

# This page reads like a growth feed and is not one. Measured 4 to 10 August, the
# registry added 2,803 endpoints and removed 2,643 for a net gain of 160, with six
# origins behind two thirds of the additions. Say that here rather than let volume
# be mistaken for adoption.
NEWEST_BODY = (
    f'<p class="note"><b>This is a churn feed, not a growth chart.</b> Everything that appeared in the '
    f'public registry since we started watching, newest first: {_tot_new:,} endpoints across '
    f'{len(NEWEST)} services, {_tot_svc} of them services that did not exist before. Each line is one paid '
    f'endpoint with the seller\'s own description and its 30-day paid-call count, all of it unverified '
    f'until we buy something.</p>'
    f'<p class="note dim">Volume here is not adoption. Between 4 and 10 August the registry added 2,803 '
    f'endpoints and removed 2,643, a net gain of about 1%, and six origins produced two thirds of the '
    f'additions while one produced a third of the removals. Most delisted services are not dead either: of '
    f'25 checked, 24 still answered. For what actually gets paid, see '
    f'<a href="/leaderboard">top paid services</a>; for the full working, '
    f'<a href="/p/who-fills-the-directory">read the study</a>.</p>'
    f'{newest_html}')

NOTES_BODY = (f'<p class="note">{len(NOTES)} ways paying an API as software goes wrong, each one learned by '
              f'getting it wrong first. Most of these cost me a retraction rather than money.</p>{notes_html}')

page = site_page("/", "What Agents Buy | x402 API reviews for agentic commerce",
                 "Independent reviews of the x402 APIs behind agentic commerce and agentic payments. I pay x402 "
                 "endpoints with a real wallet and publish what was quoted, what was charged and whether the goods "
                 "arrived, alongside daily USDC stablecoin settlement on Base swept straight from chain logs. Free "
                 "MCP server and JSON API so an agent can check a service before it pays.", FEED_BODY, ld=_jsonld)

# Every off-site link opens in its own tab. A reader checking a transaction on a
# block explorer should not lose the post they were reading. Internal anchors
# deliberately stay in the same tab.
def _newtab(m):
    href, attrs = m.group(1), m.group(2)
    if "target=" in attrs:
        return m.group(0)
    attrs = re.sub(r'\s*rel="[^"]*"', "", attrs)  # avoid a duplicate rel
    return f'<a href="{href}"{attrs} target="_blank" rel="noopener noreferrer">'


def finish(s):
    return re.sub(r'<a href="(https?://[^"]+)"([^>]*)>', _newtab, s)


PUB = os.path.join(HERE, "public")


def write(rel, content):
    path = os.path.join(PUB, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write(finish(content))


def crumbs(*pairs):
    """BreadcrumbList so search engines can show where a page sits in the site."""
    return json.dumps({"@context": "https://schema.org", "@type": "BreadcrumbList",
        "itemListElement": [{"@type": "ListItem", "position": i, "name": n, "item": SITE + u}
                            for i, (n, u) in enumerate(pairs, 1)]}, separators=(",", ":"))


def shell(title, desc, canonical, body, ld=""):
    """One skeleton, so every URL carries its own title, description and canonical."""
    return (
        f'<title>{html.escape(title)}</title>\n'
        + (f'<script type="application/ld+json">{ld}</script>\n' if ld else "")
        + ''
        f'<meta name="description" content="{html.escape(desc)}">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<link rel="canonical" href="{SITE}{canonical}">\n'
        '<meta property="og:type" content="article">\n'
        f'<meta property="og:title" content="{html.escape(title)}">\n'
        f'<meta property="og:description" content="{html.escape(desc)}">\n'
        f'<meta property="og:url" content="{SITE}{canonical}">\n'
        f'<meta property="og:image" content="{SITE}/og.png">\n'
        '<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:image" content="{SITE}/og.png">\n'
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">\n'
        '<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">\n'
        '<link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
        '<meta name="theme-color" content="#0d0d0d">\n'
        '<script defer src="/_vercel/insights/script.js"></script>\n'
        '<script defer src="/_vercel/speed-insights/script.js"></script>\n'
        f'<link rel="alternate" type="application/rss+xml" title="What Agents Buy RSS" href="{SITE}/feed.xml">\n'
        f'<style>{CSS}</style>\n'
        '<div class="wrap">\n<header>\n'
        '<h1><a href="/" style="color:inherit;text-decoration:none">What Agents Buy<span class="dot">.</span></a></h1>\n'
        '<p class="subline">Independent measurement of the <b>x402</b> agent economy: what agents pay for, '
        'what it costs, and whether it works. <a href="/guide">How this works</a>.</p>\n'
        f'<p class="byline">By <a href="{CONTACT}">{AUTHOR}</a> &middot; feedback on <a href="{CONTACT}">LinkedIn</a> &middot; <a href="/feed.xml">RSS</a> &middot; <a href="/">All posts</a></p>\n'
        '</header>\n'
        + SEARCHBAR + "\n"
        f'{body}\n'
        f'<footer><p>What Agents Buy &middot; by <a href="{CONTACT}">{AUTHOR}</a> &middot; independent, no service pays to appear here &middot; every figure carries '
        f'its source and as-of date &middot; feedback and corrections on <a href="{CONTACT}">LinkedIn</a></p></footer>\n'
        '</div>' + SEARCH_JS
    )


os.makedirs(PUB, exist_ok=True)
write("index.html", page)
open(os.path.join(PUB, "feed.xml"), "w").write(rss())

# Each nav destination is its own URL, its own title and its own canonical.
write("leaderboard/index.html", site_page(
    "/leaderboard", "Top paid services: x402 revenue and settlement rankings | What Agents Buy",
    "Which x402 services actually receive the most USDC, ranked by settlement swept daily straight from "
    "Base chain logs. Not registry counters, and not a quality ranking: it measures money received, which is "
    "turnover rather than revenue, since several of the biggest pay almost all of it straight back out.",
    leaderboard_html))
write("ratings/index.html", site_page(
    "/ratings", "Every endpoint rating, best to worst | What Agents Buy",
    "Every x402 endpoint bought from and graded here, sorted best to worst, with what was quoted, what was "
    "actually charged, and whether the goods arrived. Nothing is graded that was not paid for.",
    ledger_html))
write("new-endpoints/index.html", site_page(
    "/new-endpoints", "New x402 endpoints, newest first | What Agents Buy",
    "Every paid endpoint that has appeared in the public x402 registry since we started watching, newest "
    "first, with the seller's own description and its 30-day paid-call count.",
    NEWEST_BODY))

# --- a real URL for every payment and every field note ----------------------
urls = ["/"]
for po in FEED:
    # Receipts stay collapsed. Opening them by default meant the second thing a
    # reader met was a wall of monospace.
    body = post_html(po)
    if po.get("type") == "payment" and po.get("n"):
        title = "Payment #%02d: %s" % (po["n"], po.get("title", ""))
    else:
        title = po.get("title") or po.get("text", "")[:70]
    api = (po.get("api") or {}).get("name")
    if api:
        title = f"{title} — {api}"
    desc = po.get("lede") or po.get("text", "")[:180]
    # Per-page structured data. A Review on a post is what makes it eligible for a
    # rich result; the homepage ItemList alone does nothing for the permalink.
    _g = po.get("grade")
    _svc = (po.get("api") or {}).get("name")
    _ld = {"@context": "https://schema.org",
           "@type": "Review" if _g else "Article",
           "headline": po.get("title", ""), "name": po.get("title", ""),
           "url": f'{SITE}/p/{po["id"]}',
           "datePublished": po["ts"].split(" ")[0],
           "author": PERSON,
           "publisher": {"@type": "Organization", "name": "What Agents Buy", "url": SITE},
           "description": desc[:300]}
    if _g:
        _ld["reviewRating"] = {"@type": "Rating", "ratingValue": _g,
                               "bestRating": "A+", "worstRating": "F"}
        _ld["itemReviewed"] = {"@type": "WebAPI", "name": _svc or po.get("title", ""),
                               "url": (po.get("api") or {}).get("url") or SITE,
                               "description": po.get("endpoint") or ""}
        _v = po.get("verdict") or {}
        if _v.get("call"):
            _ld["reviewBody"] = (f'{_v["call"]}. Quoted {_v.get("quoted")}, charged {_v.get("charged")}, '
                                 f'goods {"delivered" if _v.get("delivered") else "not delivered"}.')
    # Related links: other grades for the same service, then posts and field notes
    # that share a tag. Internal links are how a crawler finds the rest of the site
    # from a permalink, and until now a post was a dead end.
    _tags = set(po.get("tags") or [])
    _rel = []
    for _o in FEED:
        if _o["id"] == po["id"]:
            continue
        _same_svc = _svc and (_o.get("api") or {}).get("name") == _svc
        _shared = _tags & set(_o.get("tags") or [])
        if _same_svc or _shared:
            _rel.append((0 if _same_svc else 1, len(_shared), _o))
    _rel = [x[2] for x in sorted(_rel, key=lambda x: (x[0], -x[1]))][:4]
    _relnotes = [n for n in NOTES if _tags & set(n.get("tags") or [])][:2]
    if not _relnotes:
        _relnotes = NOTES[:2]
    _rel_html = ""
    if _rel or is_host(_svc):
        _items = ""
        if is_host(_svc):
            _items += f'<li><a href="/s/{html.escape(_svc)}">Everything graded on {html.escape(_svc)}</a></li>'
        _items += "".join(f'<li><a href="/p/{html.escape(r["id"])}">{html.escape(r.get("title",""))}</a></li>'
                          for r in _rel)
        _items += "".join(f'<li><a href="/notes/{html.escape(n["id"])}">Field note: {html.escape(n["trap"])}</a></li>'
                          for n in _relnotes)
        _rel_html = (f'<nav class="related" aria-label="Related"><h3>Related</h3><ul>{_items}</ul>'
                     f'<p><a href="/ratings">All ratings</a> &middot; <a href="/leaderboard">Top paid services</a> '
                     f'&middot; <a href="/notes">All field notes</a> &middot; <a href="/about">About this site</a></p></nav>')
    body = body + _rel_html
    _bc = crumbs(("Home", "/"), ("Ratings", "/ratings"), (po.get("title", "")[:60], f'/p/{po["id"]}'))
    _ldall = json.dumps([json.loads(json.dumps(_ld)), json.loads(_bc)], separators=(",", ":"))
    write(f"p/{po['id']}/index.html", shell(f"{title} | What Agents Buy", desc, f"/p/{po['id']}", body, ld=_ldall))
    urls.append(f"/p/{po['id']}")

# --- /guide: how paying an endpoint actually works ---------------------------
# This is the one page written for someone arriving cold from a search. Every
# claim in it is something we established by probing or paying, not by reading docs.
GUIDE = '''
<article class="guide">
<h2>How to pay an x402 endpoint</h2>
<p class="glede">Everything below was learned by doing it, mostly by getting it wrong first.
If you are trying to make an agent pay for an API and something is not behaving, start here.</p>

<h3>What x402 is</h3>
<p><b>x402</b> revives HTTP status code 402, "Payment Required", which sat unused in the spec for
thirty years. You request a resource, the server answers <code>402</code> with a machine readable
quote, your client signs a stablecoin payment, and retries with the payment attached. No API key,
no account, no subscription, no invoice. The buyer can be software with a wallet and nothing else.</p>
<p>Payment is almost always <b>USDC</b>, most often on <b>Base</b>. The common scheme is called
<code>exact</code>: pay this precise amount to this precise address. <b>MPP</b>, Stripe's Machine
Payments Protocol, uses the same 402 pattern but is a separate protocol, so a client that speaks one
does not automatically speak the other.</p>

<h3>The four steps</h3>
<ol class="gsteps">
<li><b>Ask without paying.</b> Send the normal request. A working endpoint answers 402 and includes
the price, the asset, the chain and the destination address.</li>
<li><b>Read the quote.</b> Never trust a price from a registry or a docs page. Read the live 402.</li>
<li><b>Sign and retry.</b> Your wallet signs an authorisation for that exact amount and the request
goes again with the payment attached. The private key stays on your machine.</li>
<li><b>Check what arrived.</b> A 200 is not proof of delivery. Confirm the response actually contains
the goods, and confirm on a block explorer that what left your wallet matches the quote.</li>
</ol>

<h3>The payment challenge hides in three places</h3>
<p>This is the single most common reason a working endpoint looks broken. The quote can arrive in the
response body as an <code>accepts[]</code> array, in a <code>payment-required</code> header as base64
JSON, or in <code>WWW-Authenticate</code> in either of two formats. Parsing only the body once produced
a false headline here that 56% of endpoints were unpayable. They were not. Parse all three before you
conclude anything.</p>

<h3>Things that will catch you</h3>
<ul class="gtraps">
<li><b>The wrong verb reads as a dead endpoint.</b> Send GET to a POST only endpoint and you get a 404,
401 or 405 that looks like the service is gone. Read the declared method first. This falsely marked
118 live endpoints as dead in one run here.</li>
<li><b>Decimals are not always six.</b> USDC uses 6. Tokens on BNB Chain typically use 18. Assume 6
across the board and a one cent call reads as ten billion dollars, which is a mistake this site
published and had to correct.</li>
<li><b>Some sellers mint a fresh address per request.</b> A changing payTo is not automatically a
hijack. Probe twice before accusing anyone.</li>
<li><b>A published quote is not a promise.</b> At least one large seller publishes a valid standard
quote and then refuses standard payment, accepting only its own proprietary client.</li>
<li><b>Revenue is not revenue.</b> Money arriving at a seller address is turnover. Several of the
largest earners pay almost all of it straight back out, because they are games returning stakes or
resellers paying suppliers.</li>
</ul>

<h3>What you need to pay with</h3>
<p>The reference implementation is Coinbase's, published under Apache 2.0 with TypeScript, Python and
Go SDKs. The helper packages <code>@x402/fetch</code> and <code>@x402/axios</code> handle the whole
loop: catch the 402, parse it, check it against your ceiling, sign, retry. Roughly twenty lines if you
build it yourself.</p>
<p>Managed options exist at every layer. <b>Coinbase CDP</b> is the broadest facilitator.
<b>Cloudflare</b> ships agent wallets and MCP tooling. <b>Circle</b> has an agent stack.
<b>AWS</b> does buyer side wallets and budgets. <b>AgentCash</b>, which pays for everything on this
site, handles both x402 and MPP across chains and enforces a per call spend cap, which is the feature
that has stopped a bad payment here more than once.</p>

<h3>Where the endpoints are</h3>
<p>Two public registries list what exists, both free and neither requiring auth: Coinbase's
<b>CDP discovery API</b> and <b>Circle's</b> marketplace discovery. Be aware that neither is the
universe. Measured here, CDP accounted for 3% of observed transactions and missed about 35% of all
seller dollars, including at one point the second largest seller by revenue, because plenty of
earning services never registered anywhere.</p>

<h3>How the numbers on this site are produced</h3>
<p>Settlement is swept daily straight from Base JSON RPC logs, reading USDC Transfer events into the
payTo address each service advertises in its own challenge. Registry call counters are not used: one
registry credited a service with 4,201 calls over thirty days while the chain showed 131,803
settlements in a single day. Grades come from buying the thing with a real wallet and checking what
came back. Nobody pays to appear here and no grade is for sale.</p>

<p class="gclose"><a href="/">See the ratings and the daily settlement tape</a>, or read the
<a href="/notes">field notes</a> for the full list of traps.</p>
</article>
'''

NOTE_TPL = (
    '<article class="fnote" style="max-width:760px">\n'
    '<h2 style="margin:0 0 9px;font-size:23px;letter-spacing:-.015em">{trap}</h2>\n'
    '<p class="fbite">{bite}</p>\n'
    '<p class="fdo"><b>What to do:</b> {do}</p>\n'
    '<p class="fev">{ev}</p>\n'
    '</article>\n'
    '<p class="note" style="margin-top:16px"><a href="/notes">All field notes</a> &middot; '
    '<a href="/">Latest payments</a></p>'
)
LIST_TPL = (
    '<article class="fnote">\n'
    '<h2 style="margin:0 0 7px;font-size:17px"><a href="/notes/{id}" style="color:inherit;text-decoration:none">{trap}</a></h2>\n'
    '<p class="fbite">{bite}</p>\n'
    '<p class="fdo"><b>What to do:</b> {do}</p>\n'
    '<p class="fev">{ev}</p>\n'
    '</article>'
)

for n in NOTES:
    body = NOTE_TPL.format(trap=html.escape(n["trap"]), bite=html.escape(n["bite"]),
                           do=html.escape(n["do"]), ev=html.escape(n["evidence"]))
    _note_ld = json.dumps([
        {"@context": "https://schema.org", "@type": "TechArticle",
         "headline": n["trap"], "description": n["bite"][:300],
         "url": f'{SITE}/notes/{n["id"]}', "datePublished": TODAY,
         "author": PERSON,
         "publisher": {"@type": "Organization", "name": "What Agents Buy", "url": SITE},
         "proficiencyLevel": "Expert",
         "about": {"@type": "Thing", "name": "x402 payment protocol"}},
        json.loads(crumbs(("Home", "/"), ("Field notes", "/notes"), (n["trap"][:60], f'/notes/{n["id"]}')))],
        separators=(",", ":"))
    write(f"notes/{n['id']}/index.html",
          shell(f"{n['trap']} | What Agents Buy field notes", n["bite"][:180], f"/notes/{n['id']}",
                body, ld=_note_ld))
    urls.append(f"/notes/{n['id']}")

write("notes/index.html", site_page(
    "/notes", f"Field notes: {len(NOTES)} traps in paying APIs as software | What Agents Buy",
    "Per-asset decimals, payment challenges hiding in three places, model labels that are not the model, "
    "ticker ambiguity, quote windows and phantom paywalls. Every one learned by getting it wrong first.",
    NOTES_BODY))
urls.append("/notes")
urls.extend(["/leaderboard", "/ratings", "/new-endpoints"])

# --- /studies: the findings, in one place ------------------------------------
# The studies are the reason the ratings exist. Each is a finding that came from
# paying this market and hitting a contrast or a contradiction, not from opinion.
# Until now they only lived interleaved in the feed, one per screen among the
# grades; collecting them gives the findings a home and a hub the rest of the
# site links into, which is how the ratings, thin on traffic, get pulled along.
_study_posts = [p for p in FEED if p.get("kind") == "study"]
_studies_cards = "".join(post_summary(p) for p in _study_posts)
STUDIES_BODY = (
    f'<p class="note"><b>{len(_study_posts)} studies.</b> A rating grades one service. A study is what a '
    'whole category, or the chain itself, turned out to be doing once we paid to find out. Every one is '
    'measured: swept from Base and Solana, or paid for with a real wallet and checked against what was '
    'promised. None is an opinion.</p>'
    f'{_studies_cards}'
    '<p class="note dim" style="margin-top:18px"><a href="/">The full feed</a> &middot; '
    '<a href="/ratings">Every rating</a> &middot; <a href="/leaderboard">Top paid services</a> '
    '&middot; <a href="/delivery">Delivery checks</a> &middot; <a href="/notes">Field notes</a></p>')
write("studies/index.html", site_page(
    "/studies",
    f"Studies: {len(_study_posts)} measured findings on the agentic-commerce market | What Agents Buy",
    "What paying the x402 market with a real wallet actually revealed: how much settles off the chain "
    "everyone reads, whether endpoints deliver what they promise, the economy of manufactured demand, and "
    "an address-poisoning campaign found on-chain. Each finding is measured, not guessed.",
    STUDIES_BODY))
urls.append("/studies")

write("guide/index.html", site_page(
    "/guide", "How to pay an x402 endpoint | What Agents Buy",
    "A practical guide to paying APIs as software: how the x402 handshake works, where the payment "
    "challenge hides, the traps that make working endpoints look broken, and which clients and registries "
    "actually exist. Written from real payments, not from docs.", GUIDE))
urls.append("/guide")

# --- /about ------------------------------------------------------------------
# Review content is weighted by who wrote it and how. Until now there was no page
# saying who grades these services, what the method is, or what the conflicts are.
ABOUT = f'''<article class="guide">
<h2>Who writes this, and how the grades are made</h2>
<p class="glede">What Agents Buy is written by <a href="{CONTACT}">{AUTHOR}</a>, who works on agentic payments and x402
(<a href="{AUTHOR_URL}">neilkpatel.com</a>, <a href="{CONTACT}">LinkedIn</a>). I give an AI agent a real wallet,
let it buy from the APIs other agents are paying for, and publish the receipt: what was quoted, what was actually
charged, and whether the goods arrived. Feedback, corrections and disputes are welcome on
<a href="{CONTACT}">LinkedIn</a>.</p>

<h3>The method</h3>
<ol class="gsteps">
<li><b>Measure the money first.</b> Every day I sweep USDC Transfer logs straight from Base JSON-RPC into the
payTo address each service advertises in its own x402 challenge. Registry call-counters are never used: one
registry credited a service with 4,201 calls over thirty days while the chain showed 131,803 settlements in a
single day.</li>
<li><b>Then buy something.</b> Grades come from paying a service with a real wallet, or from calling it where it
is free. Nothing is graded that was not called. The wallet is
<code>0xC533Bf5268A2F64aDDe58dcE380651f70Aa92D7A</code> and every payment is checkable on Basescan.</li>
<li><b>Grade a behaviour, not a vendor.</b> One service can hold an A for price honesty and a C for accuracy at
the same time, and several do. A grade always says what it is a grade of.</li>
<li><b>Publish the request.</b> Every entry shows the exact call that produced it, so you can disagree with the
input as well as the conclusion.</li>
</ol>

<h3>What the grades mean</h3>
<p>A means it did what it said. F means it took money and did not deliver. In between, the letter is attached to
one named behaviour, and the card says which. Verdicts are <b>Honest</b>, <b>Overcharged</b>, <b>Partial</b> or
<b>No goods</b>. A grade here is not a safety rating and not an endorsement: a well-run service can still be a
bad idea to send money to.</p>

<h3>Conflicts and money</h3>
<p>No service pays to appear here, to be graded, or to be graded again. Nothing on this site is sponsored,
affiliated or paid placement. I hold no position in any service listed. The only money moving is mine, going
out, to buy the things being reviewed. Payments are made through AgentCash, which is named in the entries
because it is the tool doing the paying, not because anyone is paying me.</p>

<h3>Corrections</h3>
<p>I get things wrong and I publish the corrections in place rather than deleting the post. A $10 billion price
quote turned out to be my own decimals bug. A published F was withdrawn within the hour when the on-chain tape
showed the claim was not supported. If you can show a number here is wrong, I would rather hear it than not:
<a href="{CONTACT}">message me on LinkedIn</a>.</p>

<h3>Using this data</h3>
<p>Quote it with attribution and the as-of date. Everything is also machine-readable:
<a href="/api/ratings.json">ratings.json</a>, <a href="/api/leaderboard.json">leaderboard.json</a>,
<a href="/api/field-notes.json">field-notes.json</a>, and the whole site as one document at
<a href="/llms-full.txt">llms-full.txt</a>.</p>

<p class="gclose"><a href="/ratings">Every rating</a> &middot; <a href="/guide">How to pay an x402 endpoint</a>
&middot; <a href="/notes">Field notes</a></p>
</article>'''

_about_ld = json.dumps([
    {"@context": "https://schema.org", "@type": "AboutPage", "url": f"{SITE}/about",
     "name": "Who writes What Agents Buy and how the grades are made",
     "publisher": {"@type": "Organization", "name": "What Agents Buy", "url": SITE},
     "author": PERSON},
    json.loads(crumbs(("Home", "/"), ("About", "/about")))], separators=(",", ":"))

write("about/index.html", site_page(
    "/about", "Who writes What Agents Buy, and how the grades are made",
    "The method behind these x402 API reviews: how settlement is measured on Base, why every grade comes from "
    "a real payment, what the letters mean, and the conflicts policy. No service pays to appear or be graded.",
    ABOUT, ld=_about_ld))
urls.append("/about")

# --- /api ---------------------------------------------------------------------
# The JSON has been public since it shipped, but nothing pointed at it, so a model
# reading the whole site concluded there was no data product. This page is the
# pointer, and it is written for something that intends to consume it.
API_BODY = f'''<article class="guide">
<h2>The data, for machines</h2>
<p class="glede">Every rating, the settlement tape and all the field notes are published as JSON.
No key, no signup, no rate limit. They are static files on a CDN, so call them as often as you
like. Rebuilt whenever the site is, which is at least daily.</p>

<h3>Endpoints</h3>
<ol class="gsteps">
<li><b><a href="/api/ratings.json">/api/ratings.json</a></b>: every grade, best to worst.
Each entry carries <code>service</code>, <code>grade</code>, <code>graded_on</code> (the behaviour
the grade is of), <code>verdict</code>, <code>quoted</code>, <code>charged</code>,
<code>delivered</code>, <code>free_to_call</code>, <code>date</code> and links to the write-up
and the service page. Currently {len(pays)} ratings.</li>
<li><b><a href="/api/leaderboard.json">/api/leaderboard.json</a></b>: who actually receives USDC,
swept daily from Base. Each row carries <code>rank</code>, <code>host</code>,
<code>usdc_received</code>, <code>settlements</code>, <code>paying_wallets</code>,
<code>avg_ticket</code>, <code>sends_back_pct</code>, the <code>address</code> money arrives at,
and any <code>grades</code> we hold. Read the <code>caveat</code> field before quoting the totals:
this is turnover, not revenue.</li>
<li><b><a href="/api/field-notes.json">/api/field-notes.json</a></b>: the traps, each with
<code>trap</code>, <code>what_happens</code>, <code>what_to_do</code> and the
<code>evidence</code> behind it. Currently {len(NOTES)}.</li>
<li><b><a href="/api/receipts.json">/api/receipts.json</a></b>: the dispute ledger, one
content-hashed, self-verifying receipt per paid call. Each carries the <code>promise</code>, the
<code>payment</code> (with the on-chain <code>tx</code>), the <code>delivery</code> as a response
shape (types only, never the goods), the <code>verdict</code> (delivered / short / accurate / off /
inconclusive) and a three-level <code>verify</code> anyone can reproduce. The open format is at
<a href="/receipts/spec">/receipts/spec</a>.</li>
<li><b><a href="/api/preflight.json">/api/preflight.json</a></b>: <b>Preflight</b>, one pre-payment
verdict per seller, the check an agent runs before the x402 402. Each host carries a
<code>light</code> (green / yellow / red = CLEAR / HOLD / ABORT), a <code>score</code>, and the
<code>reasons</code>, folding live price and payTo honesty, phantom paywalls and delivery receipts.
Or call the <code>preflight</code> MCP tool with a URL. See <a href="/preflight">/preflight</a>.</li>
</ol>

<h3>MCP server</h3>
<p class="glede">If your agent speaks MCP, it can query all of this as tools instead of fetching and
joining files. Remote server, nothing to install, no key and no payment:</p>
<pre class="cmd"><code>claude mcp add --transport http whatagentsbuy https://whatagentsbuy.com/mcp</code></pre>
<p><code>preflight</code> is the one to call before your agent pays: pass it a URL and it returns a
single CLEAR / HOLD / ABORT verdict, folding live price and payTo honesty, phantom paywalls and the
delivery receipts into one signal to gate on; pass <code>detail:true</code> for the full payment-safety
read behind it. <code>find_api</code> finds a payable API for a task, ranked by graded accuracy then
reliability then price. <code>most_accurate</code> ranks one objective category (crypto, stock, fx,
gas, balance, weather) by how close each seller's returned value is to a primary source.
<code>get_service</code> returns every grade a host holds, what was quoted against what was charged, and
its demand shape. <code>search_services</code> searches the graded ledger. <code>top_services</code>
ranks by money received or by Organic Demand Score, and the two disagree often.
<code>list_traps</code> returns the field notes, worth reading before you write payment code.
<code>market_summary</code> gives the size of the whole market as measured on chain.</p>
<p class="glede"><b>Wire it into your agent in one line.</b> Put this in your system prompt:</p>
<pre class="cmd"><code>Before paying any x402 / HTTP 402 API, call preflight(url) on the whatagentsbuy MCP and gate on the light (CLEAR / HOLD / ABORT / UNRATED). Always read the payTo and amount out of the live 402 and sign against those, never a listing.</code></pre>
<p class="note dim">Full recipe, including the <code>find_api</code> &rarr; <code>preflight</code> &rarr; pay loop:
<a href="https://github.com/neilkpatel/whatagentsbuy/blob/main/llms-install.md">llms-install.md</a>.</p>
<p class="note dim">The agent-facing surface is free on purpose. This site argues that measurements
should be checkable by anyone, and putting a paywall in front of the machines would contradict it.</p>

<h3>Also machine-readable</h3>
<p><a href="/llms.txt">/llms.txt</a> is the whole site as one page, and
<a href="/llms-full.txt">/llms-full.txt</a> is everything including every bullet of every post.
<a href="/feed.xml">/feed.xml</a> is RSS. Every page carries schema.org structured data:
<code>Review</code> on ratings, <code>TechArticle</code> on field notes, <code>WebAPI</code> on
service pages.</p>

<h3>Try it</h3>
<div class="reqbox"><pre># the worst grades we have handed out
curl -s {SITE}/api/ratings.json \\
  | jq '[.ratings[] | select(.grade | test("^[DF]"))] | .[] | {{service, grade, graded_on}}'

# services taking real money that nobody has bought from yet
curl -s {SITE}/api/leaderboard.json \\
  | jq '[.rows[] | select(.grades == [] and .paying_wallets > 10)]
        | .[] | {{host, usdc_received, paying_wallets}}'

# every trap, as a checklist
curl -s {SITE}/api/field-notes.json | jq -r '.notes[] | "- \\(.trap)"'</pre></div>

<h3>Terms</h3>
<p>CC BY 4.0. Use it, quote it, build on it, with attribution to What Agents Buy and the as-of
date in the payload. Every file carries <code>generated</code>, <code>source</code> and a
<code>method</code> line describing how the numbers were produced. Figures change daily, so read
them rather than caching them. If you find something wrong, <a href="{CONTACT}">tell me on LinkedIn</a>.</p>

<p class="gclose">Built from the same data as <a href="/ratings">the ratings</a> and
<a href="/leaderboard">top paid services</a>. How the measuring works is in
<a href="/about">about</a>.</p>
</article>'''

write("api/index.html", site_page(
    "/api", "Data API: every x402 rating and the settlement tape as JSON | What Agents Buy",
    f"Free JSON of {len(pays)} x402 endpoint ratings, daily USDC settlement rankings from Base, "
    f"and {len(NOTES)} field notes on paying APIs as software. No key, no rate limit, CC BY 4.0.",
    API_BODY))
urls.append("/api")

# --- /checklist ---------------------------------------------------------------
# The field notes are a reference; this is the lesson. One screen, ordered the way
# a buyer actually hits each problem, every line linked to the note that proves it.
# Built from notes.json and asserted against it, so it cannot drift out of sync.
CHECKLIST = [
 ("Before you call", [
  ("two-registries",       "There is no single catalog. Two public registries exist and neither is the whole market."),
  ("registry-counters",    "Ignore registry usage counters. One credited 4,201 calls where the chain showed 131,803 in a day."),
  ("declared-method",      "Call it the way the schema says. A GET to a POST-only endpoint reads as a dead service, not a mistake."),
  ("paywall-before-router", "Check the declared method twice. Some paywalls charge before they route, so a wrong verb costs money and returns HTML."),
 ]),
 ("Reading the price", [
  ("challenge-location",   "Read the live 402 for the price. Never the docs, never the registry listing."),
  ("challenge-usually-header-only",
                           "Look in three places. 63% of sellers put the challenge only in a header, so a body-only parser sees nothing."),
  ("decimals",             "Check the asset's decimals. USDC is 6; BNB-chain tokens are 18. Assuming 6 turned one cent into ten billion dollars here."),
  ("many-options",         "One endpoint can quote several prices. Compare the options before picking one."),
  ("flat-pricing",         "Price does not always scale with the request. Some sellers charge the same for a tenth of the work."),
  ("quote-window",         "Quotes expire, sometimes in 30 seconds. Do not think for a minute and then pay."),
 ]),
 ("Before you pay", [
  ("free-tier-answers-the-paid-question",
                           "Call the free endpoint first. Several sellers answer the same question for nothing on another path."),
  ("cheap-action-expensive-entry",
                           "Check whether the cheap action needs state you can only buy. A three cent call can have a nine dollar entry fee."),
  ("custom-payment-header","A published quote is not a promise. Some sellers only accept their own client."),
  ("phantom-paywalls",     "Ask for a route that cannot exist. Anything that quotes you a price for it is billing on nothing."),
  ("rotating-payto",       "A changing payment address is usually fine. Some sellers mint a fresh one per request; probe twice before accusing."),
 ]),
 ("After you pay", [
  ("client-says-null-chain-says-paid",
                           "Reconcile against the chain. Your client reporting no payment is not proof that no payment was made."),
  ("score-the-transaction-not-the-counterparty",
                           "Check the goods against the promise. Reputation scores cannot see a service that is reachable, well-formed and simply does not deliver."),
  ("cite-the-block-not-the-clock",
                           "Keep the block number, not the clock. A timestamp cannot be reproduced; a block height can."),
  ("model-label-is-not-the-model",
                           "Confirm you got what you paid for. Two differently named models returned the same weights on one router."),
  ("symbol-not-coin",      "A ticker is not an asset. Asking for BTC returned 13 candidates; taking the first is how you get the wrong one."),
  ("payto-from-challenge-not-history",
                           "Read payTo from the challenge every time. 89 counterfeit addresses now sit in one buyer's history, all rendering identically to the real one."),
 ]),
 ("Reading the market", [
  ("turnover-not-revenue", "Money arriving is not a sale. Several top earners pay almost all of it straight back out."),
  ("shared-wallets",       "Many storefronts, one operator. Wallet addresses collapse brands that look independent."),
  ("silent-truncation",    "A catalog fetch that stops early looks exactly like a complete one. Floor-check every pull against the last."),
  ("activity-metrics-are-purchasable",
                           "Never read a registry call counter as demand. One service sells the number outright, $190 a year to pay your own endpoint twice a month."),
 ]),
]

_note_ids = {n["id"] for n in NOTES}
for _sec, _items in CHECKLIST:
    for _nid, _ in _items:
        assert _nid in _note_ids, f"checklist references a field note that no longer exists: {_nid}"

_n = 0
_secs = []
for _sec, _items in CHECKLIST:
    _rows = []
    for _nid, _line in _items:
        _n += 1
        _rows.append(f'<li><span class="cknum">{_n}</span><span class="cktext">{html.escape(_line)} '
                     f'<a href="/notes/{html.escape(_nid)}">why</a></span></li>')
    _secs.append(f'<h3 class="ckh">{html.escape(_sec)}</h3><ol class="cklist">{"".join(_rows)}</ol>')

CHECKLIST_BODY = (
    '<article class="guide">'
    '<h2>Before you let an agent pay an API</h2>'
    f'<p class="glede">{_n} checks, in the order you hit them. Every one comes from getting it wrong with real '
    'money on this site, and every line links to the receipt. If you only read one page here, read this one.</p>'
    + "".join(_secs)
    + '<p class="gclose">The long version of each is in the <a href="/notes">field notes</a>. How the protocol '
      'itself works is in <a href="/guide">how to pay an x402 endpoint</a>. What happened when I paid these '
      'services is in the <a href="/ratings">ratings</a>.</p></article>')

_ck_ld = json.dumps([
    {"@context": "https://schema.org", "@type": "HowTo",
     "name": "Before you let an agent pay an API",
     "description": f"{_n} checks to run before and after an AI agent pays an x402 endpoint, each one learned from a real payment.",
     "url": f"{SITE}/checklist",
     "author": PERSON,
     "step": [{"@type": "HowToStep", "position": i, "name": line.split(".")[0],
               "text": line, "url": f"{SITE}/notes/{nid}"}
              for i, (nid, line) in enumerate(
                  [(a, b) for _s, items in CHECKLIST for a, b in items], 1)]},
    json.loads(crumbs(("Home", "/"), ("Checklist", "/checklist")))], separators=(",", ":"))

write("checklist/index.html", site_page(
    "/checklist", f"Before you let an agent pay an API: {_n} checks | What Agents Buy",
    f"{_n} things to check before and after an AI agent pays an x402 endpoint, in the order you hit them. "
    "Read the live 402 not the docs, look for the challenge in three places, check the asset's decimals, and "
    "reconcile against the chain. Every check learned from a real payment.",
    CHECKLIST_BODY, ld=_ck_ld))
urls.append("/checklist")

# Delivery checks per host: endpoints we PAID and compared against their own
# promised schema.
#
# RE-VERIFIED 2026-08-13. The original harness scored working, delivering
# endpoints as failures because it failed to capture the paid response. That data
# was quarantined, the capture bug in conform.py was fixed, three guardrails were
# added (canary, three-way verdict, re-verify), the raw response is now archived,
# and the whole set was re-graded offline plus re-called on-chain. The clean
# result lives in data/conformance_verified.json; the contaminated files are in
# data/quarantine/ (not globbed). A verdict is now one of three: delivered, short
# (confirmed on two calls), or inconclusive (we could not get a clean paid
# response). Only delivered/short ever render as a delivery result; inconclusive
# is our failure to measure, not the seller's failure to deliver.
_DELIVERY_OK = True
_DELIVERY_BY_HOST = {}
_DELIVERY_INCONCLUSIVE = 0
if _DELIVERY_OK:
    for _cf in sorted(glob.glob(os.path.join(HERE, "data", "conformance_*.json"))):
        try:
            for _cr in json.load(open(_cf)).get("rows", []):
                _st = _cr.get("status")
                if _st == "inconclusive":
                    _DELIVERY_INCONCLUSIVE += 1
                if _st not in ("delivered", "short"):
                    continue  # only a real, measured verdict renders as a result
                _DELIVERY_BY_HOST.setdefault(_cr.get("host", ""), {})[_cr["url"]] = {
                    "status": _st,
                    "conforms": _st == "delivered",
                    "missing": _cr.get("missing") or [],
                    "promised": _cr.get("promised") or [],
                    "extra": _cr.get("extra") or [],
                    "observed_schema": _cr.get("observed_schema"),
                    "latency_ms": _cr.get("ms"),
                }
        except Exception:
            pass

# --- /delivery: the paid delivery checks, everything we verified --------------
# The home for conformance. A human and a bot can both see, per endpoint, whether
# we paid it and whether it returned what its own schema promised. This is the
# one signal free probing cannot produce, so it gets its own page.
_dall = []
for _h, _eps in _DELIVERY_BY_HOST.items():
    for _u, _v in _eps.items():
        _dall.append({"host": _h, "url": _u, **_v})
_dall.sort(key=lambda x: (not x["conforms"], x["host"]))
_d_ok = sum(1 for x in _dall if x["conforms"])
_DGOOD = '<span class="dgood">delivered</span>'
_DBAD = '<span class="dbad">short</span>'
if _dall:
    # The verdict span is built outside the f-string expression on purpose: a
    # backslash inside an f-string {...} fails to parse on Python < 3.12, which
    # made build.py silently un-runnable on the system 3.9. Keep expressions here
    # free of escaped quotes so a fresh or downgraded environment still builds.
    _drows = "".join(
        '<tr>'
        f'<td>{_DGOOD if x["conforms"] else _DBAD}</td>'
        f'<td class="lbsvc"><span class="lbname">{html.escape(x["host"])}</span>'
        f'<span class="sub">{html.escape("/" + x["url"].split("/", 3)[-1] if x["url"].count("/") >= 3 else x["url"])}</span></td>'
        f'<td class="num">{len(x["promised"]) - len(x["missing"])}/{len(x["promised"])}</td>'
        '</tr>'
        for x in _dall)
    _dbody = (
        f'<p class="note"><b>{_d_ok} of {len(_dall)}</b> endpoints we could measure returned everything '
        'their own published schema promised. A delivery check pays an endpoint with its own advertised '
        'input and compares the response against its own advertised output. It is the one thing free '
        'probing cannot see: anyone can read a 402, almost no one pays and checks whether the goods '
        f'arrive. A short means the response was real and genuinely missing fields, confirmed on two '
        f'separate calls before it is recorded here.</p>'
        f'<p class="note dim">A further <b>{_DELIVERY_INCONCLUSIVE}</b> endpoints could not be measured: '
        'they need an API key, rejected our advertised input, or returned a 4xx/5xx. Those are counted '
        'as inconclusive, never as a failure to deliver, because not getting a clean response is our '
        'limitation, not evidence the seller shortchanged anyone.</p>'
        '<p class="note dim">Every payment here is capped at the endpoint\'s own advertised price, so a '
        'seller that quotes more live is refused and recorded rather than paid. This set grows as we test '
        'more of the market. Machine-readable at <a href="/api/delivery.json">/api/delivery.json</a>.</p>'
        '<div class="scroll"><table class="tbl lbtbl"><thead><tr>'
        '<th>Delivered</th><th>Endpoint</th><th>Fields</th></tr></thead>'
        f'<tbody>{_drows}</tbody></table></div>')
else:
    _dbody = '<p class="note">No delivery checks recorded yet.</p>'
write("delivery/index.html", site_page(
    "/delivery", "Delivery checks: does the API return what it promised | What Agents Buy",
    "We pay x402 endpoints with their own advertised input and check the response against their own "
    "published output schema. The delivery signal free probing cannot see: does the API actually return "
    "what it promised.", _dbody))
urls.append("/delivery")
json.dump({"generated": NOW_ISO, "source": SITE, "api_version": 1,
           "license": "CC BY 4.0, attribute What Agents Buy",
           "what": ("Paid delivery checks: each endpoint called with its own advertised input, its response "
                    "compared against its own advertised output schema. Payments capped at the advertised "
                    "price so an overcharge is refused, not paid. status is delivered, short (real response "
                    "missing fields, confirmed on two calls), or inconclusive (no clean paid response, our "
                    "limitation not the seller's). Only delivered and short are counted here."),
           "delivered": _d_ok, "short": len(_dall) - _d_ok, "checked": len(_dall),
           "inconclusive": _DELIVERY_INCONCLUSIVE,
           "endpoints": _dall},
          open(os.path.join(PUB, "api", "delivery.json"), "w"), indent=1)

# --- dispute receipts: the evidence layer, published and self-verifying -------
# Phase 1 built the receipt (receipts.py); this publishes it. Each receipt is a
# portable, content-hashed record of one paid call: what was promised, what was
# paid (with the on-chain tx), what came back (SHAPE only, never the goods), and
# a verdict anyone can re-derive without trusting us. This is the dispute layer
# the agentic market is said to lack, made concrete from calls we already paid.
_RECEIPTS = []
try:
    with open(os.path.join(HERE, "data", "receipts", "receipts.jsonl")) as _rf:
        for _line in _rf:
            _line = _line.strip()
            if _line:
                _RECEIPTS.append(json.loads(_line))
except Exception:
    _RECEIPTS = []

# Guardrail at the publish boundary: a receipt carries the response SHAPE (type
# names only), never a real value from the body. Refuse to render any delivery
# receipt whose schema holds a non-type leaf, so a future change to the ledger
# can never leak a paid response onto the public page.
_RECEIPTS = [r for r in _RECEIPTS if r.get("kind") != "delivery"
             or preflight.shape_is_clean((r.get("delivery") or {}).get("observed_schema"))]
_RECEIPTS_BY_HOST = {}
for _r in _RECEIPTS:
    _RECEIPTS_BY_HOST.setdefault(_r["seller"]["host"], []).append(_r)


def _tx_link(r):
    _tx = (r.get("payment") or {}).get("tx")
    if _tx:
        return f'<a href="https://basescan.org/tx/{_tx}">tx {_tx[:10]}&hellip;</a>'
    return "served free" if (r.get("payment") or {}).get("free") else "no tx recorded"


def _verify3(r):
    _c = r.get("checks") or {}
    _lab = {"integrity": "integrity", "verdict": "verdict", "raw": "raw bytes"}
    _out = []
    for _k in ("integrity", "verdict", "raw"):
        _v = _c.get(_k)
        if _v is True:
            _out.append(f'<span class="vok">&#10003; {_lab[_k]}</span>')
        elif _v is None:
            _out.append(f'<span class="vna">&ndash; {_lab[_k]}</span>')
        else:
            _out.append(f'<span class="vno">&#10007; {_lab[_k]}</span>')
    return '<div class="verify3">' + "".join(_out) + '</div>'


def _receipt_card(r):
    _st = r["verdict"]["status"]
    _host = r["seller"]["host"]
    _q, _ch = r["promise"].get("price_usdc"), r["payment"].get("charged_usdc")
    _evidence = ""
    if r["kind"] == "delivery":
        _pf = r["promise"].get("fields") or []
        _miss = r["delivery"].get("missing") or []
        if _st == "short":
            _badge = '<span class="dbad">underdelivered</span>'
            _mstr = ", ".join(_miss[:8]) + ("&hellip;" if len(_miss) > 8 else "")
            _what = (f'Promised <b>{len(_pf)}</b> fields for <b>${_q}</b>, charged <b>${_ch}</b>, and '
                     f'<b>underdelivered</b>: returned {len(_pf) - len(_miss)} of {len(_pf)}. '
                     f'Missing <code>{html.escape(_mstr)}</code>. Shortfall confirmed on two calls.')
        else:
            _badge = '<span class="dgood">delivered</span>'
            _what = f'Promised <b>{len(_pf)}</b> fields for <b>${_q}</b>, paid, and returned all {len(_pf)}.'
        _sch = r["delivery"].get("observed_schema")
        if _sch is not None:
            _evidence = (f'<p class="rnote">The real response shape, types only, values discarded:</p>'
                         f'<pre><code>{html.escape(json.dumps(_sch, indent=1))}</code></pre>')
    else:
        _metric = html.escape(r["promise"].get("metric", "a value"))
        _ret, _tr = r["delivery"].get("returned"), r.get("truth", {})
        if _st == "accurate":
            _badge = '<span class="dgood">accurate</span>'
            _what = (f'Asked for {_metric}; returned <b>{_ret}</b>, within {html.escape(_tr.get("tolerance", ""))} '
                     f'of <b>{_tr.get("value")}</b> from {html.escape(_tr.get("source", ""))}.')
        elif _st == "off":
            _badge = '<span class="dbad">off</span>'
            _what = (f'Asked for {_metric}; returned <b>{_ret}</b>, <b>off</b> by {html.escape(_tr.get("deviation", ""))} '
                     f'vs <b>{_tr.get("value")}</b> from {html.escape(_tr.get("source", ""))}.')
        else:
            _badge = '<span class="dbad">inconclusive</span>'
            _what = f'Asked for {_metric}; {html.escape(r["verdict"].get("why", ""))}.'
        _evidence = (f'<p class="rnote">Graded against a source that cannot be a reseller: '
                     f'<b>{html.escape(_tr.get("source", ""))}</b>. Returned {_ret} versus a truth of '
                     f'{_tr.get("value")}.</p>')
    return (
        '<article class="rcpt">'
        f'<div class="rcpt-top">{_badge}<code class="rid">{r["receipt_id"]}</code></div>'
        f'<h3 class="rcpt-h"><a href="/s/{_host}">{html.escape(_host)}</a></h3>'
        f'<p class="rcpt-what">{_what}</p>'
        f'<div class="rcpt-facts">{_tx_link(r)}</div>'
        + _verify3(r)
        + '<details class="repro"><summary>Evidence and how to verify it yourself</summary>'
        + _evidence
        + '<p class="rnote">Three independent checks, none needing trust in this site: '
          '<b>integrity</b> recomputes the id as a hash of the evidence, so any edit shows; '
          '<b>verdict</b> re-derives the result offline; '
          '<b>raw</b> re-reads the untouched bytes archived at pay time and reproduces the shape.</p>'
        + f'<div class="cmd"><code>{html.escape(r["verify"]["cmd"])}</code></div>'
        + '</details></article>')


_r_disputes = sorted([r for r in _RECEIPTS if r["verdict"]["status"] == "short"],
                     key=lambda r: r["seller"]["host"])
_r_accurate = sorted([r for r in _RECEIPTS if r["kind"] == "accuracy" and r["verdict"]["status"] == "accurate"],
                     key=lambda r: r["seller"]["host"])
_r_delivered = sum(1 for r in _RECEIPTS if r["verdict"]["status"] == "delivered")
_r_paid_short = sum(float((r["payment"].get("charged_usdc") or 0)) for r in _r_disputes)

if _RECEIPTS:
    _rbody = (
        '<p class="note">Every paid call is captured as a <b>receipt</b>: a portable, tamper-evident record of '
        'what an endpoint promised, what it charged, what it actually returned, and a verdict you can re-derive '
        'yourself. Agentic commerce is said to have working payments but no dispute layer, because nobody keeps '
        'intent-versus-delivery in a form a third party can check. This is that record, built from calls already '
        'paid with a real wallet. A receipt publishes the response <b>shape only</b>, never the goods.</p>'
        f'<p class="note dim"><b>{len(_RECEIPTS)}</b> receipts &middot; <b>{len(_r_disputes)}</b> underdelivered '
        f'&middot; <b>{_r_delivered}</b> delivered in full &middot; <b>{len(_r_accurate)}</b> graded accurate '
        f'against a primary source &middot; ${_r_paid_short:.3f} paid for goods that came up short. '
        'Machine-readable at <a href="/api/receipts.json">/api/receipts.json</a>. How the format works: '
        '<a href="/receipts/spec">the spec</a>.</p>'
        f'<h2 class="rsec">Underdelivered ({len(_r_disputes)})</h2>'
        '<p class="note dim">Each paid its full quoted price and returned a real response that genuinely lacked '
        'promised fields, confirmed on two separate calls before it is recorded. Open any card to see the '
        'evidence and reproduce the verdict.</p>'
        '<div class="rcpt-grid">' + "".join(_receipt_card(r) for r in _r_disputes) + '</div>'
        f'<h2 class="rsec">Accurate against a primary source ({len(_r_accurate)})</h2>'
        '<p class="note dim">A different question from delivery: not whether the fields arrived but whether the '
        "number was right, graded against a source that cannot be a reseller (an exchange median, or the chain's "
        'own balance read). This is the check the automated badge services do not do.</p>'
        '<div class="rcpt-grid">' + "".join(_receipt_card(r) for r in _r_accurate) + '</div>'
        f'<h2 class="rsec">Delivered in full ({_r_delivered})</h2>'
        f'<p class="note">{_r_delivered} endpoints returned every field their own schema promised. They are in '
        'the <a href="/delivery">delivery checks</a> and in <a href="/api/receipts.json">the JSON</a>, each with '
        'its own verifiable receipt.</p>')
else:
    _rbody = '<p class="note">No receipts recorded yet.</p>'
write("receipts/index.html", site_page(
    "/receipts", "Receipts: a verifiable dispute record for x402 agentic commerce | What Agents Buy",
    "Every paid call as a tamper-evident receipt: what was promised, what was charged, what was returned, and a "
    "verdict anyone can re-derive. The dispute-evidence layer agentic payments lack, built from real paid calls.",
    _rbody))
urls.append("/receipts")

json.dump({"generated": NOW_ISO, "source": SITE, "api_version": 1,
           "license": "CC BY 4.0, attribute What Agents Buy",
           "what": ("Dispute receipts: one content-hashed record per paid call. Each carries the promise, the "
                    "payment (with on-chain tx), the response SHAPE (type names only, never the goods), and a "
                    "verdict re-derivable at three levels. status: delivered; short (underdelivered, confirmed on "
                    "two calls); accurate or off (graded against a primary source, not another API); or "
                    "inconclusive (could not measure, never a negative claim against a seller)."),
           "verify": {"integrity": "sha256 over the evidence fields equals receipt_id",
                      "verdict": "re-derive the verdict offline from the stored shape",
                      "raw": "re-read the archived bytes and reproduce the shape",
                      "how": "python3 receipts.py --verify <receipt_id>"},
           "counts": {"receipts": len(_RECEIPTS), "underdelivered": len(_r_disputes),
                      "delivered": _r_delivered, "accurate": len(_r_accurate)},
           "receipts": _RECEIPTS},
          open(os.path.join(PUB, "api", "receipts.json"), "w"), indent=1)

# --- /receipts/spec: the open format (Phase 3) -------------------------------
_r_example = next((r for r in _RECEIPTS if r["receipt_id"] == "wab_01c3cc545cd9d991"),
                  (_r_disputes[0] if _r_disputes else None))
_r_example_json = html.escape(json.dumps(_r_example, indent=1)) if _r_example else "{}"
_spec_body = (
    '<article class="post">'
    '<p class="note">A dispute receipt is a small, open, self-verifying record of a single paid API call. It '
    'exists so that when an AI agent pays for something and does not get it, there is a record a third party can '
    'check, which is the piece agentic commerce is otherwise missing. This page defines the format. It is free '
    'to adopt, attribution appreciated.</p>'

    '<h2 class="rsec">Why</h2>'
    '<p class="note">Payments settle in a second; disputes do not settle at all, because nothing records what '
    'was promised against what was delivered in a form anyone can audit after the fact. A receipt fixes that at '
    'the smallest useful unit: one call. It does not move money or reverse a payment. It makes the facts of a '
    'call portable and checkable, so reputation and resolution can be built on evidence rather than on trust.</p>'

    '<h2 class="rsec">What a receipt records</h2>'
    '<p class="note">Six things, and a content hash over them:</p>'
    '<ul class="blist">'
    '<li><b>The promise</b>: the advertised price and the output the listing said it returns.</li>'
    '<li><b>The order</b>: the exact call made (endpoint, method, input).</li>'
    '<li><b>The payment</b>: what was charged and the on-chain transaction that settled it.</li>'
    '<li><b>The delivery</b>: the response, recorded as its <b>shape</b> (type names only), never the goods, plus '
    'a pointer to the untouched bytes archived at pay time.</li>'
    '<li><b>The truth</b> (for accuracy receipts): the value from a primary source that cannot be a reseller.</li>'
    '<li><b>The verdict</b>: delivered, short (underdelivered), accurate, off, or inconclusive, with the reason.</li>'
    '</ul>'
    '<p class="note">The <code>receipt_id</code> is a sha256 over those evidence fields. Change any one and the '
    'id no longer matches. The record is tamper-evident by construction.</p>'

    '<h2 class="rsec">The three-level verification</h2>'
    '<p class="note">A receipt is arbitrable without trusting its publisher, at three independent levels:</p>'
    '<ul class="blist">'
    '<li><b>Integrity</b>: recompute the hash of the evidence and confirm it equals the id. Detects any edit.</li>'
    '<li><b>Verdict</b>: re-derive delivered/short/accurate from the stored evidence, offline, no network.</li>'
    '<li><b>Raw</b>: pull the untouched bytes the seller returned (archived at pay time, matched by endpoint and '
    'transaction) and re-derive the response shape from scratch, proving the evidence itself is faithful.</li>'
    '</ul>'
    '<p class="note">Reference implementation: <code>python3 receipts.py --verify &lt;receipt_id&gt;</code> runs '
    'all three. A verdict reached by a two-call re-verify cannot be reproduced from one stored shape, so it is '
    'reported honestly as not-applicable at the offline level and confirmed at the raw level instead.</p>'

    '<h2 class="rsec">Opening and resolving a dispute</h2>'
    '<p class="note">A dispute is opened by pointing at a receipt whose verdict is <b>short</b> or <b>off</b>. The '
    'receipt is the whole case file: promise, payment, delivery, and an independent verdict already in it. The '
    'seller may contest by submitting its own call, which is recorded as another receipt and graded the same way. '
    'Because every verdict re-derives from archived bytes, resolution is a reproducible fact, not an opinion. The '
    'consequence is reputational, recorded against the seller\'s public reliability, not a forced refund, because '
    'a third party\'s legitimate power here is evidence and reputation, not settlement.</p>'

    '<h2 class="rsec">The shape of a receipt</h2>'
    '<p class="note">A real underdelivered receipt from this site, in full:</p>'
    f'<pre class="speccode"><code>{_r_example_json}</code></pre>'
    '<p class="note dim"><a href="/receipts">Back to the receipts</a> &middot; '
    '<a href="/api/receipts.json">The whole ledger as JSON</a></p>'
    '</article>')
write("receipts/spec/index.html", site_page(
    "/receipts/spec", "The dispute receipt: an open, verifiable format for x402 disputes | What Agents Buy",
    "The open format for agentic-commerce dispute receipts: what a receipt records, how its verdict re-derives "
    "at three independent levels without trusting the publisher, and how a dispute is opened and resolved.",
    _spec_body))
urls.append("/receipts/spec")

# --- Greenlight: one pre-payment verdict per seller ---------------------------
# The oracle. Everything the site measures, collapsed into one signal an agent
# can gate on before it pays: green (nothing alarming), yellow (pay with care),
# red (do not pay without checking the live 402), gray (unrated). Built from the
# same daily data, exposed as /api/greenlight.json, a badge on every seller page,
# and the greenlight() MCP tool. Whatever it says, always read the payTo and the
# amount from the live 402 and sign against those.
_LB_BY_HOST = {}
try:
    for _r in LB["windows"]["1d"]["rows"]:
        _LB_BY_HOST[(_r.get("host") or "").lower()] = _r
except Exception:
    pass

# payment-safety signals per host, from the same source as /api/probe.json but
# computed here so the verdict can badge each seller page before the probe block.
# The same pass captures each seller's advertised text, so the verdict can also
# flag a category whose data is free upstream.
_PROBE_BY_HOST = {}
_HOST_TEXT = {}
try:
    for _r in json.load(open(os.path.join(HERE, "data", "latest.json")))["origins"]:
        _h = (_r.get("origin") or "").lower()
        if _h:
            _HOST_TEXT[_h] = (str(_r.get("blurb") or "") + " " + str(_r.get("service") or "") + " "
                              + str(_r.get("descriptions") or "") + " " + str(_r.get("tags") or "")).lower()
        _cs = [c for c in (_r.get("checked") or []) if not c.get("inconclusive")]
        if not _cs:
            continue
        _mism = any(c.get("adv_amount") and c.get("live_amount")
                    and abs(c["adv_amount"] - c["live_amount"]) / max(c["adv_amount"], c["live_amount"]) > 0.02
                    for c in _cs)
        _ptm = any((c.get("adv_payto") or "") and (c.get("live_payto") or "")
                   and c["adv_payto"].lower() != c["live_payto"].lower() for c in _cs)
        _PROBE_BY_HOST[_h] = {
            "price_ok": not _mism, "payto_ok": not _ptm, "phantom": bool(_r.get("phantom"))}
except Exception:
    pass

# The verdict logic lives in preflight.py as pure functions so it can be tested,
# because a false ABORT is a public accusation. build.py just looks up the
# per-host inputs and calls it.
def _greenlight(host):
    host = (host or "").lower().replace("www.", "").split("/")[0]
    return preflight.verdict(
        host, _PROBE_BY_HOST.get(host), _RECEIPTS_BY_HOST.get(host, []),
        _LB_BY_HOST.get(host), preflight.free_category(_HOST_TEXT.get(host, "")))


_GL_HOSTS = set(_PROBE_BY_HOST) | set(_RECEIPTS_BY_HOST) | set(_LB_BY_HOST)
_GREENLIGHT = {h: _greenlight(h) for h in _GL_HOSTS}

_GL_LABEL = {"green": "CLEAR", "yellow": "HOLD", "red": "ABORT", "gray": "UNRATED"}


def _gl_badge(light):
    return f'<span class="glpill gl-{light}">{_GL_LABEL.get(light, light.upper())}</span>'


def _gl_card(v):
    _rz = "".join(f'<li class="glr-{r["level"]}">{html.escape(r["text"])}</li>'
                  for r in v["reasons"] if r["level"] != "good")
    _sc = f' &middot; score {v["score"]}' if v["score"] is not None else ""
    return (f'<article class="glcard"><div class="glcard-top">{_gl_badge(v["light"])}'
            f'<a class="glhost" href="/s/{html.escape(v["host"])}">{html.escape(v["host"])}</a>'
            f'<span class="glmeta">{v["receipts"]} receipt{"s" if v["receipts"] != 1 else ""}{_sc}</span></div>'
            f'<ul class="gllist">{_rz}</ul></article>')


from collections import Counter as _Counter
_gl_counts = _Counter(v["light"] for v in _GREENLIGHT.values())
json.dump({"generated": NOW_ISO, "source": SITE, "api_version": 1,
           "license": "CC BY 4.0, attribute What Agents Buy",
           "what": ("Preflight: one pre-payment verdict per seller, the check an agent runs before the x402 "
                    "402. green = nothing alarming found; yellow = pay with care; red = do not pay without "
                    "checking; gray = unrated. Aggregates payment safety (live price and payTo vs the listing, "
                    "phantom paywalls), delivery history (verifiable receipts) and demand realness. WHATEVER "
                    "THIS SAYS, read the payTo and the amount from the live 402 on every call and sign against "
                    "those."),
           "lights": {"green": "nothing alarming found, safe to pay", "yellow": "pay with care, see reasons",
                      "red": "do not pay without checking the live 402", "gray": "unrated, no data yet"},
           "counts": dict(_gl_counts),
           "sellers": _GREENLIGHT},
          open(os.path.join(PUB, "api", "preflight.json"), "w"), indent=1)

_gl_all = sorted(_GREENLIGHT.values(), key=lambda v: (v["score"] if v["score"] is not None else 999, v["host"]))
_gl_red = [v for v in _gl_all if v["light"] == "red"]
_gl_yellow = [v for v in _gl_all if v["light"] == "yellow"]
_gl_yellow_shown = _gl_yellow[:60]
_glbody = (
    '<p class="note"><b>Preflight</b> is the one call an agent makes before it pays an unfamiliar '
    '<b>x402</b> API, the check it runs before it hits the 402. It collapses everything measured here, live '
    'price and payment-address honesty, phantom paywalls, and the delivery <a href="/receipts">receipts</a>, '
    'into a single verdict it can gate on: '
    '<span class="glpill gl-green">CLEAR</span> nothing alarming, '
    '<span class="glpill gl-yellow">HOLD</span> pay but verify, '
    '<span class="glpill gl-red">ABORT</span> do not pay without checking, '
    '<span class="glpill gl-gray">UNRATED</span> no data yet.</p>'
    f'<p class="note dim"><b>{_gl_counts.get("green", 0)}</b> clear &middot; <b>{_gl_counts.get("yellow", 0)}</b> '
    f'hold &middot; <b>{_gl_counts.get("red", 0)}</b> abort &middot; <b>{_gl_counts.get("gray", 0)}</b> '
    f'unrated, across {len(_GREENLIGHT)} x402 sellers. '
    f'<b>{sum(1 for v in _GREENLIGHT.values() if v.get("free_alternative"))}</b> of them advertise a '
    f'category you can get free upstream (an exchange price, weather, a chain read, a web search), and each '
    f'verdict says so. Machine-readable at '
    '<a href="/api/preflight.json">/api/preflight.json</a>, or have an agent call the '
    '<a href="/api">preflight MCP tool</a>. The contract is at <a href="/preflight/spec">/preflight/spec</a>. '
    'Whatever it says, always read the payTo and the amount from '
    'the live 402 and sign against those, never a listing.</p>'
    f'<h2 class="rsec">Abort ({len(_gl_red)})</h2>'
    + ('<p class="note dim">A live payment address that disagrees with the listing, a phantom paywall, or a '
       'paid call that returned none of its promised goods. Check the live 402 before paying any of these.</p>'
       '<div class="rcpt-grid">' + "".join(_gl_card(v) for v in _gl_red) + '</div>'
       if _gl_red else '<p class="note">None flagged to abort right now.</p>')
    + f'<h2 class="rsec">Hold ({len(_gl_yellow)})</h2>'
    '<p class="note dim">Payable, but with something to verify: a live quote that disagrees with the listing, '
    'or a paid call that came up short of its promised fields.</p>'
    '<div class="rcpt-grid">' + "".join(_gl_card(v) for v in _gl_yellow_shown) + '</div>'
    + (f'<p class="note dim">And {len(_gl_yellow) - len(_gl_yellow_shown)} more in '
       '<a href="/api/preflight.json">the JSON</a>.</p>' if len(_gl_yellow) > len(_gl_yellow_shown) else '')
    + f'<h2 class="rsec">Cleared ({_gl_counts.get("green", 0)})</h2>'
    f'<p class="note">{_gl_counts.get("green", 0)} sellers show nothing alarming: their live price and payTo '
    'match their listing and no paid call has come up short. Each carries its own verdict at '
    '<a href="/api/preflight.json">/api/preflight.json</a> and a badge on its '
    'service page. A clear preflight is a payment-safety verdict, not a promise the goods are worth buying.</p>')
write("preflight/index.html", site_page(
    "/preflight", "Preflight: the x402 pre-payment check for AI agents | What Agents Buy",
    "The one call an agent makes before paying an x402 API, the check it runs before the 402: a single "
    "CLEAR / HOLD / ABORT verdict folding live price and payTo honesty, phantom paywalls and delivery "
    "receipts into one gradable signal.",
    _glbody))
urls.append("/preflight")

# --- /preflight/spec: the open contract --------------------------------------
_pf_example = next((v for v in _gl_red if v.get("reasons")), (_gl_all[0] if _gl_all else None))
_pf_example_json = html.escape(json.dumps(_pf_example, indent=1)) if _pf_example else "{}"
_pfspec_body = (
    '<article class="post">'
    '<p class="note"><b>Preflight</b> is a single, open, pre-payment verdict for one x402 seller, the check '
    'an agent runs before it hits the 402. This page defines the contract: what the verdict means, what goes '
    'into it, and how to act on it. It is free to adopt, attribution appreciated.</p>'

    '<h2 class="rsec">The verdict</h2>'
    '<p class="note">One <code>light</code>, with a human label and a rule for what an agent should do:</p>'
    '<ul class="blist">'
    '<li><span class="glpill gl-green">CLEAR</span> <code>green</code>: nothing alarming found. Safe to pay '
    'on payment-safety grounds. Not a promise the goods are worth buying.</li>'
    '<li><span class="glpill gl-yellow">HOLD</span> <code>yellow</code>: payable, but resolve the reasons '
    'first (a live quote that disagrees with the listing, or a paid call that came up short).</li>'
    '<li><span class="glpill gl-red">ABORT</span> <code>red</code>: do not pay without checking the live 402. '
    'The address or the goods have failed before.</li>'
    '<li><span class="glpill gl-gray">UNRATED</span> <code>gray</code>: no data yet. Absence is not a bad '
    'sign, but nothing has been verified.</li>'
    '</ul>'
    '<p class="note">The machine value is <code>light</code> (green / yellow / red / gray); CLEAR / HOLD / '
    'ABORT is only its label. A <code>score</code> from 0 to 100 accompanies it for a numeric gate.</p>'

    '<h2 class="rsec">What goes into it</h2>'
    '<p class="note">The verdict folds four independent signals, each measured, none sponsored:</p>'
    '<ul class="blist">'
    '<li><b>Payment safety</b>: does the live 402 price match the listing, does the live payTo match the '
    'listing, is the paywall real. From probing the whole directory, free.</li>'
    '<li><b>Delivery</b>: has a paid call to this host come up short of its promised fields, confirmed on two '
    'calls. From the <a href="/receipts">receipts</a>.</li>'
    '<li><b>Demand realness</b>: does its on-chain revenue come from many wallets or one, and does most of it '
    'leave again. Context, does not move the light.</li>'
    '<li><b>Free upstream</b>: is this a category an agent can get for nothing (an exchange price, weather, a '
    'chain read, a web search). Informational, does not move the light.</li>'
    '</ul>'

    '<h2 class="rsec">The one rule for a red light</h2>'
    '<p class="note">ABORT is a strong claim, so it fires only from evidence that money or goods have actually '
    'gone wrong, never a soft signal: a <b>payTo that disagrees with the listing</b>, a <b>phantom paywall</b> '
    '(a price quoted for a route that cannot exist), or a <b>reverified severe underdeliver</b> (paid in full '
    'and returned none of two or more promised fields, confirmed on two calls). Demand and free-upstream flags '
    'are context and never turn a light red.</p>'

    '<h2 class="rsec">How an agent uses it</h2>'
    '<p class="note">Call <code>preflight(url)</code> on the <a href="/api">MCP server</a>, or GET the host out '
    'of <a href="/api/preflight.json">/api/preflight.json</a>. Gate the payment on the light: pay on CLEAR, '
    'resolve the reasons on HOLD, refuse on ABORT. Then, whatever the verdict, <b>read the payTo and the amount '
    'out of the live 402 on every call and sign against those, never a listing</b>. A verdict is a prior, not '
    'a substitute for reading the challenge you are about to pay.</p>'

    '<h2 class="rsec">The shape of a verdict</h2>'
    '<p class="note">A real ABORT verdict, in full:</p>'
    f'<pre class="speccode"><code>{_pf_example_json}</code></pre>'
    '<p class="note dim"><a href="/preflight">Back to Preflight</a> &middot; '
    '<a href="/api/preflight.json">The whole set as JSON</a></p>'
    '</article>')
write("preflight/spec/index.html", site_page(
    "/preflight/spec", "The Preflight contract: an open pre-payment verdict for x402 | What Agents Buy",
    "The open contract for Preflight, the pre-payment verdict an agent gates on before paying an x402 API: "
    "what CLEAR / HOLD / ABORT mean, the four signals behind them, and the one rule for a red light.",
    _pfspec_body))
urls.append("/preflight/spec")

# --- /categories: the corpus leaderboard, each category vs a primary source ---
# The readable repository. For every accuracy category the daily lab grades, rank
# the sellers by how close they came to the primary source, so a buyer sees which
# API returns the most accurate stock quote / crypto price / weather / fx / gas /
# balance, and at what cost. This data exists nowhere else.
_LAB = {}
try:
    _LAB = json.load(open(os.path.join(HERE, "data", "lab.json"))).get("categories", {})
except Exception:
    _LAB = {}

_CAT_TITLES = {"crypto-price": "Crypto price (BTC/USD)", "stock-price": "Stock price (AAPL)",
               "fx-rate": "FX rate (EUR/USD)", "gas-price": "Gas price (Base)",
               "wallet-balance": "Wallet balance (USDC)", "weather": "Weather (New York)"}

if _LAB:
    _cat_html, _api_cats = [], {}
    _n_graded = 0
    for _cat in sorted(_LAB):
        _c = _LAB[_cat]
        _tol = _c.get("tol", 100)
        _unit = _c.get("unit", "")
        _graded = [r for r in _c.get("rows", []) if r.get("value") is not None]
        for _r in _graded:
            _r["_ok"] = abs(_r.get("dev") or 0) <= _tol
        _graded.sort(key=lambda r: (abs(r.get("dev") or 0), r.get("quoted") or 9))
        _n_graded += len(_graded)
        if not _graded:
            continue
        _trs = ""
        for _i, _r in enumerate(_graded, 1):
            _vspan = ('<span class="dgood">accurate</span>' if _r["_ok"]
                      else '<span class="dbad">off</span>')
            _off = (("%+g " % _r["dev"]) + _unit) if _r.get("dev") is not None else "&mdash;"
            _trs += ('<tr>'
                     f'<td class="num">{_i}</td>'
                     f'<td class="lbsvc"><a class="lbname" href="/s/{html.escape(_r["host"])}">{html.escape(_r["host"])}</a></td>'
                     f'<td class="num">{_r.get("value")}</td>'
                     f'<td class="num">{_off}</td>'
                     f'<td class="num">${_r.get("quoted")}</td>'
                     f'<td>{_vspan}</td></tr>')
        _cat_html.append(
            f'<h2 class="rsec">{_CAT_TITLES.get(_cat, _cat)}</h2>'
            f'<p class="note dim">Graded against <b>{html.escape(str(_c.get("source", "")))}</b> '
            f'(reference {_c.get("reference")}). {len(_graded)} sellers returned a usable value, '
            f'ranked by accuracy then price.</p>'
            '<div class="scroll"><table class="tbl lbtbl"><thead><tr>'
            '<th>#</th><th>Seller</th><th>Returned</th><th>Off by</th><th>Price</th><th>Verdict</th>'
            '</tr></thead><tbody>' + _trs + '</tbody></table></div>')
        _api_cats[_cat] = {"metric": _c.get("metric"), "source": _c.get("source"),
                           "reference": _c.get("reference"), "unit": _unit, "tolerance": _tol,
                           "sellers": [{"host": r["host"], "value": r.get("value"),
                                        "deviation": r.get("dev"), "price_usdc": r.get("quoted"),
                                        "verdict": "accurate" if r["_ok"] else "off"} for r in _graded]}
    _catbody = (
        '<p class="note">The corpus. Every seller in an objective category, paid and graded against a primary '
        'source an agent cannot get through them, an exchange median, a professional stock feed, the ECB, the '
        'chain, a weather model. This is the data behind the accuracy verdicts, and it does not exist anywhere '
        'else. Ranked by how close each seller came, then by price.</p>'
        f'<p class="note dim"><b>{_n_graded}</b> sellers graded across <b>{len(_api_cats)}</b> categories. '
        'Machine-readable at <a href="/api/categories.json">/api/categories.json</a>. Every grade is a '
        'verifiable <a href="/receipts">receipt</a>.</p>'
        + "".join(_cat_html))
    write("categories/index.html", site_page(
        "/categories", "Accuracy leaderboards: which x402 API returns the right number | What Agents Buy",
        "Every objective-data seller graded against a primary source: which x402 API returns the most accurate "
        "stock price, crypto price, weather, FX rate, gas price or wallet balance, and at what cost.",
        _catbody))
    urls.append("/categories")
    json.dump({"generated": NOW_ISO, "source": SITE, "api_version": 1,
               "license": "CC BY 4.0, attribute What Agents Buy",
               "what": ("Per-category accuracy corpus: every seller in an objective category, paid and graded "
                        "against a primary source (exchange median, FMP, the ECB, the chain, a weather model), "
                        "ranked by deviation then price. verdict is accurate or off; sellers that returned no "
                        "usable value are omitted here and marked inconclusive in the receipts."),
               "categories": _api_cats},
              open(os.path.join(PUB, "api", "categories.json"), "w"), indent=1)

# --- one page per graded service ---------------------------------------------
# "is <service> legit" is the search people actually type, and until now a
# service with two grades had them at two unrelated URLs and no home. This page
# collects every grade, every receipt and the live settlement figures in one
# place, and it gets stronger each time the same service is rated again.
_svc_posts = {}
for _p in FEED:
    _nm = (_p.get("api") or {}).get("name")
    if is_host(_nm) and _p.get("grade"):
        _svc_posts.setdefault(_nm, []).append(_p)

# What each host actually sells, and what the registry claims about it. All of
# this was already on disk and none of it was on the page, which left the one URL
# about a service thinner than the post that graded it.
_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
_REG_BY_HOST, _FIRST_SEEN = {}, {}
try:
    _reg_raw = json.load(open(os.path.join(HERE, "data", "cdp_resources_raw.json")))
    for _r in _reg_raw:
        _h = (urlparse(_r.get("resource") or "").hostname or "").lower()
        if not _h:
            continue
        _amts = [int(a["amount"]) / 1e6 for a in (_r.get("accepts") or [])
                 if (a.get("asset") or "").lower() == _USDC
                 and str(a.get("amount", "")).isdigit()]
        _nets = {a.get("network") for a in (_r.get("accepts") or []) if a.get("network")}
        _REG_BY_HOST.setdefault(_h, []).append({
            "url": _r.get("resource"), "desc": (_r.get("description") or "").strip(),
            "price": min(_amts) if _amts else None, "networks": _nets,
            "calls30d": (_r.get("quality") or {}).get("l30DaysTotalCalls"),
            "payers30d": (_r.get("quality") or {}).get("l30DaysUniquePayers"),
            "last_called": (_r.get("quality") or {}).get("lastCalledAt"),
        })
except Exception as _e:
    print(f"  note: registry detail unavailable for service pages ({_e})")
try:
    for _u, _d0 in json.load(open(os.path.join(HERE, "data", "first_seen.json"))).items():
        _h = (urlparse(_u).hostname or "").lower()
        if _h and (_h not in _FIRST_SEEN or _d0 < _FIRST_SEEN[_h]):
            _FIRST_SEEN[_h] = _d0
except Exception:
    pass


def _chain_name(net):
    """CAIP-2 identifiers are correct and unreadable. "eip155:8453" tells a buyer
    nothing; "Base" tells them whether their wallet can pay it."""
    n = (net or "").lower()
    return {"eip155:8453": "Base", "eip155:1": "Ethereum", "eip155:84532": "Base Sepolia",
            "eip155:137": "Polygon", "eip155:42161": "Arbitrum", "eip155:10": "Optimism",
            "eip155:56": "BNB Chain", "eip155:43114": "Avalanche"}.get(
        n, "Solana" if n.startswith("solana") else (net or "an unnamed chain"))


def _svc_trend(addr):
    """Daily USDC into one payment address across the whole tape, oldest first.
    Keyed on the address rather than the host because the address is what the
    sweep records and what a reader can check on Basescan."""
    out = []
    if not addr:
        return out
    for _f in sorted(glob.glob(os.path.join(HERE, "data", "history", "settlements_*.json"))):
        try:
            _d = json.load(open(_f))
        except Exception:
            continue
        _v = (_d.get("by_address") or {}).get(addr.lower()) or {}
        out.append((_d.get("date"), round(_v.get("usdc", 0.0), 2)))
    return out


for _host, _plist in _svc_posts.items():
    _plist = sorted(_plist, key=lambda x: grade_rank(x.get("grade")))
    _lb_row = None
    if LB:
        _lb_row = next((r for r in LB["windows"]["1d"]["rows"] if r["host"] == _host), None)
    _grades = [x["grade"] for x in _plist]
    _chips = "".join(f'<span class="g g{g[0]}">{html.escape(g)}</span>' for g in _grades)
    _desc = (SERVICES.get(_host, {}) or {}).get("desc") or (_plist[0].get("endpoint") or "")
    # Seller descriptions arrive with newlines collapsed, so a markdown heading
    # shows up inline as "... no subscriptions. ## No wallet?". Cut at the heading.
    _desc = re.split(r"\s*#{1,6}\s|\n\n", _desc)[0].strip().rstrip(".").strip()
    if _desc:
        _desc += "."

    _facts = []
    if _lb_row:
        _facts += [("Received, 24h", f'${_lb_row["usdc"]:,.2f}'),
                   ("Payments", f'{_lb_row["settlements"]:,}'),
                   ("Paying wallets", str(_lb_row["payers"]))]
        if _lb_row.get("avg_ticket"):
            _facts.append(("Average ticket", f'${_lb_row["avg_ticket"]:,.4f}'))
        if _lb_row.get("circular"):
            _facts.append(("Sends back out", f'{_lb_row["payout_pct"]:.0f}%'))
    _paid = [x for x in _plist if (x.get("verdict") or {}).get("charged")]
    if _paid:
        _facts.append(("Times bought from", str(len(_paid))))
    _facts_html = ("".join(f'<div><b>{html.escape(v)}</b><span>{html.escape(k)}</span></div>'
                           for k, v in _facts))

    _rows = []
    for _x in _plist:
        _v = _x.get("verdict") or {}
        _call = (f'<span class="vtag v-{CALL_CLS.get(_v["call"], "warn")}">{html.escape(_v["call"])}</span>'
                 if _v.get("call") else '<span class="dim">no purchase</span>')
        _rows.append(
            f'<a class="svcrow" href="/p/{html.escape(_x["id"])}">'
            f'<span class="g g{_x["grade"][0]}">{html.escape(_x["grade"])}</span>'
            f'<span class="svcmain"><b>{html.escape(_x.get("title",""))}</b>'
            f'<span class="sub">{html.escape(_x.get("graded",""))} &middot; {_x["ts"].split(" ")[0]}</span></span>'
            f'{_call}</a>')

    # ---- demand, said in full rather than as one label ----------------------
    _demand_html = ""
    if _lb_row and _lb_row.get("organic_score") is not None:
        _op = _lb_row.get("organic_parts") or {}
        _sc = _lb_row["organic_score"]
        _tone = "ods-hi" if _sc >= 70 else ("ods-mid" if _sc >= 40 else "ods-lo")
        _bars = "".join(
            f'<div class="odsbar"><span>{k}</span>'
            f'<i style="width:{(v or 0) / m * 100:.0f}%"></i><b>{v or 0:g}/{m}</b></div>'
            for k, v, m in (("breadth", _op.get("breadth"), 40),
                            ("spread", _op.get("dispersion"), 40),
                            ("repeat", _op.get("repeat"), 20)))
        _circ = ""
        if _lb_row.get("payout_pct") is not None:
            _circ = (f'<p class="note dim">It sends <b>{_lb_row["payout_pct"]:.0f}%</b> of what it '
                     f'receives back out again. '
                     + ("Most of the money leaving again is a flag, not a verdict: it can equally be "
                        "cost of goods. Read it beside the score."
                        if _lb_row.get("circular") else "Money largely stays.") + '</p>')
        _demand_html = (
            f'<h3 class="svch3">Is the demand real?</h3>'
            f'<div class="odsbox"><div class="odsnum {_tone}">{_sc}<span>/100</span></div>'
            f'<div class="odsbars">{_bars}</div></div>'
            f'<p class="note dim">Organic Demand Score measures the <b>shape</b> of the money and not '
            f'its size or its honesty: how many distinct wallets paid, how evenly the dollars fell '
            f'across them, and how many came back. Revenue is excluded on purpose, because a big '
            f'number is not evidence anyone wanted the product. These are also the exact quantities a '
            f'wash trader would optimise, so it is a starting point and not a verdict.</p>{_circ}')

    # ---- what it sells, from the registry ------------------------------------
    _eps = _REG_BY_HOST.get(_host, [])
    _sells_html = ""
    if _eps:
        _priced = sorted([e for e in _eps if e["price"]], key=lambda e: e["price"])
        _nets = sorted({n for e in _eps for n in e["networks"]})
        _rangetxt = (f'${_priced[0]["price"]:.4f} to ${_priced[-1]["price"]:.4f}'
                     if len(_priced) > 1 else
                     (f'${_priced[0]["price"]:.4f}' if _priced else "not priced in USDC"))
        _list = "".join(
            f'<li><code>${e["price"]:.4f}</code> {html.escape((e["desc"] or e["url"] or "")[:118])}</li>'
            for e in _priced[:8])
        _more = (f'<li class="dim">and {len(_priced) - 8} more</li>' if len(_priced) > 8 else "")
        _sells_html = (
            f'<h3 class="svch3">What it sells</h3>'
            f'<p class="svcline"><b>{len(_eps)}</b> endpoint{"s" if len(_eps) != 1 else ""} listed, '
            f'priced {_rangetxt}'
            + (f', settling on {", ".join(sorted({_chain_name(n) for n in _nets}))}' if _nets else "") + '.</p>'
            + (f'<ul class="eplist">{_list}{_more}</ul>' if _list else ""))

    # ---- what the registry claims against what the chain shows ---------------
    _claim_html = ""
    _c30 = sum(e["calls30d"] or 0 for e in _eps)
    if _c30 and _lb_row and _lb_row.get("settlements"):
        _claim_html = (
            f'<h3 class="svch3">Registry claim against the chain</h3>'
            f'<p class="svcline">The directory credits this service with <b>{_c30:,}</b> call'
            f'{"s" if _c30 != 1 else ""} over 30 days. The chain shows '
            f'<b>{_lb_row["settlements"]:,}</b> settled payment'
            f'{"s" if _lb_row["settlements"] != 1 else ""} in the last 24 hours alone. '
            'Registry counters are not demand and can be bought, so they are shown here for '
            'contrast rather than as a measurement.</p>')

    # ---- is it growing or dying ---------------------------------------------
    _trend_html = ""
    _tr = _svc_trend((_lb_row or {}).get("address"))
    _tr = [t for t in _tr if t[0]]
    if len(_tr) >= 3:
        _mx = max(v for _, v in _tr) or 1
        _bars2 = "".join(
            f'<div class="tbar" title="{d}: ${v:,.2f}">'
            f'<i style="height:{max(2, v / _mx * 100):.0f}%"></i><span>{d[-2:]}</span></div>'
            for d, v in _tr)
        _first, _last = _tr[0][1], _tr[-1][1]
        _dir = ("grew" if _last > _first * 1.15 else
                "shrank" if _last < _first * 0.85 else "held roughly flat")
        _trend_html = (
            f'<h3 class="svch3">Growing or dying?</h3>'
            f'<div class="tchart">{_bars2}</div>'
            f'<p class="note dim">Daily USDC into this address across the tape, '
            f'{_tr[0][0]} to {_tr[-1][0]}. It {_dir} over that window. '
            f'A missing day is a day the sweep did not run, not a day with no revenue.</p>')

    # --- did it deliver what it promised? (paid check) ------------------------
    _delivery_html = ""
    _dchecks = _DELIVERY_BY_HOST.get(_host, {})
    if _dchecks:
        _ok = sum(1 for v in _dchecks.values() if v["conforms"])
        _rows_d = []
        for _u, _v in list(_dchecks.items())[:6]:
            _path = "/" + _u.split("/", 3)[-1] if _u.count("/") >= 3 else _u
            _tag = (f'<span class="dgood">delivered</span> returned all {len(_v["promised"])} promised fields'
                    if _v["conforms"] else
                    f'<span class="dbad">short</span> missing {len(_v["missing"])} of {len(_v["promised"])}')
            _lat = f' &middot; {_v["latency_ms"]:,}ms' if _v.get("latency_ms") else ""
            _extra = (f' &middot; {len(_v["extra"])} bonus field(s)' if _v.get("extra") else "")
            _shape = ""
            if _v.get("observed_schema"):
                _shape = (f'<details class="repro"><summary>Real response shape</summary>'
                          f'<pre><code>{html.escape(json.dumps(_v["observed_schema"], indent=1))}</code></pre>'
                          f'<p class="rnote">Types only, verified from a real paid call. Values discarded.</p></details>')
            _rows_d.append(f'<li>{_tag} <code>{html.escape(_path[:52])}</code>{_lat}{_extra}{_shape}</li>')
        _delivery_html = (
            f'<h3 class="svch3">Did it deliver what it promised?</h3>'
            f'<p class="svcline"><b>{_ok} of {len(_dchecks)}</b> endpoint'
            f'{"s" if len(_dchecks) != 1 else ""} we paid returned everything their own '
            f'published schema promised.</p>'
            f'<ul class="dlist">{"".join(_rows_d)}</ul>'
            f'<p class="note dim">This is a paid delivery check: we called the endpoint with its own '
            f'advertised input and compared the response against its own advertised output. It is the one '
            f'thing free probing cannot see.</p>')

    # --- verifiable receipts for this host ------------------------------------
    _receipts_html = ""
    _hrs = _RECEIPTS_BY_HOST.get(_host, [])
    if _hrs:
        _hdisp = [r for r in _hrs if r["verdict"]["status"] in ("short", "off")]
        _shown = (_hdisp or _hrs)[:4]
        _tail = (f', including <b>{len(_hdisp)}</b> where it underdelivered' if _hdisp
                 else ', all delivered or graded accurate')
        _receipts_html = (
            f'<h3 class="svch3">Verifiable receipts</h3>'
            f'<p class="svcline">{len(_hrs)} paid call{"s" if len(_hrs) != 1 else ""} to this host are recorded '
            f'as tamper-evident receipts{_tail}. Each can be re-verified independently, from the archived bytes '
            f'up.</p>'
            f'<div class="rcpt-grid">{"".join(_receipt_card(r) for r in _shown)}</div>'
            f'<p class="note dim">The full record and the format are at <a href="/receipts">/receipts</a>.</p>')

    # --- Preflight verdict: the pre-payment oracle for this host --------------
    _pf = _GREENLIGHT.get(_host)
    _pf_html = ""
    if _pf:
        _pf_reasons = "".join(f'<li class="glr-{r["level"]}">{html.escape(r["text"])}</li>'
                              for r in _pf["reasons"] if r["level"] != "good")
        _pf_html = (
            f'<div class="pfverdict">{_gl_badge(_pf["light"])}'
            f'<span class="pflabel"><b>Preflight</b> verdict before paying this host'
            + (f' &middot; score <b>{_pf["score"]}</b>' if _pf["score"] is not None else "")
            + f' &middot; <a href="/preflight">what this means</a></span></div>'
            + (f'<ul class="gllist" style="margin:-8px 0 14px">{_pf_reasons}</ul>' if _pf_reasons else ""))

    _meta_bits = []
    if _FIRST_SEEN.get(_host):
        _meta_bits.append(f'first seen in the registry on {_FIRST_SEEN[_host]}')
    if _lb_row and _lb_row.get("shared_wallet"):
        _meta_bits.append('<b>shares a payment wallet with other listings</b>, so brands that look '
                          'independent may be one operator')
    if _lb_row and _lb_row.get("address"):
        _meta_bits.append(f'paid at <a href="https://basescan.org/address/{_lb_row["address"]}">'
                          f'{_lb_row["address"][:10]}…{_lb_row["address"][-6:]}</a>')
    _meta_html = (f'<p class="note dim svcmeta">{"; ".join(_meta_bits).capitalize()}.</p>'
                  if _meta_bits else "")

    _body = (f'<article class="svcpage">'
             f'<div class="svchead"><div class="svcgrades">{_chips}</div>'
             f'<div><h2>{html.escape(_host)}</h2>'
             f'<p class="svcdesc">{html.escape(_desc[:240])}</p></div></div>'
             + _pf_html
             + (f'<div class="statcard svcfacts">{_facts_html}</div>' if _facts else "")
             + f'<h3 class="svch3">Every rating</h3>{"".join(_rows)}'
             + _demand_html + _delivery_html + _receipts_html + _sells_html + _trend_html + _claim_html + _meta_html
             + '<p class="note dim" style="margin-top:18px">Grades here come from paying this service and '
               'recording what happened, or from calling it where it is free. Settlement figures are swept '
               f'daily from Base USDC Transfer logs. Nothing on this page is sponsored.</p>'
             + '<p class="note" style="margin-top:14px"><a href="/ratings">All ratings</a> &middot; '
               '<a href="/leaderboard">Top paid services</a> &middot; <a href="/">Latest</a></p></article>')

    _t = f'{_host}: {"/".join(_grades)} on x402 | What Agents Buy'
    _d = (f'{_host} graded {" and ".join(_grades)} after paying it directly. '
          f'{_desc[:110]} What was quoted, what was actually charged, and whether the goods arrived.')
    _svc_ld = json.dumps([
        {"@context": "https://schema.org", "@type": "WebAPI", "name": _host,
         "url": f"https://{_host}", "description": _desc[:300],
         "provider": {"@type": "Organization", "name": _host},
         "subjectOf": [{"@type": "Review", "url": f'{SITE}/p/{x["id"]}',
                        "name": x.get("title", ""), "datePublished": x["ts"].split(" ")[0],
                        "author": PERSON,
                        "reviewRating": {"@type": "Rating", "ratingValue": x["grade"],
                                         "bestRating": "A+", "worstRating": "F"}}
                       for x in _plist]},
        json.loads(crumbs(("Home", "/"), ("Ratings", "/ratings"), (_host, f"/s/{_host}")))],
        separators=(",", ":"))
    write(f"s/{_host}/index.html", site_page(f"/s/{_host}", _t, _d, _body, ld=_svc_ld))
    urls.append(f"/s/{_host}")

# Remove permalink pages for posts and notes that no longer exist. Without this,
# unpublishing something leaves it reachable at its old URL.
import shutil
for folder, keep in (("p", {po["id"] for po in FEED}), ("notes", {n["id"] for n in NOTES})):
    base = os.path.join(PUB, folder)
    if not os.path.isdir(base):
        continue
    for name in os.listdir(base):
        d = os.path.join(base, name)
        if os.path.isdir(d) and name not in keep:
            shutil.rmtree(d)
            print(f"  removed stale page /{folder}/{name}")

sm = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
# lastmod tells a crawler what actually changed, which is the whole point of
# re-reading a sitemap. Posts and notes carry their own date; everything else
# moves whenever the daily tape does.
_lastmod = {}
for _p in FEED:
    _lastmod[f'/p/{_p["id"]}'] = _p["ts"].split(" ")[0]
for _n in NOTES:
    _lastmod.setdefault(f'/notes/{_n["id"]}', TODAY)
_site_day = LB["last_day"] if LB else TODAY
sm += "".join(
    f'<url><loc>{SITE}{u}</loc><lastmod>{_lastmod.get(u, _site_day)}</lastmod></url>\n'
    for u in urls)
sm += "</urlset>\n"
open(os.path.join(PUB, "sitemap.xml"), "w").write(sm)
open(os.path.join(PUB, "robots.txt"), "w").write(
    "User-agent: *\nAllow: /\n\n"
    "# Assistants and their crawlers are welcome; this site is about them.\n"
    "# Named explicitly so the permission is unambiguous rather than inherited.\n"
    + "".join(f"User-agent: {b}\nAllow: /\n\n" for b in (
        "GPTBot", "OAI-SearchBot", "ChatGPT-User",
        "Google-Extended", "Googlebot", "Google-CloudVertexBot",
        "ClaudeBot", "Claude-User", "Claude-SearchBot", "anthropic-ai",
        "PerplexityBot", "Perplexity-User",
        "Bingbot", "CCBot", "Applebot", "Applebot-Extended",
        "meta-externalagent", "Amazonbot", "Bytespider",
        "MistralAI-User", "cohere-ai", "DuckDuckBot", "YouBot", "Diffbot"))
    + "# The whole site as text, for anything that would rather read than crawl.\n"
    f"# {SITE}/llms.txt      the site in one page\n"
    f"# {SITE}/llms-full.txt every post and field note in full\n"
    f"# {SITE}/mcp           MCP server, if you would rather call tools\n\n"
    + f"Sitemap: {SITE}/sitemap.xml\n")

# --- llms.txt: the site as one readable page, for models rather than crawlers ---
_ll = [f"# What Agents Buy", "",
       "> Independent measurement of the x402 agent economy. Every USDC payment agents make on Base "
       "is swept straight off the chain daily, and the endpoints agents pay are bought from with a "
       "real wallet and graded on what actually arrives.", "",
       f"Written by {HANDLE} ({CONTACT}). Site: {SITE}. Feed: {SITE}/feed.xml", "",
       f"Full text: {SITE}/llms-full.txt · JSON: {SITE}/api/ratings.json, {SITE}/api/leaderboard.json, {SITE}/api/field-notes.json",
       f"MCP server (free, no key): {SITE}/mcp — tools preflight, find_api, most_accurate, get_service, search_services, top_services, list_traps, market_summary.",
       f"  claude mcp add --transport http whatagentsbuy {SITE}/mcp",
       "Integrate: give your agent one rule, \"call preflight(url) before paying any x402 API and gate on the "
       "light\". Full drop-in recipe: https://github.com/neilkpatel/whatagentsbuy/blob/main/llms-install.md", ""]
if LB:
    _w = LB["windows"]["1d"]
    _ll += ["## The market, last 24 hours", "",
            f'- Settled: ${_w["total_usdc"]:,.0f} USDC across {_w["total_settlements"]:,} payments',
            f'- Services tracked: {len(O):,}',
            f'- Measured on Base, tape runs {LB["first_day"]} to {LB["last_day"]}',
            "- Method: Base JSON-RPC USDC Transfer logs, joined to the payTo address each service "
            "advertises in its own x402 challenge. Registry call-counters are not used; they "
            "understate real volume by orders of magnitude.", "",
            "### Top earners", ""]
    for _i, _r in enumerate(_w["rows"][:10], 1):
        _flag = f' (pays back out {_r["payout_pct"]:.0f}% of it)' if _r.get("circular") else ""
        _ll.append(f'{_i}. {_r["service"]} ({_r["host"]}) — ${_r["usdc"]:,.0f} on '
                   f'{_r["settlements"]:,} payments from {_r["payers"]} wallets{_flag}')
    _ll.append("")
_ll += ["## Ratings", "",
        "Grades run A+ to F and apply to a named behaviour, not to a vendor, so one service can hold "
        "two. Verdicts are Honest, Overcharged, Partial, or No goods. Every rating is backed by a "
        "real payment from wallet 0xC533Bf5268A2F64aDDe58dcE380651f70Aa92D7A, verifiable on Basescan.", ""]
for _p in pays_sorted:
    _v = _p.get("verdict") or {}
    _svc = (_p.get("api") or {}).get("name") or _p.get("title", "")
    _line = f'- **{_svc}** — {_p["grade"]}'
    if _p.get("graded"):
        _line += f' on {_p["graded"]}'
    if _v.get("call"):
        _line += f'. {_v["call"]}: quoted {_v.get("quoted")}, charged {_v.get("charged")}, ' \
                 f'goods {"delivered" if _v.get("delivered") else "not delivered"}'
    else:
        _line += ". Graded without completing a purchase"
    _ll.append(f'{_line}. {SITE}/p/{_p["id"]}')
# Studies are the original research on this site and were missing from both text
# files entirely, which meant the two strongest findings were invisible to
# anything that reads llms.txt instead of crawling.
_studies = [p for p in FEED if p.get("kind") in ("study", "news")]
if _studies:
    _ll += ["", "## Studies", "",
            "Findings that came from measuring the market rather than from buying one endpoint. "
            "These are the original research; nothing here is a purchase review.", ""]
    for _p in _studies:
        _subj = (_p.get("api") or {}).get("name") or ""
        _ll.append(f'- **{_p.get("title","")}**'
                   + (f' (subject: {_subj})' if _subj else "")
                   + f' {SITE}/p/{_p["id"]}')

_ll += ["", "## Preflight (call this before your agent pays)", "",
        f"One pre-payment verdict per x402 seller, the check to run before the 402: CLEAR (nothing alarming), "
        f"HOLD (pay but verify), ABORT (do not pay without checking the live 402), UNRATED. Folds live price "
        f"and payTo honesty, phantom paywalls and delivery receipts into one signal to gate on. "
        f"{_gl_counts.get('green', 0)} clear, {_gl_counts.get('yellow', 0)} hold, {_gl_counts.get('red', 0)} "
        f"abort across {len(_GREENLIGHT)} sellers. Whatever it says, read the payTo and amount from the live "
        f"402 and sign against those.",
        f"- Verdicts: {SITE}/preflight",
        f"- Machine-readable: {SITE}/api/preflight.json",
        f"- MCP tool: call preflight(url) at {SITE}/mcp", ""]
if _LAB:
    _ll += ["", "## Accuracy corpus (which API returns the right number)", "",
            "Every seller in an objective category, paid and graded against a primary source it cannot get "
            "through these vendors: exchange median for crypto price, a professional feed (FMP) for stock "
            "quotes, the ECB for FX, the chain for gas and wallet balance, a weather model for weather. Ranked "
            "by how close each came, then price, so the top rows are the cheapest accurate sellers. This data "
            "exists nowhere else.",
            f"- Leaderboards: {SITE}/categories",
            f"- Machine-readable: {SITE}/api/categories.json",
            f"- MCP tool: call most_accurate(category) at {SITE}/mcp", ""]
_ll += ["", "## Dispute receipts", "",
        f"Every paid call recorded as a portable, tamper-evident receipt: what was promised, what was charged "
        f"(with the on-chain transaction), what came back (response shape only, never the goods), and a verdict "
        f"anyone can re-derive at three levels without trusting this site. {len(_RECEIPTS)} receipts, "
        f"{len(_r_disputes)} underdelivered, {_r_delivered} delivered in full, {len(_r_accurate)} graded accurate "
        f"against a primary source. This is the dispute-evidence layer agentic commerce is otherwise missing.",
        f"- Receipts: {SITE}/receipts",
        f"- The open format (spec): {SITE}/receipts/spec",
        f"- Machine-readable ledger: {SITE}/api/receipts.json", ""]
_ll += ["", "## Field notes", "",
        "Traps found while paying these endpoints, each with what to do about it.", ""]
_ll += [f'- **{n["trap"]}** — {n["bite"]} {SITE}/notes/{n["id"]}' for n in NOTES]
_ll += ["", "## Reuse", "",
        "Figures may be quoted with attribution to What Agents Buy and the as-of date shown above. "
        "The numbers change daily; re-read this file rather than caching them.", ""]
open(os.path.join(PUB, "llms.txt"), "w").write("\n".join(_ll))

# --- machine-readable layer ---------------------------------------------------
# A site about agents buying things should be readable by an agent. These are
# stable, documented shapes so anyone can consume the ratings without scraping.
os.makedirs(os.path.join(PUB, "api"), exist_ok=True)

_ratings = []
for _p in pays_sorted:
    _v = _p.get("verdict") or {}
    _ratings.append({
        "service": (_p.get("api") or {}).get("name"),
        "grade": _p.get("grade"),
        "graded_on": _p.get("graded"),
        "verdict": _v.get("call"),
        "quoted": _v.get("quoted"),
        "charged": _v.get("charged"),
        "delivered": _v.get("delivered"),
        "free_to_call": bool(_p.get("free")),
        "date": _p["ts"].split(" ")[0],
        "title": _p.get("title"),
        "url": f'{SITE}/p/{_p["id"]}',
        "service_url": f'{SITE}/s/{(_p.get("api") or {}).get("name")}',
    })
json.dump({"generated": NOW_ISO, "source": SITE,
           "license": "CC BY 4.0, attribute What Agents Buy",
           "method": ("Every grade comes from paying the service with a real wallet and recording what happened, "
                      "or from calling it where it is free. No service pays to appear or to be graded."),
           "grade_scale": GRADE_ORDER, "count": len(_ratings), "ratings": _ratings},
          open(os.path.join(PUB, "api", "ratings.json"), "w"), indent=1)

def reliability(returns_402, checked, price_ok, payto_ok, phantom, median_ms):
    """A 0-100 payment-safety score for one seller, from free probe signals.

    This answers the agent's pre-payment question: will it respond, will it
    charge what it says, and will the money go where the catalogue says. It is
    NOT a delivery score. Whether the goods actually arrive is conformance, which
    costs money to measure and is reported separately where we have it.

    Components, each visible so the number can be taken apart:
      responds     30   returns a proper 402 when called unpaid
      price honest 25   live quote matches the advertised listing
      payTo honest 25   money goes to the address the listing names
      real         10   not a phantom paywall (a price for a route that can't exist)
      fast         10   median challenge latency under ~1s
    """
    parts = {}
    parts["responds"] = round(30 * (returns_402 / checked)) if checked else 0
    parts["price_honest"] = 25 if price_ok else 0
    parts["payto_honest"] = 25 if payto_ok else 0
    parts["real"] = 0 if phantom else 10
    if median_ms is None:
        parts["fast"] = 5
    elif median_ms <= 1000:
        parts["fast"] = 10
    elif median_ms <= 3000:
        parts["fast"] = 6
    else:
        parts["fast"] = 2
    return sum(parts.values()), parts


# --- /api/probe.json: the whole listed market, checked without paying ----------
# The question an agent has before spending is narrow: will this URL answer, does
# its live price match its listing, and does the money go where the catalogue
# says. Every one of those is answerable from a 402 alone, which costs nothing,
# so it can cover the entire directory rather than the handful we have bought.
try:
    _pr = json.load(open(os.path.join(HERE, "data", "latest.json")))["origins"]
except Exception:
    _pr = []
if _pr:
    def _live(r):
        return [c for c in (r.get("checked") or []) if not c.get("inconclusive")]
    _prows = []
    for r in _pr:
        cs = _live(r)
        if not cs:
            continue
        mism = [c for c in cs if c.get("adv_amount") and c.get("live_amount")
                and abs(c["adv_amount"] - c["live_amount"]) / max(c["adv_amount"], c["live_amount"]) > 0.02]
        ptm = [c for c in cs if (c.get("adv_payto") or "") and (c.get("live_payto") or "")
               and c["adv_payto"].lower() != c["live_payto"].lower()]
        lat = sorted(c["ms"] for c in cs if c.get("ms"))
        _n402 = sum(1 for c in cs if c.get("status") == 402)
        _mms = lat[len(lat) // 2] if lat else None
        _rel, _relparts = reliability(_n402, len(cs), not mism, not ptm,
                                      bool(r.get("phantom")), _mms)
        _prows.append({
            "host": r["origin"],
            "endpoints_listed": r.get("breadth"),
            "endpoints_checked": len(cs),
            "answered": True,
            "reliability": _rel,
            "reliability_parts": _relparts,
            "returns_402_unpaid": _n402,
            "challenge_priceable": sum(1 for c in cs if c.get("payable")),
            "price_matches_listing": not mism,
            "price_mismatches": [{"url": c["url"], "listed": c["adv_amount"],
                                  "live": c["live_amount"]} for c in mism][:5],
            "payto_matches_listing": not ptm,
            "phantom_paywall": bool(r.get("phantom")),
            "median_ms": _mms,
            "free_tier": bool(r.get("free_tier")),
            "graded_by_purchase": BOUGHT_GRADE.get(r["origin"], []),
        })
    _prows.sort(key=lambda x: x["host"])
    _dead = len(_pr) - len(_prows)
    json.dump({
        "generated": NOW_ISO, "source": SITE,
        "license": "CC BY 4.0, attribute What Agents Buy",
        "method": ("Every listed origin was called without paying. A 402 challenge carries the price and "
                   "the payment address, so the live quote can be compared against the catalogue listing "
                   "for free. No payment was made to collect any of this."),
        "caveat": ("price_matches_listing false means the live 402 asked for a different amount than the "
                   "directory advertises, which is the seller's own quote disagreeing with its own listing. "
                   "payto_matches_listing false means money would go to an address the catalogue does not "
                   "name, which is the one to check before paying anything."),
        "origins_listed": len(_pr),
        "origins_that_answered": len(_prows),
        "origins_silent": _dead,
        "count": len(_prows),
        "origins": _prows,
    }, open(os.path.join(PUB, "api", "probe.json"), "w"), indent=1)

    # --- /api/catalog.json: what an agent can discover and pay -----------------
    # The registry lists 15k endpoints; this is the payable, reachable subset
    # joined with each host's reliability, so an agent can search by task and get
    # a ranked shortlist it can actually pay. Kept compact on purpose: one row per
    # endpoint, description trimmed, so the whole thing is a single cheap fetch.
    _rel_by_host = {r["host"]: r for r in _prows}
    # Delivery evidence: whether an endpoint returned what its own schema promised.
    # This is the moat signal, the one thing free probing cannot see, keyed by URL.
    # Only delivered/short carry a delivered_as_promised flag; an inconclusive row
    # (no clean paid response) stays None, the same as never tested, because we
    # cannot honestly claim it did or did not deliver. See _DELIVERY_OK above.
    _conf_by_url = {}
    for _cf in (sorted(glob.glob(os.path.join(HERE, "data", "conformance_*.json"))) if _DELIVERY_OK else []):
        try:
            for _cr in json.load(open(_cf)).get("rows", []):
                if _cr.get("status") in ("delivered", "short"):
                    _conf_by_url[_cr["url"]] = {
                        "conforms": _cr.get("status") == "delivered",
                        "schema": _cr.get("observed_schema"),
                        "latency_ms": _cr.get("ms"),
                    }
        except Exception:
            pass
    _grade_by_host = {}
    for _p in FEED:
        _h = (_p.get("api") or {}).get("name")
        if _p.get("grade") and is_host(_h):
            _grade_by_host.setdefault(_h, []).append(_p["grade"])
    try:
        _reg_cat = json.load(open(os.path.join(HERE, "data", "cdp_resources_raw.json")))
    except Exception:
        _reg_cat = []
    _cat = []
    for _r in _reg_cat:
        _url = _r.get("resource") or ""
        _host = (urlparse(_url).hostname or "").lower()
        _relrow = _rel_by_host.get(_host)
        if not (_host and _url.startswith("http") and _relrow):
            continue  # only endpoints on hosts we actually reached
        _amts = [int(a["amount"]) / 1e6 for a in (_r.get("accepts") or [])
                 if (a.get("asset") or "").lower() == _USDC and str(a.get("amount", "")).isdigit()]
        _nets = sorted({_chain_name(a.get("network")) for a in (_r.get("accepts") or []) if a.get("network")})
        _bz = ((_r.get("extensions") or {}).get("bazaar") or {}).get("info", {})
        _cat.append({
            "host": _host, "url": _url,
            "description": (_r.get("description") or "")[:220],
            "price_usdc": round(min(_amts), 6) if _amts else None,
            "method": (_bz.get("input", {}) or {}).get("method", "GET"),
            "chains": _nets,
            "reliability": _relrow["reliability"],
            "price_honest": _relrow["price_matches_listing"],
            "payto_honest": _relrow["payto_matches_listing"],
            "grades": _grade_by_host.get(_host, []),
            # None = never paid to check; True/False = we paid and it did/didn't
            # return what its own schema promised.
            "delivered_as_promised": (_conf_by_url.get(_url) or {}).get("conforms"),
            # The real observed response shape (types only, no values), verified
            # from a paid call. The integration signal an agent wants before it
            # pays. Present only where we have tested delivery.
            "observed_schema": (_conf_by_url.get(_url) or {}).get("schema"),
        })
    _cat.sort(key=lambda x: (-(x["reliability"] or 0), x["price_usdc"] if x["price_usdc"] is not None else 9e9))
    json.dump({
        "generated": NOW_ISO, "source": SITE,
        "license": "CC BY 4.0, attribute What Agents Buy",
        "what": ("Every payable endpoint on a host we reached, with its live-checkable price, the chains "
                 "it settles on, and its host reliability score. Search this to find an API for a task, "
                 "then always read the payTo out of the live 402 before paying. Reliability is a "
                 "payment-safety score, not a delivery guarantee."),
        "count": len(_cat),
        "endpoints": _cat,
    }, open(os.path.join(PUB, "api", "catalog.json"), "w"), indent=1)
    globals()["_CATALOG_COUNT"] = len(_cat)

    # --- /api/search-index.json: the whole corpus, searchable client-side -------
    # One compact index so a visitor can type any API name and instantly see what
    # we know about it. Every entry is labelled by how much we actually verified:
    # graded (we paid and graded it), measured (we swept settlement or tested
    # delivery), or listed (in the registry, reachable, not yet tested). That
    # measured-vs-claimed label is the point: it never presents a self-reported
    # registry entry as if we had checked it.
    _lb_hosts = set()
    if LB:
        for _wrows in LB["windows"].values():
            for _lr in _wrows["rows"]:
                if _lr.get("host"):
                    _lb_hosts.add(_lr["host"])
    _agg = {}
    for _e in _cat:
        _h = _e["host"]
        _a = _agg.setdefault(_h, {"host": _h, "n": 0, "price": None, "rel": _e["reliability"],
                                  "grades": _e["grades"], "desc": _e["description"][:70],
                                  "delivered": None})
        _a["n"] += 1
        if _e["price_usdc"] is not None:
            _a["price"] = _e["price_usdc"] if _a["price"] is None else min(_a["price"], _e["price_usdc"])
        if _e.get("delivered_as_promised") is not None:
            _a["delivered"] = bool(_e["delivered_as_promised"]) if _a["delivered"] is None \
                else (_a["delivered"] and bool(_e["delivered_as_promised"]))
    _si = []
    for _h, _a in _agg.items():
        _graded = bool(_a["grades"])
        _measured = (not _graded) and (_a["delivered"] is not None or _h in _lb_hosts)
        _status = "graded" if _graded else ("measured" if _measured else "listed")
        _si.append({"t": "host", "label": _h,
                    "url": (f"/s/{_h}" if _graded else None),
                    "status": _status,
                    "grade": (_a["grades"][0] if _a["grades"] else None),
                    "price": _a["price"], "rel": _a["rel"], "n": _a["n"],
                    "delivered": _a["delivered"], "desc": _a["desc"]})
    # Every graded service belongs in the index even if it dropped out of today's
    # catalog: its /s/ page still exists and "is X reviewed" is the query we most
    # want to answer. is_host() guards against grades attached to non-host subjects.
    for _gh, _grs in _grade_by_host.items():
        if is_host(_gh) and _gh not in _agg:
            _si.append({"t": "host", "label": _gh, "url": f"/s/{_gh}", "status": "graded",
                        "grade": _grs[0], "price": None, "rel": None, "n": None,
                        "delivered": None, "desc": ""})
    for _p in FEED:
        _api = (_p.get("api") or {}).get("name") or ""
        _si.append({"t": _p.get("kind", "rating"), "label": _p.get("title", "")[:140],
                    "url": f"/p/{_p['id']}", "status": "post",
                    "kw": (" ".join(_p.get("tags", [])) + " " + _api).lower().strip()})
    for _n in NOTES:
        _si.append({"t": "note", "label": _n["trap"][:140], "url": f"/notes/{_n['id']}",
                    "status": "note", "kw": (_n.get("bite", "") or "")[:80].lower()})
    json.dump({"generated": NOW_ISO, "source": SITE, "count": len(_si),
               "legend": {"graded": "we paid and graded it", "measured": "we swept its settlement or tested delivery",
                          "listed": "in the registry and reachable, not yet tested by us",
                          "post": "a study or rating", "note": "a field note"},
               "items": _si}, open(os.path.join(PUB, "api", "search-index.json"), "w"),
              separators=(",", ":"))
    globals()["_SEARCH_COUNT"] = len(_si)

if LB:
    _w = LB["windows"]["1d"]
    json.dump({"generated": NOW_ISO, "source": SITE,
               "license": "CC BY 4.0, attribute What Agents Buy",
               "method": ("USDC Transfer logs swept daily from Base JSON-RPC into the payTo address each service "
                          "advertises in its own x402 challenge. Registry call-counters are not used."),
               "window": "24h", "as_of": LB["last_day"],
               "tape_from": LB["first_day"], "tape_to": LB["last_day"],
               "total_usdc": _w["total_usdc"], "total_settlements": _w["total_settlements"],
               "caveat": ("Ranked by money received, which is turnover rather than profit. sends_back_pct at or "
                          "above 90 means most of what arrived left the same wallet in the window."),
               "organic_demand_score": ("0 to 100 describing the shape of a service's demand, not its size: "
                                        "breadth of paying wallets (40, log scale topping out at 100), evenness "
                                        "of dollars across them (40, from the Herfindahl index), and share of "
                                        "buyers who returned (20, topping out at 30%). Revenue and outflow are "
                                        "excluded by design. These are the same quantities a wash trader would "
                                        "optimise, so read it alongside sends_back_pct rather than alone."),
               "chains_measured": ["base", "solana"],
               "chain_note": ("usdc_received and settlements are cross-chain totals. The demand-shape "
                              "fields (sends_back_pct, buyer_hhi, organic_demand_score) are measured on "
                              "Base only, shown by demand_measured_on, because outflow and payer "
                              "concentration are not swept on Solana."),
               "rows": [{"rank": i, "service": r["service"], "host": r["host"],
                         "usdc_received": r["usdc"], "settlements": r["settlements"],
                         "chains": r.get("chains"), "demand_measured_on": r.get("demand_measured_on"),
                         "paying_wallets": r["payers"], "avg_ticket": r.get("avg_ticket"),
                         "sends_back_pct": r.get("payout_pct"), "address": r["address"],
                         "repeat_buyers": r.get("repeat_payers"),
                         "top_buyer_share": r.get("top_payer_share"),
                         "top5_buyer_share": r.get("top5_payer_share"),
                         "buyer_hhi": r.get("payer_hhi"),
                         "demand": r.get("demand"),
                         "organic_demand_score": r.get("organic_score"),
                         "organic_demand_parts": r.get("organic_parts"),
                         "organic_demand_confidence": r.get("organic_confidence"),
                         "grades": BOUGHT_GRADE.get(r["host"], [])}
                        for i, r in enumerate(_w["rows"], 1)]},
              open(os.path.join(PUB, "api", "leaderboard.json"), "w"), indent=1)

json.dump({"generated": NOW_ISO, "source": SITE,
           "notes": [{"trap": n["trap"], "what_happens": n["bite"], "what_to_do": n["do"],
                      "evidence": n["evidence"], "url": f'{SITE}/notes/{n["id"]}'} for n in NOTES]},
          open(os.path.join(PUB, "api", "field-notes.json"), "w"), indent=1)

# --- /api/index.json: one manifest so an agent can integrate safely ------------
# The single document a tool should read first: every data endpoint, the MCP
# server and its tools, the stability promise, and where to start. Field names in
# the JSON APIs are stable within api_version 1; a breaking change bumps it.
json.dump({
    "name": "What Agents Buy",
    "what": ("Independent measurement of the APIs behind agentic commerce. We pay x402 endpoints with a "
             "real wallet and publish what was quoted, charged and delivered, sweep every USDC payment on "
             "Base and Solana, and probe the whole directory for free. Nothing is sponsored."),
    "api_version": 1,
    "generated": NOW_ISO,
    "source": SITE,
    "license": "CC BY 4.0, attribute What Agents Buy",
    "stability": ("Field names in these JSON endpoints are stable within api_version 1. New fields may be "
                  "added; existing ones are not renamed or removed without bumping api_version. Numbers "
                  "change daily, so re-read rather than cache; each response carries a generated timestamp."),
    "start_here": ("If you are an agent deciding who to pay: call find_api on the MCP server to discover a "
                   "service (or most_accurate to rank an objective category by graded accuracy), then "
                   "preflight on your pick (detail:true for the full payment-safety read), then read the "
                   "amount and payTo out of the live 402 before signing."),
    "mcp": {
        "url": f"{SITE}/mcp",
        "transport": "streamable-http",
        "install": f"claude mcp add --transport http whatagentsbuy {SITE}/mcp",
        "auth": "none",
        "tools": [
            {"name": "preflight", "use": "one CLEAR/HOLD/ABORT verdict before your agent pays an x402 API, folding price and payTo honesty, phantom paywalls and delivery receipts; detail:true adds the full payment-safety read (live-402 honesty, demand, grades)"},
            {"name": "find_api", "use": "discover a payable API for a task, ranked by graded accuracy then reliability then price"},
            {"name": "most_accurate", "use": "rank an objective category (crypto/stock/fx/gas/balance/weather) by how close each seller's returned value is to a primary source"},
            {"name": "get_service", "use": "every grade, demand shape and score we hold for one host"},
            {"name": "search_services", "use": "search the graded ledger"},
            {"name": "top_services", "use": "rank sellers by revenue or organic demand"},
            {"name": "list_traps", "use": "the field notes: known ways paying an API goes wrong"},
            {"name": "market_summary", "use": "size of the market settled on chain in the last day"},
        ],
    },
    "data_endpoints": [
        {"path": "/api/catalog.json", "what": "every payable endpoint on a reachable host, with price, chains, reliability and grades",
         "count": globals().get("_CATALOG_COUNT"), "key_fields": ["host", "url", "price_usdc", "chains", "reliability", "price_honest", "payto_honest", "grades"]},
        {"path": "/api/probe.json", "what": "the whole directory checked without paying: who answers, price/payTo honesty, reliability",
         "key_fields": ["host", "reliability", "price_matches_listing", "payto_matches_listing", "phantom_paywall", "median_ms"]},
        {"path": "/api/leaderboard.json", "what": "who actually gets paid, cross-chain (Base + Solana), swept daily",
         "key_fields": ["host", "usdc_received", "chains", "demand_measured_on", "sends_back_pct", "organic_demand_score"]},
        {"path": "/api/ratings.json", "what": "every grade earned by paying a service",
         "key_fields": ["service", "grade", "graded_on", "verdict", "quoted", "charged", "delivered"]},
        {"path": "/api/field-notes.json", "what": "traps found while paying APIs, with what to do",
         "key_fields": ["trap", "what_happens", "what_to_do", "evidence"]},
        {"path": "/api/receipts.json", "what": "the dispute ledger: one content-hashed, self-verifying receipt per paid call (promise, payment with on-chain tx, response shape only, verdict, three-level verify). The dispute-evidence layer agentic commerce lacks",
         "count": len(_RECEIPTS), "key_fields": ["receipt_id", "kind", "seller", "promise", "payment", "delivery", "verdict", "verify"]},
        {"path": "/api/preflight.json", "what": "Preflight: one pre-payment verdict per seller (green/yellow/red = CLEAR/HOLD/ABORT), folding live price and payTo honesty, phantom paywalls and delivery receipts. The check an agent runs before the x402 402",
         "count": len(_GREENLIGHT), "key_fields": ["host", "light", "score", "reasons", "receipts", "disputes"]},
        {"path": "/api/categories.json", "what": "the accuracy corpus: every seller in an objective category (crypto/stock/fx/gas/balance/weather), paid and graded against a primary source that cannot be a reseller, ranked by deviation then price. Behind the most_accurate MCP tool",
         "key_fields": ["metric", "source", "reference", "unit", "sellers"]},
    ],
    "text": {"/llms.txt": "the whole site as one page", "/llms-full.txt": "every post and note in full"},
    "safety_note": ("reliability is a payment-safety score, not a delivery guarantee. A payTo that does not "
                    "match a listing is the one field to never ignore. Always read the payTo from the live "
                    "402 rather than from any listing, including ours."),
}, open(os.path.join(PUB, "api", "index.json"), "w"), indent=1)

# --- /openapi.json: the machine contract every tool and agent framework reads --
# agentcash discover, MCP directories and generic API tooling all look for this.
# One spec documenting the free data endpoints and the paid x402 lookup.
_PAYTO = "0xC533Bf5268A2F64aDDe58dcE380651f70Aa92D7A"
json.dump({
    "openapi": "3.1.0",
    "info": {
        "title": "What Agents Buy API",
        "version": "1.0.0",
        "description": ("Independent trust, discovery and delivery data for the APIs behind agentic "
                        "commerce. Free JSON endpoints plus one paid x402 lookup. No key required for "
                        "the free data."),
        "license": {"name": "CC BY 4.0"},
        "contact": {"url": SITE},
    },
    "servers": [{"url": SITE}],
    "paths": {
        "/x402/service": {
            "get": {
                "summary": "Everything known about one x402 seller, by host",
                "description": ("Grade earned by paying, live-vs-listed price honesty, payTo stability, "
                                "demand shape, Organic Demand Score and delivery verification for one host. "
                                "Paid $0.001 USDC on Base via x402; the free bulk data is at "
                                "/api/catalog.json."),
                "parameters": [{"name": "host", "in": "query", "required": True,
                                "schema": {"type": "string"},
                                "example": "blockrun.ai"}],
                "x-402": {"price": "0.001", "currency": "USDC", "network": "eip155:8453",
                          "payTo": _PAYTO},
                "responses": {"200": {"description": "Seller trust and delivery record"},
                              "402": {"description": "Payment required (x402 challenge)"}},
            }
        },
        "/api/catalog.json": {"get": {"summary": "Every payable endpoint, searchable, with price, chains, reliability and delivery",
                                      "responses": {"200": {"description": "The payable market"}}}},
        "/api/probe.json": {"get": {"summary": "The whole directory checked free: who answers, price and payTo honesty, reliability",
                                    "responses": {"200": {"description": "Probe results"}}}},
        "/api/delivery.json": {"get": {"summary": "Paid delivery checks: does the API return what it promised, with the real response shape",
                                       "responses": {"200": {"description": "Delivery verifications"}}}},
        "/api/leaderboard.json": {"get": {"summary": "Who actually gets paid, cross-chain (Base + Solana)",
                                          "responses": {"200": {"description": "Settlement leaderboard"}}}},
        "/api/ratings.json": {"get": {"summary": "Grades earned by paying each service",
                                      "responses": {"200": {"description": "Ratings"}}}},
        "/api/receipts.json": {"get": {"summary": "Dispute receipts: one self-verifying record per paid call, promise vs delivery with a three-level verifiable verdict",
                                       "responses": {"200": {"description": "The dispute ledger"}}}},
        "/api/preflight.json": {"get": {"summary": "Preflight: one CLEAR/HOLD/ABORT pre-payment verdict per seller, the check an agent runs before the 402",
                                        "responses": {"200": {"description": "Pre-payment verdicts by host"}}}},
        "/api/categories.json": {"get": {"summary": "The accuracy corpus: every objective-category seller graded against a primary source, ranked by deviation then price",
                                         "responses": {"200": {"description": "Per-category accuracy leaderboards"}}}},
    },
    "x-mcp": {"url": f"{SITE}/mcp", "transport": "streamable-http",
              "install": f"claude mcp add --transport http whatagentsbuy {SITE}/mcp"},
}, open(os.path.join(PUB, "openapi.json"), "w"), indent=1)

# --- .well-known/*: how an agent or a crawler discovers us without a human -----
# These are the files agent frameworks, MCP directories and x402 scanners look
# for by convention. Being present in them is how we get found where agents
# actually look, rather than waiting for a person to visit the site.
_wk = os.path.join(PUB, ".well-known")
os.makedirs(_wk, exist_ok=True)

# x402: our own paid endpoint, in the shape a scanner expects, so we appear in
# the registry we measure rather than only measuring it.
json.dump({
    "x402Version": 1,
    "resources": [{
        "resource": f"{SITE}/x402/service",
        "type": "http",
        "description": ("Independent trust and delivery data for any x402 API seller: grade earned by "
                        "paying, live-vs-listed price honesty, payTo stability, demand shape and delivery "
                        "verification. Query with ?host=<hostname>."),
        "accepts": [{
            "scheme": "exact", "network": "eip155:8453",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "1000", "payTo": _PAYTO,
            "extra": {"name": "USD Coin", "version": "2"},
        }],
        "provider": "What Agents Buy",
        "free_alternative": f"{SITE}/api/catalog.json",
    }],
}, open(os.path.join(_wk, "x402"), "w"), indent=1)

# MCP: a pointer to our server so directories and clients can auto-add it.
json.dump({
    "name": "whatagentsbuy",
    "description": ("Independent measurement of the APIs behind agentic commerce. Find a payable API for a "
                    "task, verify a seller before paying, and read grades earned by real payment."),
    "version": "1.0.0",
    "transport": "streamable-http",
    "url": f"{SITE}/mcp",
    "auth": "none",
    "install": f"claude mcp add --transport http whatagentsbuy {SITE}/mcp",
    "tools": ["preflight", "find_api", "most_accurate", "get_service", "search_services",
              "top_services", "list_traps", "market_summary"],
    "homepage": SITE,
    "documentation": f"{SITE}/api/index.json",
}, open(os.path.join(_wk, "mcp.json"), "w"), indent=1)

# A general agent manifest tying it all together, one file to point anything at.
json.dump({
    "name": "What Agents Buy",
    "role": ("The trust and discovery layer for agentic commerce. Before an agent pays an x402 API, it can "
             "discover one for its task, verify the seller, and check whether the API actually delivers."),
    "mcp": f"{SITE}/mcp",
    "openapi": None,
    "data": {
        "manifest": f"{SITE}/api/index.json",
        "discover": f"{SITE}/api/catalog.json",
        "verify": f"{SITE}/api/probe.json",
        "delivery": f"{SITE}/api/delivery.json",
        "disputes": f"{SITE}/api/receipts.json",
        "preflight": f"{SITE}/api/preflight.json",
        "accuracy_corpus": f"{SITE}/api/categories.json",
        "who_gets_paid": f"{SITE}/api/leaderboard.json",
    },
    "text": {"llms": f"{SITE}/llms.txt", "llms_full": f"{SITE}/llms-full.txt"},
    "license": "CC BY 4.0, attribute What Agents Buy",
}, open(os.path.join(_wk, "agent.json"), "w"), indent=1)

# --- /api/agent-usage.json: the audience Vercel Web Analytics cannot see --------
# Agents hit /mcp and the JSON APIs as raw HTTP with no browser JS, so they never
# appear in the pageview dashboard. A local KeepAlive job captures those hits from
# Vercel's runtime logs into dated JSONL; this aggregates them so the agent-facing
# usage is visible on the site, which is the only place it can be.
_hits = _AGENT_HITS   # loaded once, early, so the homepage counter shares this source
if _hits:
    _by_tool = collections.Counter(h.get("tool") for h in _hits if h.get("tool"))
    _by_subject = collections.Counter(h.get("subject") for h in _hits if h.get("subject"))
    _by_day = collections.Counter(
        time.strftime("%Y-%m-%d", time.gmtime((h.get("ts") or 0) / 1000)) for h in _hits if h.get("ts"))
    # Which software actually calls the MCP (from the client family in each log
    # line). Be honest: curl/wget/unknown and our own probe UAs are tooling and our
    # own testing, not agents, so report them but count "agent" traffic separately
    # rather than inflate the number with our own hits. Older hits predate client
    # logging and have no client; they fall into unknown.
    _TOOLING = {"curl", "wget", "unknown", "python-urllib", "python-requests",
                "whatagentsbuy-x402", "touchstone-probe", "vercel", "go-http-client", "-", None}
    _by_client = collections.Counter((h.get("client") or "unknown") for h in _hits)
    _agent_tool_hits = [h for h in _hits if h.get("tool") and (h.get("client") or "unknown") not in _TOOLING]
    _agent_clients = collections.Counter(h["client"] for h in _agent_tool_hits)
    _agent_days = collections.Counter(
        time.strftime("%Y-%m-%d", time.gmtime((h.get("ts") or 0) / 1000)) for h in _agent_tool_hits if h.get("ts"))
    globals()["_AGENT_USAGE"] = {"tool_hits": len(_agent_tool_hits),
                                 "clients": dict(_agent_clients.most_common()),
                                 "by_day": dict(sorted(_agent_days.items()))}
    json.dump({
        "generated": NOW_ISO, "source": SITE, "api_version": 1,
        "what": ("Agent usage of this site's tools and APIs, captured from server logs. These hits do not "
                 "appear in browser analytics because agents run no JavaScript. Hosts queried are the "
                 "services agents asked us to check before paying."),
        "total_hits": len(_hits),
        "by_tool": dict(_by_tool.most_common()),
        "by_day": dict(sorted(_by_day.items())),
        "by_client": dict(_by_client.most_common()),
        "distinct_clients": len(_by_client),
        "agent_traffic": {
            "note": ("Excludes obvious tooling/self (curl, wget, our own probe UAs, and pre-attribution hits "
                     "with no client). This is the credible 'which agents actually call this' number."),
            "tool_hits": len(_agent_tool_hits),
            "distinct_clients": len(_agent_clients),
            "clients": dict(_agent_clients.most_common()),
            "by_day": dict(sorted(_agent_days.items())),
        },
        "top_hosts_queried": dict(_by_subject.most_common(25)),
    }, open(os.path.join(PUB, "api", "agent-usage.json"), "w"), indent=1)

# llms-full.txt: everything in one document, for a model that would rather read
# once than crawl thirty pages.
_lf = ["# What Agents Buy, in full", "",
       "> Independent reviews of the APIs behind agentic commerce. Every grade below was earned by paying the "
       "service with a real wallet, or by calling it where it is free. No service pays to appear.", "",
       f"Source: {SITE} · Written by {HANDLE} ({CONTACT}) · JSON: {SITE}/api/ratings.json", ""]
if LB:
    _w = LB["windows"]["1d"]
    _lf += [f'## The market, 24h to {LB["last_day"]}', "",
            f'${_w["total_usdc"]:,.0f} USDC settled across {_w["total_settlements"]:,} payments on Base and Solana, '
            f'swept from chain logs. {len(O):,} services tracked.', ""]
    for i, r in enumerate(_w["rows"][:15], 1):
        _lf.append(f'{i}. {r["service"]} ({r["host"]}) — ${r["usdc"]:,.0f} on {r["settlements"]:,} payments '
                   f'from {r["payers"]} wallets')
    _lf.append("")
_lf += ["## Ratings, best to worst", ""]
for _p in pays_sorted:
    _v = _p.get("verdict") or {}
    _lf += [f'### {(_p.get("api") or {}).get("name")} — {_p["grade"]} on {_p.get("graded","")}', "",
            f'{_p.get("title","")}', "",
            (f'Quoted {_v.get("quoted")}, charged {_v.get("charged")}, '
             f'goods {"delivered" if _v.get("delivered") else "not delivered"}. ' if _v.get("call") else
             "Called without payment. ") + f'{SITE}/p/{_p["id"]}', ""]
    for _b in (_p.get("bullets") or []):
        _lf.append(f'- {_b}')
    _lf.append("")
if _studies:
    _lf += ["## Studies: findings from measuring the market, not from buying one endpoint", ""]
    for _p in _studies:
        _subj = (_p.get("api") or {}).get("name") or ""
        _lf += [f'### {_p.get("title","")}', "",
                (f'Subject: {_subj}. ' if _subj else "") + f'{SITE}/p/{_p["id"]}', ""]
        if _p.get("lede"):
            _lf += [_p["lede"], ""]
        for _b in (_p.get("bullets") or []):
            _lf.append(f'- {_b}')
        _lf.append("")

_lf += ["## Field notes: what goes wrong when software pays an API", ""]
for n in NOTES:
    _lf += [f'### {n["trap"]}', "", n["bite"], "", f'**What to do:** {n["do"]}', "",
            n["evidence"], "", f'{SITE}/notes/{n["id"]}', ""]
_lf += ["## Reuse", "",
        "Quote freely with attribution to What Agents Buy and the as-of date shown. Figures change daily; "
        f"re-read rather than cache. Structured versions: {SITE}/api/ratings.json, "
        f"{SITE}/api/leaderboard.json, {SITE}/api/field-notes.json", ""]
open(os.path.join(PUB, "llms-full.txt"), "w").write("\n".join(_lf))

# --- keep the project's own briefing current ------------------------------------
# CLAUDE.md went five days stale once and started claiming the wrong live URL, the
# wrong post counts and a byline that had been removed. Those are all facts this
# script already holds, so it owns them now. Everything outside the AUTO markers is
# hand-written judgement and is never touched.
def refresh_claude_md():
    path = os.path.join(HERE, "CLAUDE.md")
    if not os.path.exists(path):
        return
    doc = open(path).read()
    _graded = [x for x in FEED if x.get("grade")]
    _svcs = {(x.get("api") or {}).get("name") for x in _graded}
    _kinds = collections.Counter(x.get("kind") for x in FEED if x.get("kind"))
    # Exclude the Solana sweep files (settlements_solana_<date>.json): their stem
    # is "solana_<date>", which otherwise sorts last and prints as the tape end
    # date ("to solana_2026-08-13") while also inflating the day count. The tape
    # range is the Base daily sweep; Solana is folded into revenue, not the tape.
    _tape = sorted(f[len("settlements_"):-len(".json")]
                   for f in os.listdir(os.path.join(HERE, "data", "history"))
                   if f.startswith("settlements_") and "solana" not in f) if os.path.isdir(os.path.join(HERE, "data", "history")) else []
    state = (
        f"**LIVE: {SITE}** (Vercel, apex domain, www 308s to it). Rebuilt and deployed daily.\n"
        f"Repo: github.com/neilkpatel/touchstone (private). Authored by **{AUTHOR}** (real name; the "
        f"@crowdturtle pseudonym is retired from the page), contact via LinkedIn, no email.\n\n"
        f"As of **{TODAY}**: {len(_graded)} ratings across {len(_svcs)} services, {len(NOTES)} field notes, "
        f"{_kinds.get('study', 0)} Key Insight studies, {_kinds.get('news', 0)} news "
        f"post{'s' if _kinds.get('news', 0) != 1 else ''}, "
        f"{len(urls)} indexable URLs.\n"
        + (f"Settlement tape: {len(_tape)} days, {_tape[0]} to {_tape[-1]}.\n" if _tape else "")
        + (f"Last 24h measured: ${LB['windows']['1d']['total_usdc']:,.0f} across "
           f"{LB['windows']['1d']['total_settlements']:,} payments.\n" if LB else "")
    )
    nav = "\n".join(f"{u:<17s} {lbl}" for u, lbl in NAV_ITEMS)
    mp = ("Every nav destination is a real URL with its own title, description and self-canonical.\n\n"
          "```\n" + nav + "\n"
          "/p/<id>           post permalinks, Review + BreadcrumbList schema\n"
          "/notes/<id>       field notes, TechArticle schema\n"
          "/s/<host>         one page per graded service\n"
          "/api/*.json       ratings, leaderboard, field-notes\n"
          "/llms.txt         the site in one page   /llms-full.txt  everything\n```\n")
    for tag, body in (("STATE", state), ("MAP", mp)):
        a, b = f"<!-- AUTO:{tag} -->", f"<!-- /AUTO:{tag} -->"
        if a in doc and b in doc:
            doc = doc[:doc.index(a) + len(a)] + "\n" + body + doc[doc.index(b):]
    open(path, "w").write(doc)


refresh_claude_md()


def audit_coverage():
    """Every post, note and service must appear on every surface that should carry
    it. Fails the build rather than warning, because a post nobody can find is
    worse than no post, and a warning in a log nobody reads is how this happened:
    llms.txt and llms-full.txt silently omitted every study for a week, so the
    strongest research on the site was invisible to anything reading those files
    instead of crawling.

    The daily job never adds content, only refreshes data, so an assertion here
    can only fire while a human is adding a post. That is exactly when to catch it.
    """
    read = lambda p: open(os.path.join(PUB, p)).read()
    sm, ll, lf, rss, home = (read("sitemap.xml"), read("llms.txt"),
                             read("llms-full.txt"), read("feed.xml"), read("index.html"))
    gaps = []

    for p in FEED:
        pid, where = p["id"], f"/p/{p['id']}"
        if not os.path.exists(os.path.join(PUB, "p", pid, "index.html")):
            gaps.append(f"post {pid}: no permalink page")
        surfaces = [("sitemap.xml", sm), ("llms.txt", ll),
                    ("llms-full.txt", lf), ("the front page", home)]
        if p in FEED[:RSS_ITEMS]:
            surfaces.append(("feed.xml", rss))
        for surface, blob in surfaces:
            if where not in blob:
                gaps.append(f"post {pid}: missing from {surface}")

    for n in NOTES:
        nid, where = n["id"], f"/notes/{n['id']}"
        if not os.path.exists(os.path.join(PUB, "notes", nid, "index.html")):
            gaps.append(f"note {nid}: no page")
        for surface, blob in (("sitemap.xml", sm), ("llms.txt", ll), ("llms-full.txt", lf)):
            if where not in blob:
                gaps.append(f"note {nid}: missing from {surface}")

    for host in sorted({(p.get("api") or {}).get("name", "") for p in FEED
                        if p.get("grade") and is_host((p.get("api") or {}).get("name", ""))}):
        if f"/s/{host}" not in sm:
            gaps.append(f"service {host}: missing from sitemap.xml")

    if gaps:
        raise SystemExit("BUILD FAILED, content would ship invisible:\n  "
                         + "\n  ".join(gaps))
    print(f"  coverage: {len(FEED)} posts and {len(NOTES)} notes present on every surface "
          "(pages, sitemap, llms.txt, llms-full.txt, RSS, front page)")


audit_coverage()

print(f"built: {len(FEED)} posts, {len(NOTES)} field notes, {len(urls)} URLs, llms.txt + api/ + CLAUDE.md")
