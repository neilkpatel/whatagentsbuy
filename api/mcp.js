// Remote MCP server for What Agents Buy.
//
//   claude mcp add --transport http whatagentsbuy https://whatagentsbuy.com/mcp
//
// Streamable HTTP, stateless: every POST is a self-contained JSON-RPC message
// and we hold no session. That suits a serverless function, which may not be the
// same instance twice, and it means there is no session to expire or leak.
//
// Implemented directly rather than through the SDK. The stateless subset is
// small, and a dependency-free function cold-starts faster, which matters when
// an agent is waiting on a tool call.
//
// No auth and no payment. The whole point of this site is that the measurements
// are free to check; putting a paywall in front of the agent-facing surface
// would be the opposite of the argument it makes.

import { siteJson } from "./_x402.js";

// Read the body ourselves. Vercel's platform parser rejects malformed JSON with
// an opaque empty 400 before our handler runs, so a client that sends slightly
// bad JSON never sees a proper JSON-RPC parse error. With the parser off we read
// the raw stream, cap its size, and emit our own -32700.
export const config = { api: { bodyParser: false } };

const SERVER = { name: "whatagentsbuy", version: "1.0.0" };
const FALLBACK_PROTOCOL = "2025-06-18";

// JSON-RPC is tiny; even a large legal batch is a few KB. Anything past this is a
// mistake or an attempt to make us buffer, so we refuse it before allocating.
const MAX_BODY_BYTES = 1_000_000;
const MAX_BATCH = 50;

// Read and size-cap the raw request body. Honours an already-parsed req.body if a
// runtime provides one, so this stays correct even if the parser is re-enabled.
async function readRawBody(req, maxBytes = MAX_BODY_BYTES) {
  if (req.body !== undefined && req.body !== null) return req.body;
  const chunks = [];
  let n = 0;
  for await (const chunk of req) {
    n += chunk.length;
    if (n > maxBytes) { const e = new Error("request body too large"); e.code = "TOO_LARGE"; throw e; }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

const TOOLS = [
  {
    name: "get_service",
    title: "Look up one API seller",
    description:
      "Everything known about one x402 seller by host: every grade earned by actually paying it, " +
      "what was quoted versus what was charged, whether goods arrived, plus its settlement volume " +
      "and Organic Demand Score. Use before letting an agent pay an unfamiliar API.",
    inputSchema: {
      type: "object",
      properties: {
        host: { type: "string", description: "Hostname, e.g. blockrun.ai or api.bitrefill.com" },
      },
      required: ["host"],
    },
  },
  {
    name: "search_services",
    title: "Search graded sellers",
    description:
      "Search the graded ledger by hostname, behaviour graded, or words in the finding's title. " +
      "Returns matching services with their grades.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "Free text, e.g. 'email', 'price honesty', 'trust'" },
        min_grade: {
          type: "string",
          description: "Optional floor, e.g. 'B'. Returns only grades at or above it.",
        },
      },
      required: ["query"],
    },
  },
  {
    name: "top_services",
    title: "Rank sellers",
    description:
      "Rank sellers by money received or by Organic Demand Score. Revenue answers who is busy; " +
      "organic demand answers whose money comes from many independent wallets rather than one. " +
      "They disagree often, and the disagreement is usually the interesting part.",
    inputSchema: {
      type: "object",
      properties: {
        by: { type: "string", enum: ["revenue", "organic_demand"], default: "revenue" },
        limit: { type: "integer", default: 10, minimum: 1, maximum: 50 },
      },
    },
  },
  {
    name: "list_traps",
    title: "Known ways paying an API goes wrong",
    description:
      "The field notes: each is a specific way an agent paying an API loses money or is misled, " +
      "what it cost to learn, and what to do instead. Read before writing payment code.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "Optional filter, e.g. 'price', 'delivery'" },
      },
    },
  },
  {
    name: "find_api",
    title: "Find a payable API for a task",
    description:
      "Search the whole payable market for an API that does what you need, and get a ranked, pre-vetted " +
      "shortlist you can actually pay. Covers every reachable endpoint, not just the ones we have bought " +
      "from. Ranks proven-accurate sellers first, then by a payment-safety reliability score, then price. Use " +
      "this to choose who to pay, then call preflight on your pick and read the payTo from the live 402 before signing.",
    inputSchema: {
      type: "object",
      properties: {
        task: { type: "string", description: "What you need, in words or keywords, e.g. 'validate an email address', 'BTC price', 'geocode an address'" },
        max_price_usdc: { type: "number", description: "Optional ceiling on the per-call price in USDC" },
        min_reliability: { type: "integer", description: "Optional floor on the reliability score 0-100 (default 70)" },
        limit: { type: "integer", description: "How many results (default 8, max 25)" },
      },
      required: ["task"],
    },
  },
  {
    name: "most_accurate",
    title: "Rank a category by graded accuracy",
    description:
      "For a category with an objective ground truth (crypto-price, stock-price, fx-rate, gas-price, " +
      "wallet-balance, weather), the sellers we PAID ranked by how close their returned value was to a " +
      "primary source that cannot be a reseller (exchange median, chain balanceOf, ECB rates, FMP quote). " +
      "The top rows are the cheapest accurate sellers. Call with no category to list the available ones.",
    inputSchema: {
      type: "object",
      properties: {
        category: { type: "string", description: "One of: crypto-price, stock-price, fx-rate, gas-price, wallet-balance, weather. Omit to list categories." },
        limit: { type: "integer", description: "How many results (default 10, max 50)" },
      },
    },
  },
  {
    name: "preflight",
    title: "Preflight: one verdict before your agent pays",
    description:
      "The single call to make before your agent pays an unfamiliar x402 API, the check it runs before the " +
      "402. Returns one gradable verdict for a URL or host: CLEAR (nothing alarming), HOLD (pay but verify), " +
      "ABORT (do not pay without checking the live 402), or UNRATED. Folds live price and payTo honesty " +
      "versus the listing, phantom paywalls, and delivery receipts from actually paying it into that one " +
      "light, with the reasons behind it. Pass detail:true to also get the full payment-safety read " +
      "(live 402 honesty, on-chain demand, every grade earned by paying it). Gate your payment on the light. " +
      "Whatever it says, still read the payTo and amount out of the live 402 and sign against those.",
    inputSchema: {
      type: "object",
      properties: {
        url: { type: "string", description: "Full URL or bare hostname, e.g. https://blockrun.ai/api/v1/exa/search or blockrun.ai" },
        detail: { type: "boolean", description: "Include the full payment-safety detail (live-402 honesty, demand shape, grades). Default false." },
      },
      required: ["url"],
    },
  },
  {
    name: "market_summary",
    title: "Size of the agent payment market",
    description:
      "What actually settled on Base in the last day: total USDC, number of payments, services " +
      "tracked, and the date of the tape. Measured from chain logs, not from registry counters, " +
      "which are not demand and can be bought.",
    inputSchema: { type: "object", properties: {} },
  },
];

export const GRADE_ORDER = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"];
export const rank = (g) => {
  const i = GRADE_ORDER.indexOf((g || "").toUpperCase());
  return i === -1 ? 99 : i;
};

// Normalize a URL or bare hostname to the host key our data is keyed by.
// Exported for unit tests: a bug here silently looks up the wrong seller.
export function normHost(raw) {
  let host = String(raw || "").trim().toLowerCase();
  try {
    if (/^https?:\/\//i.test(raw)) host = new URL(raw).hostname.toLowerCase();
  } catch { /* fall through: treat as a bare host */ }
  return host.replace(/^www\./, "").split("/")[0];
}

// The client FAMILY from the User-Agent (e.g. "cline", "claude-code", "python",
// "node", "curl"), lowercased and stripped to a short token. This is software
// identity, not a person or an IP, and it is the only per-call signal a stateless
// server has for "which agents actually call this". Deliberately coarse: the token
// before the first "/" or space, so no versions, no full header, nothing to track a
// caller by. Exported so a bug that logs the wrong thing is caught by a test.
export function clientToken(ua) {
  const first = String(ua || "").trim().split(/[\s/]/)[0].toLowerCase();
  return first.replace(/[^a-z0-9._-]/g, "").slice(0, 24) || "unknown";
}

// Shared: the detailed payment-safety read for one host (probe honesty,
// warnings, grades, demand). Used by preflight(detail:true) and the
// check_before_paying alias, so there is one source of truth.
async function paymentSafetyDetail(host, fetchJson) {
  const [probe, ratings, board] = await Promise.all([
    fetchJson("/api/probe.json"),
    fetchJson("/api/ratings.json"),
    fetchJson("/api/leaderboard.json"),
  ]);
  const p = (probe.origins || []).find((o) => (o.host || "").toLowerCase() === host) || null;
  const grades = (ratings.ratings || []).filter((r) => (r.service || "").toLowerCase() === host);
  const lb = (board.rows || []).find((r) => (r.host || "").toLowerCase() === host) || null;
  const warnings = [];
  if (!p) {
    warnings.push(
      "We have no live check for this host. It was either silent when we last swept the whole " +
      "directory, or it is not listed in it. Absence is not a bad sign by itself, but nothing here " +
      "has been verified.");
  } else {
    if (!p.payto_matches_listing)
      warnings.push(
        "CRITICAL: its live payment address does not match the address the directory lists for it. " +
        "Money would go somewhere the catalogue does not name. Read the payTo out of the live 402 " +
        "and confirm it before signing anything.");
    if (!p.price_matches_listing)
      warnings.push(
        `Its live quote disagrees with its listing: ${JSON.stringify(p.price_mismatches?.slice(0, 2) || [])}. ` +
        "Budget from the live 402, never from the listing.");
    if (p.phantom_paywall)
      warnings.push(
        "It quotes a price for a route that cannot exist, so a price alone is not evidence the " +
        "endpoint is real.");
    if (p.endpoints_checked && p.returns_402_unpaid < p.endpoints_checked)
      warnings.push(
        `${p.endpoints_checked - p.returns_402_unpaid} of ${p.endpoints_checked} checked endpoints ` +
        "did not return a 402 when called unpaid.");
  }
  if (lb?.sends_back_pct >= 90)
    warnings.push(
      `It sends ${lb.sends_back_pct}% of what it receives straight back out. That can be cost of ` +
      "goods or recycling; it is a flag, not a verdict.");
  if (lb?.demand === "one wallet")
    warnings.push("Almost all of its revenue comes from a single wallet, so its volume is not evidence of broad demand.");
  return {
    checked_live: Boolean(p),
    warnings,
    live_check: p && {
      answered: p.answered, endpoints_checked: p.endpoints_checked,
      returns_402_unpaid: p.returns_402_unpaid, price_matches_listing: p.price_matches_listing,
      payto_matches_listing: p.payto_matches_listing, phantom_paywall: p.phantom_paywall,
      median_ms: p.median_ms, free_tier: p.free_tier,
    },
    grades_we_earned_by_paying: grades.map((g) => ({
      grade: g.grade, behaviour: g.graded_on, verdict: g.verdict,
      quoted: g.quoted, charged: g.charged, delivered: g.delivered, url: g.url,
    })),
    demand: lb && {
      usdc_received_24h: lb.usdc_received, paying_wallets: lb.paying_wallets,
      demand_shape: lb.demand, organic_demand_score: lb.organic_demand_score, sends_back_pct: lb.sends_back_pct,
    },
    as_of: probe.generated,
  };
}


// fetchJson is injectable so tests can drive runTool with fixtures, no network.
export async function runTool(name, args, fetchJson = siteJson) {
  const a = args || {};
  if (name === "get_service") {
    const host = String(a.host || "").trim().toLowerCase();
    if (!host) throw new Error("host is required");
    const [ratings, board] = await Promise.all([
      fetchJson("/api/ratings.json"),
      fetchJson("/api/leaderboard.json"),
    ]);
    const grades = (ratings.ratings || []).filter(
      (r) => (r.service || "").toLowerCase() === host
    );
    const lb = (board.rows || []).find((r) => (r.host || "").toLowerCase() === host) || null;
    if (!grades.length && !lb) {
      return {
        found: false,
        host,
        note: "Never bought from and no settlement measured. Absence of a grade is not a bad grade.",
      };
    }
    return {
      found: true,
      host,
      grades: grades.map((g) => ({
        grade: g.grade,
        behaviour_graded: g.graded_on,
        verdict: g.verdict,
        quoted: g.quoted,
        charged: g.charged,
        delivered: g.delivered,
        date: g.date,
        finding: g.title,
        url: g.url,
      })),
      settlement: lb && {
        usdc_received_24h: lb.usdc_received,
        payments: lb.settlements,
        paying_wallets: lb.paying_wallets,
        repeat_buyers: lb.repeat_buyers,
        sends_back_pct: lb.sends_back_pct,
        demand_shape: lb.demand,
        organic_demand_score: lb.organic_demand_score,
        organic_demand_parts: lb.organic_demand_parts,
      },
      how_to_read:
        "A grade covers one named behaviour, not a vendor, so one service can hold two. " +
        "Organic Demand Score measures shape and not honesty: read it beside sends_back_pct, " +
        "because a wash trader would optimise exactly the quantities it rewards.",
    };
  }

  if (name === "search_services") {
    const q = String(a.query || "").trim().toLowerCase();
    const ratings = await fetchJson("/api/ratings.json");
    let rows = (ratings.ratings || []).filter((r) =>
      [r.service, r.graded_on, r.title, r.verdict].join(" ").toLowerCase().includes(q)
    );
    if (a.min_grade) rows = rows.filter((r) => rank(r.grade) <= rank(a.min_grade));
    rows.sort((x, y) => rank(x.grade) - rank(y.grade));
    return {
      query: a.query,
      count: rows.length,
      results: rows.map((r) => ({
        host: r.service, grade: r.grade, behaviour_graded: r.graded_on,
        verdict: r.verdict, quoted: r.quoted, charged: r.charged, url: r.url,
      })),
    };
  }

  if (name === "top_services") {
    const by = a.by === "organic_demand" ? "organic_demand" : "revenue";
    const limit = Math.min(Math.max(parseInt(a.limit || 10, 10), 1), 50);
    const board = await fetchJson("/api/leaderboard.json");
    let rows = (board.rows || []).slice();
    if (by === "organic_demand") {
      rows = rows.filter((r) => r.organic_demand_score != null);
      rows.sort((x, y) => y.organic_demand_score - x.organic_demand_score);
    } else {
      rows.sort((x, y) => (y.usdc_received || 0) - (x.usdc_received || 0));
    }
    return {
      ranked_by: by,
      as_of: board.as_of,
      window: board.window,
      caveat:
        by === "revenue"
          ? "Money received is turnover, not profit or quality. sends_back_pct at or above 90 means most of it left again."
          : "High organic demand is not a clean bill of health. Read it beside sends_back_pct.",
      results: rows.slice(0, limit).map((r) => ({
        host: r.host, usdc_received: r.usdc_received, payments: r.settlements,
        paying_wallets: r.paying_wallets, sends_back_pct: r.sends_back_pct,
        demand_shape: r.demand, organic_demand_score: r.organic_demand_score,
        grades: r.grades,
      })),
    };
  }

  if (name === "list_traps") {
    const notes = await fetchJson("/api/field-notes.json");
    const q = String(a.query || "").trim().toLowerCase();
    let rows = notes.notes || notes.field_notes || [];
    if (q) rows = rows.filter((n) => JSON.stringify(n).toLowerCase().includes(q));
    return { count: rows.length, traps: rows };
  }

  if (name === "find_api") {
    const task = String(a.task || "").trim().toLowerCase();
    if (!task) throw new Error("task is required");
    const maxPrice = typeof a.max_price_usdc === "number" ? a.max_price_usdc : Infinity;
    const minRel = Number.isInteger(a.min_reliability) ? a.min_reliability : 70;
    const limit = Math.min(Math.max(parseInt(a.limit || 8, 10), 1), 25);

    const [cat, corpus] = await Promise.all([
      fetchJson("/api/catalog.json"),
      fetchJson("/api/categories.json"),
    ]);
    // host -> its accuracy grade from the corpus. A seller we PAID and found
    // returns the correct number (vs a primary source) is worth far more than one
    // that merely responds, so these jump to the top of any matching search.
    const accurate = {};
    for (const [catName, c] of Object.entries(corpus.categories || {})) {
      for (const s of c.sellers || []) {
        if (s.verdict === "accurate" && !(s.host in accurate)) {
          accurate[s.host] = { category: catName, off_by: s.deviation, unit: c.unit, vs: c.metric };
        }
      }
    }
    // Score by task-word matches, boost accuracy-graded sellers, and dedup hosts
    // (keep each host's best-scoring endpoint), so the same seller never repeats.
    const words = task.split(/\s+/).filter((w) => w.length > 2);
    const byHost = {};
    for (const e of cat.endpoints || []) {
      if ((e.price_usdc ?? Infinity) > maxPrice) continue;
      if ((e.reliability ?? 0) < minRel) continue;
      const desc = (e.description || "").toLowerCase();
      let rel = 0;
      for (const w of words) if (desc.includes(w)) rel++;
      if (desc.includes(task)) rel += words.length;
      if (rel === 0) continue;
      const acc = accurate[e.host];
      const score = rel + (acc ? 1000 : 0);          // proven-accurate ranks first
      const prev = byHost[e.host];
      if (!prev || score > prev.score) byHost[e.host] = { e, score, acc };
    }
    const scored = Object.values(byHost).sort((x, y) =>
      y.score - x.score ||
      (y.e.reliability || 0) - (x.e.reliability || 0) ||
      (x.e.price_usdc ?? 9e9) - (y.e.price_usdc ?? 9e9));

    const results = scored.slice(0, limit).map(({ e, acc }) => ({
      host: e.host, url: e.url, description: e.description,
      price_usdc: e.price_usdc, method: e.method, chains: e.chains,
      reliability: e.reliability,
      graded_accurate: acc ? { off_by: acc.off_by, unit: acc.unit, vs: acc.vs } : null,
      price_honest: e.price_honest, payto_honest: e.payto_honest,
      grades_we_earned_by_paying: e.grades,
    }));
    return {
      task: a.task,
      searched: (cat.endpoints || []).length,
      filters: { max_price_usdc: maxPrice === Infinity ? null : maxPrice, min_reliability: minRel },
      count: results.length,
      results,
      how_to_use:
        "Ranked by whether we PAID the seller and found it returns the correct value (graded_accurate, " +
        "against a primary source), then by reliability (a free payment-safety score), then price. Prefer a " +
        "graded_accurate seller. Then call preflight(host) on your pick and read the amount and payTo out of " +
        "the live 402. For a whole category ranked by accuracy, use most_accurate.",
      as_of: cat.generated,
    };
  }

  if (name === "most_accurate") {
    const catName = String(a.category || "").trim().toLowerCase();
    const limit = Math.min(Math.max(parseInt(a.limit || 10, 10), 1), 50);
    const corpus = await fetchJson("/api/categories.json");
    const cats = corpus.categories || {};
    if (!catName) {
      return { note: "Pass one of these as `category`.", available_categories: Object.keys(cats) };
    }
    const c = cats[catName];
    if (!c) {
      return { category: catName, found: false, available_categories: Object.keys(cats) };
    }
    return {
      category: catName,
      metric: c.metric,
      graded_against: c.source,
      reference: c.reference,
      count: (c.sellers || []).length,
      ranked: (c.sellers || []).slice(0, limit).map((s) => ({
        host: s.host, returned: s.value, off_by: s.deviation, unit: c.unit,
        price_usdc: s.price_usdc, verdict: s.verdict,
      })),
      how_to_use:
        "Every seller here we PAID, and graded its returned value against a primary source it cannot get " +
        "through these vendors. Ranked by accuracy (deviation), then price, so the top rows are the cheapest " +
        "accurate sellers. Still call preflight(host) and read the live 402 before paying.",
      as_of: corpus.generated,
    };
  }

  if (name === "preflight") {
    const host = normHost(a.url);
    if (!host) throw new Error("url is required");

    const pf = await fetchJson("/api/preflight.json");
    const v = (pf.sellers || {})[host] || null;
    const LABEL = { green: "CLEAR", yellow: "HOLD", red: "ABORT", gray: "UNRATED" };
    const ALWAYS =
      "Whatever this says, read the payTo and the amount out of the live 402 on every call and sign against " +
      "those, never a listing.";
    const detail = a.detail ? await paymentSafetyDetail(host, fetchJson) : undefined;
    if (!v) {
      return {
        host, light: "gray", verdict: "UNRATED",
        gate: "No data on this host yet. It was silent when we last swept the directory, or is not " +
          "listed. Absence is not a bad sign, but nothing here has been verified: read the live 402.",
        reasons: [], seller_page: `https://whatagentsbuy.com/s/${host}`,
        always: ALWAYS, as_of: pf.generated, ...(detail ? { detail_checks: detail } : {}),
      };
    }
    // The gate says what was ACTUALLY checked. A green host the free probe never
    // reached is not "payment-safety verified"; it is clean on what we measured.
    let gate;
    if (v.light === "green") {
      gate = v.checked_live
        ? "Nothing alarming in the payment-safety checks. Safe to pay; still read the live 402."
        : ((v.accurate || 0) + (v.delivered || 0) > 0
            ? "Clean on what we measured (delivery/accuracy), but the payment-safety probe has not reached " +
              "this host. Read the payTo and amount out of the live 402."
            : "Nothing alarming found, but little has been checked here. Read the live 402.");
    } else {
      gate = {
        yellow: "Payable, but resolve the reasons below before paying.",
        red: "Do not pay without checking the live 402 first.",
        gray: "Unrated.",
      }[v.light];
    }
    return {
      host,
      verdict: LABEL[v.light] || String(v.light).toUpperCase(),
      light: v.light,
      score: v.score,
      gate,
      reasons: v.reasons,
      evidence: {
        receipts: v.receipts, disputes: v.disputes,
        delivered: v.delivered, accurate: v.accurate,
        checked_live: v.checked_live,
      },
      ...(detail ? { detail_checks: detail } : {}),
      seller_page: `https://whatagentsbuy.com/s/${host}`,
      always: ALWAYS,
      as_of: pf.generated,
    };
  }

  if (name === "check_before_paying") {
    // Back-compat alias, no longer advertised: preflight(url, detail:true) is the
    // one call, returning the verdict plus these detailed payment-safety checks.
    const host = normHost(a.url);
    if (!host) throw new Error("url is required");
    const d = await paymentSafetyDetail(host, fetchJson);
    return {
      host, ...d,
      how_to_use: "For the one-line verdict plus these checks, call preflight(url) with detail:true. " +
        "Always read the payTo and amount out of the live 402 and sign against those.",
    };
  }

  if (name === "market_summary") {
    const board = await fetchJson("/api/leaderboard.json");
    return {
      as_of: board.as_of,
      window: board.window,
      total_usdc: board.total_usdc,
      total_payments: board.total_settlements,
      tape_from: board.tape_from,
      tape_to: board.tape_to,
      method: board.method,
      caveat: board.caveat,
    };
  }

  throw new Error(`unknown tool: ${name}`);
}

function rpc(id, result) {
  return { jsonrpc: "2.0", id, result };
}
function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

export default async function handler(req, res) {
  res.setHeader("access-control-allow-origin", "*");
  res.setHeader("access-control-allow-methods", "POST, GET, OPTIONS");
  res.setHeader("access-control-allow-headers", "content-type, mcp-protocol-version, accept");
  if (req.method === "OPTIONS") return res.status(204).end();

  // Some clients probe with GET expecting an SSE stream. We are stateless and
  // have no server-initiated messages, so say so rather than hanging open.
  if (req.method === "GET") {
    return res.status(405).json({
      error: "This MCP server is stateless. POST JSON-RPC messages to this URL.",
      server: SERVER,
      add: "claude mcp add --transport http whatagentsbuy https://whatagentsbuy.com/mcp",
      tools: TOOLS.map((t) => t.name),
    });
  }
  if (req.method !== "POST") {
    res.setHeader("allow", "POST, GET, OPTIONS");
    return res.status(405).json({ error: "method not allowed" });
  }

  let raw;
  try {
    raw = await readRawBody(req);
  } catch (e) {
    if (e.code === "TOO_LARGE")
      return res.status(413).json(rpcError(null, -32600, `request too large: max ${MAX_BODY_BYTES} bytes`));
    return res.status(400).json(rpcError(null, -32700, "could not read request body"));
  }
  let body = raw;
  if (typeof body === "string") {
    if (!body.trim()) return res.status(400).json(rpcError(null, -32700, "empty request body"));
    try { body = JSON.parse(body); } catch { return res.status(400).json(rpcError(null, -32700, "parse error")); }
  }
  const batch = Array.isArray(body) ? body : [body];
  if (batch.length > MAX_BATCH)
    return res.status(400).json(rpcError(null, -32600, `batch too large: ${batch.length} messages, max ${MAX_BATCH}`));
  const out = [];
  const t0 = Date.now();

  // One line per call so usage is observable at all. Vercel's runtime logs carry
  // explicit output only, so without this an MCP server looks completely idle
  // however much it is being used.
  //
  // Tool name and the queried host go in, because those are public hostnames and
  // knowing which services get looked up is the whole point. Plus the client FAMILY
  // (from the User-Agent, coarse: "cline"/"node"/"curl"), because "which agents use
  // this" is unanswerable otherwise. Raw arguments, IPs and full headers stay out:
  // this is a free unauthenticated endpoint with no reason to accumulate who called it.
  const log = (msg) => console.log(`[mcp] ${msg}`);
  const client = clientToken(req.headers?.["user-agent"]);

  for (const msg of batch) {
    const { id, method, params } = msg || {};
    const isNotification = id === undefined || id === null;
    try {
      if (method === "initialize") {
        const c = params?.clientInfo || {};
        log(`initialize client=${c.name || "?"}/${c.version || "?"} protocol=${params?.protocolVersion || "?"}`);
        out.push(rpc(id, {
          // Echo the client's version when it names one; guessing a version the
          // client does not speak is how these handshakes fail.
          protocolVersion: params?.protocolVersion || FALLBACK_PROTOCOL,
          capabilities: { tools: { listChanged: false } },
          serverInfo: SERVER,
          instructions:
            "Independent measurements of x402 API sellers. Grades come from actually paying " +
            "each service and recording what happened; nothing is sponsored. Call preflight(url) " +
            "before letting an agent pay an unfamiliar API: it returns one CLEAR / HOLD / ABORT " +
            "verdict to gate on (pass detail:true for the full payment-safety read). Use find_api " +
            "to discover a payable API for a task, most_accurate to rank a category by how close its " +
            "sellers' returned values are to a primary source, and list_traps before writing payment " +
            "code. Whatever preflight says, always read the payTo and amount from the live 402 and " +
            "sign against those.",
        }));
      } else if (method === "notifications/initialized" || method === "notifications/cancelled") {
        // nothing to do, and nothing to reply to
      } else if (method === "ping") {
        out.push(rpc(id, {}));
      } else if (method === "tools/list") {
        log("tools/list");
        out.push(rpc(id, { tools: TOOLS }));
      } else if (method === "resources/list") {
        out.push(rpc(id, { resources: [] }));
      } else if (method === "prompts/list") {
        out.push(rpc(id, { prompts: [] }));
      } else if (method === "tools/call") {
        const arg = params?.arguments || {};
        const subject = arg.host || arg.url || arg.query || arg.task || arg.category || arg.by || "";
        const data = await runTool(params?.name, params?.arguments);
        log(`tool=${params?.name}${subject ? ` subject=${subject}` : ""} client=${client} ms=${Date.now() - t0}`);
        out.push(rpc(id, {
          content: [{ type: "text", text: JSON.stringify(data, null, 1) }],
          structuredContent: data,
          isError: false,
        }));
      } else if (!isNotification) {
        out.push(rpcError(id, -32601, `method not found: ${method}`));
      }
    } catch (e) {
      if (method === "tools/call") {
        log(`tool=${params?.name} client=${client} FAILED ${e.message}`);
        out.push(rpc(id, {
          content: [{ type: "text", text: `Error: ${e.message}` }],
          isError: true,
        }));
      } else if (!isNotification) {
        out.push(rpcError(id, -32603, e.message));
      }
    }
  }

  if (!out.length) return res.status(202).end();
  res.setHeader("content-type", "application/json");
  return res.status(200).json(Array.isArray(body) ? out : out[0]);
}
