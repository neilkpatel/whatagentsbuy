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
import { createHash } from "crypto";

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
    name: "find_api",
    title: "Find a payable API for a task",
    description:
      "USE WHEN you need an API that does something and want payable options. Searches the whole x402 market " +
      "for what you describe and returns a ranked, pre-vetted shortlist, proven-accurate sellers first, then " +
      "payment-safety, then price. Covers every reachable endpoint, not just ones we have bought from. Next " +
      "step: check_before_paying on your pick, then read the payTo from the live 402 and sign against it.",
    inputSchema: {
      type: "object",
      properties: {
        task: { type: "string", description: "What you need, in words or keywords, e.g. 'validate an email address', 'BTC price', 'geocode an address'" },
        max_price_usdc: { type: "number", description: "Optional ceiling on the per-call price in USDC" },
        min_reliability: { type: "integer", description: "Optional floor on the reliability score 0-100 (default 0 = no floor; the whole reachable market)" },
        limit: { type: "integer", description: "How many results (default 8, max 25)" },
      },
      required: ["task"],
    },
  },
  {
    name: "check_before_paying",
    title: "The one check before your agent pays",
    description:
      "USE WHEN your agent is about to pay an x402 API and you want to know if it is safe and worth it. " +
      "Returns one verdict for a URL or host, gate your payment on it: CLEAR (nothing alarming), HOLD (pay " +
      "but resolve the reasons), ABORT (do not pay without checking the live 402), UNRATED (no data). Also " +
      "returns a confidence tier (verified = a payment to this seller settled AND produced a gradeable result, checked = free probe measured) and its " +
      "payment history. Folds live price and payTo honesty, phantom paywalls, delivery receipts, and the " +
      "wash/real-demand read into that one light. Pass detail:true for the full read. Whatever it says, still " +
      "read the payTo and amount out of the live 402 and sign against those. (Formerly named preflight.)",
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
    name: "look_up_seller",
    title: "Everything known about one seller",
    description:
      "USE WHEN you have a specific seller host and want its full track record. Returns every grade earned by " +
      "actually paying it, what was quoted versus charged, whether goods arrived, its settlement volume, and " +
      "its Organic Demand Score with the demand-shape read (is the volume real independent demand, or one " +
      "wallet supplying almost all of it). The deep dossier on one seller; for a quick go/no-go before paying, " +
      "use check_before_paying instead. (Formerly named get_service.)",
    inputSchema: {
      type: "object",
      properties: {
        host: { type: "string", description: "Hostname, e.g. blockrun.ai or api.bitrefill.com" },
      },
      required: ["host"],
    },
  },
  {
    name: "rank_sellers",
    title: "The leaderboard: biggest, or most real",
    description:
      "USE WHEN you want a ranked list of sellers across the market. by='revenue' ranks by raw USDC received " +
      "(who is busy); by='real_demand' ranks by Organic Demand Score (whose money comes from many independent " +
      "wallets rather than one wallet supplying almost all of it). The two disagree often, and the disagreement is the point. " +
      "For accuracy ranking within an objective category, use rank_by_accuracy. (Formerly named top_services.)",
    inputSchema: {
      type: "object",
      properties: {
        by: { type: "string", enum: ["revenue", "real_demand"], default: "revenue" },
        limit: { type: "integer", default: 10, minimum: 1, maximum: 50 },
      },
    },
  },
  {
    name: "rank_by_accuracy",
    title: "Who returns the most accurate value in a category",
    description:
      "USE WHEN a category has an objective right answer (crypto-price, stock-price, fx-rate, gas-price, " +
      "wallet-balance, weather) and you want the sellers we PAID ranked by how close their returned value was " +
      "to a primary source that cannot be a reseller (exchange median, chain balanceOf, ECB rates, FMP quote). " +
      "Top rows are the cheapest accurate sellers. Call with no category to list the available ones. (Formerly " +
      "named most_accurate.)",
    inputSchema: {
      type: "object",
      properties: {
        category: { type: "string", description: "One of: crypto-price, stock-price, fx-rate, gas-price, wallet-balance, weather. Omit to list categories." },
        limit: { type: "integer", description: "How many results (default 10, max 50)" },
      },
    },
  },
  {
    name: "market_size",
    title: "How big is the x402 market",
    description:
      "USE WHEN asked how big x402 is, whether it is growing, or what settled recently. Returns both what " +
      "actually settled on Base over the last day (from chain logs, per tracked seller) and the live market " +
      "pulse (24h volume and payments with day-over-day change, a 7-day series, and stablecoin circulation). " +
      "All measured from chain, not registry counters, which are not demand and can be bought. (Merges the " +
      "former market_summary and market_pulse.)",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "known_payment_traps",
    title: "How agents lose money paying APIs",
    description:
      "USE WHEN writing payment code, or before trusting an unfamiliar seller. The field notes: each is a " +
      "specific way an agent paying an API loses money or is misled, what it cost to learn, and what to do " +
      "instead. (Formerly named list_traps.)",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "Optional filter, e.g. 'price', 'delivery'" },
      },
    },
  },
];

export const GRADE_ORDER = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"];
export const rank = (g) => {
  const i = GRADE_ORDER.indexOf((g || "").toUpperCase());
  return i === -1 ? 99 : i;
};

// Which accuracy category (if any) a find_api task is asking for. Accuracy is
// graded per category, so a crypto-price grade must not boost or badge an
// email-validation result. Returns null for tasks with no ground truth, so no
// accuracy claim is attached there. Keywords mirror the graded categories.
const ACCURACY_CATEGORY_RULES = [
  ["crypto-price", ["btc", "eth", "bitcoin", "ethereum", "crypto price", "token price", "coin price", "spot price", "price feed", "coingecko"]],
  ["stock-price", ["stock", "aapl", "ticker", "equity", "share price", "stock quote"]],
  ["fx-rate", ["fx rate", "exchange rate", "forex", "eur/usd", "currency rate", "fiat rate"]],
  ["gas-price", ["gas price", "gas fee", "gwei", "base fee", "basefee", "gas oracle"]],
  ["wallet-balance", ["wallet balance", "token balance", "erc20 balance", "erc-20 balance", "balanceof", "address balance"]],
  ["weather", ["weather", "forecast", "temperature", "open-meteo", "meteo"]],
];
export function accuracyCategoryOf(task) {
  const t = String(task || "").toLowerCase();
  for (const [cat, kws] of ACCURACY_CATEGORY_RULES) {
    if (kws.some((k) => t.includes(k))) return cat;
  }
  return null;
}

// Normalize a URL or bare hostname to the host key our data is keyed by.
// Exported for unit tests: a bug here silently looks up the wrong seller.
export function normHost(raw) {
  // Normalize BEFORE testing for a scheme. " https://evil.example" used to skip
  // URL parsing entirely (the scheme regex saw the leading space in the RAW
  // string while the trim only touched a copy), come back as host "https:", and
  // silently turn a known ABORT into UNRATED. Parse the trimmed string, and let
  // the URL parser strip credentials and ports so every equivalent spelling of
  // a seller resolves to the same host. Only www. is folded; distinct
  // subdomains stay distinct on purpose.
  const s = String(raw || "").trim();
  try {
    return new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(s) ? s : "https://" + s)
      .hostname.toLowerCase().replace(/^www\./, "");
  } catch {
    return s.toLowerCase().replace(/^www\./, "").split("/")[0].split(":")[0];
  }
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
    // Tri-state fields: false is a MEASURED mismatch, true a measured match,
    // null means the probe never captured a comparable pair. A null must not
    // read as a mismatch (that would be an accusation nobody measured) and
    // must not read as a match either.
    if (p.payto_matches_listing === false)
      warnings.push(
        "CRITICAL: its live payment address does not match the address the directory lists for it. " +
        "Money would go somewhere the catalogue does not name. Read the payTo out of the live 402 " +
        "and confirm it before signing anything.");
    if (p.price_matches_listing === false)
      warnings.push(
        `Its live quote disagrees with its listing: ${JSON.stringify(p.price_mismatches?.slice(0, 2) || [])}. ` +
        "Budget from the live 402, never from the listing.");
    if (p.payto_matches_listing == null || p.price_matches_listing == null)
      warnings.push(
        "Not every safety signal could be measured on the free probe" +
        (p.payto_matches_listing == null ? " (live payment address unverified vs the listing)" : "") +
        (p.price_matches_listing == null ? " (live price unverified vs the listing)" : "") +
        ". Unmeasured is unknown, not clean: read the payTo and amount from the live 402.");
    if (p.phantom_paywall)
      warnings.push(
        "It quotes a price for a route that cannot exist, so a price alone is not evidence the " +
        "endpoint is real.");
    if (p.endpoints_checked && p.returns_402_unpaid < p.endpoints_checked)
      warnings.push(
        `${p.endpoints_checked - p.returns_402_unpaid} of ${p.endpoints_checked} checked endpoints ` +
        "did not return a 402 when called unpaid.");
  }
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
      demand_shape: lb.demand, organic_demand_score: lb.organic_demand_score,
      top_buyer_share: lb.top_buyer_share, repeat_buyers: lb.repeat_buyers,
    },
    as_of: probe.generated,
  };
}


// fetchJson is injectable so tests can drive runTool with fixtures, no network.
// Advertised outcome-named tools -> the existing handler that does the work.
// The menu (TOOLS) shows the clean, outcome-named set; every old name still
// resolves here, so nothing that indexed the old names breaks. See the tool guide
// at /mcp for which tool does which job.
const TOOL_ALIASES = {
  check_before_paying: "preflight",   // "is it safe to pay this?"
  look_up_seller: "get_service",      // "what do you know about this seller?"
  rank_sellers: "top_services",       // "who's best, by revenue or real demand?"
  rank_by_accuracy: "most_accurate",  // "who returns the most accurate value in a category?"
  known_payment_traps: "list_traps",  // "how do agents lose money here?"
  is_volume_real: "is_organic",       // folded into look_up_seller; alias kept callable
};

export async function runTool(name, args, fetchJson = siteJson) {
  const a = args || {};
  name = TOOL_ALIASES[name] || name;

  // market_size merges the two old market tools into one call: what actually
  // settled (market_summary) plus the live market pulse (market_pulse).
  if (name === "market_size") {
    const [summary, pulse] = await Promise.all([
      runTool("market_summary", {}, fetchJson).catch(() => null),
      runTool("market_pulse", {}, fetchJson).catch(() => null),
    ]);
    return { settled_last_day: summary, live_pulse: pulse };
  }

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
        top_buyer_share: lb.top_buyer_share,
        demand_shape: lb.demand,
        organic_demand_score: lb.organic_demand_score,
        organic_demand_parts: lb.organic_demand_parts,
      },
      how_to_read:
        "A grade covers one named behaviour, not a vendor, so one service can hold two. " +
        "Organic Demand Score measures shape and not honesty: read it beside top_buyer_share, " +
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
    const by = (a.by === "organic_demand" || a.by === "real_demand") ? "organic_demand" : "revenue";
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
          ? "Money received is turnover, not profit or quality. Size is not evidence anyone wanted the product."
          : "High organic demand is not a clean bill of health. Read it beside top_buyer_share and the buyer count.",
      results: rows.slice(0, limit).map((r) => ({
        host: r.host, usdc_received: r.usdc_received, payments: r.settlements,
        paying_wallets: r.paying_wallets, top_buyer_share: r.top_buyer_share,
        demand_shape: r.demand, organic_demand_score: r.organic_demand_score,
        grades: r.grades,
      })),
    };
  }

  if (name === "is_organic") {
    const host = normHost(a.host || "");
    if (!host) throw new Error("host is required");
    const win = ["1d", "7d", "30d"].includes(a.window) ? a.window : "7d";
    const org = await fetchJson("/api/organic.json");
    const w = (org.windows || {})[win] || {};
    const row = (w.ranking || []).find((r) => normHost(r.host) === host);
    if (!row) {
      return {
        host, window: win, found: false,
        note:
          "This host has no measured on-chain settlement in this window, or it settles below the tracked " +
          "set, so there is nothing to rank. Absence here is not a grade.",
        market_wash_share_pct: w.wash_share_pct,
      };
    }
    let verdict;
    if (row.rank_gap <= -5)
      verdict = "inflated: ranks much higher by money than by real demand";
    else if (row.rank_gap >= 5)
      verdict = "underrated: real demand is stronger than its money rank suggests";
    else verdict = "consistent: its money rank and its demand rank roughly agree";
    return {
      host, window: win, found: true,
      verdict,
      inflated: row.inflated,
      organic_demand_score: row.organic_demand_score,
      score_parts: row.organic_demand_parts,
      confidence: row.confidence,
      money_rank: row.money_rank,
      demand_rank: row.demand_rank,
      usdc_received: row.usdc_received,
      paying_wallets: row.paying_wallets,
      top_buyer_share: row.top_buyer_share,
      market_wash_share_pct: w.wash_share_pct,
      caveat:
        "A low score is a flag, not a verdict: one large genuine customer looks identical to a wallet " +
        "paying itself, and nothing on chain separates them. Read the score beside top_buyer_share and the buyer count.",
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
    // Default to NO floor: find_api advertises "covers every reachable endpoint",
    // so it must not silently hide the long tail below an arbitrary 70. Callers
    // that want only high-reliability sellers pass min_reliability themselves.
    const minRel = Number.isInteger(a.min_reliability) ? a.min_reliability : 0;
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
    // Accuracy is graded PER CATEGORY (crypto-price, stock-price, ...), so a
    // seller proven accurate at BTC/USD tells you nothing about its email
    // validator. Only honor the grade when THIS task is the same category it was
    // graded in; otherwise a host's crypto grade wrongly stamped every unrelated
    // endpoint "proven accurate" and jumped it to the top. taskCat is null for
    // tasks we have no accuracy ground truth for, so no boost is applied there.
    const taskCat = accuracyCategoryOf(task);
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
      const hostAcc = accurate[e.host];
      const acc = hostAcc && hostAcc.category === taskCat ? hostAcc : null;  // same-category only
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
      graded_accurate: acc ? { category: acc.category, off_by: acc.off_by, unit: acc.unit, vs: acc.vs } : null,
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
    // A target that cannot be a host is a CALLER WIRING BUG, and answering it
    // with a plain UNRATED hides that: on 2026-09-05 an integration sent 79
    // calls for "[object Object]" and Node event names ("data", "ready",
    // "connect"), got UNRATED for every one, and kept paying with no guard
    // running. Older clients still do this, so the server names it. The
    // response SHAPE is unchanged (still a verdict object, still fail-open) —
    // it just tells the truth about why there is no verdict.
    const looksLikeHost = host === "localhost" || host.endsWith(".localhost") || host.includes(".");
    if (!looksLikeHost) {
      return {
        host, light: "gray", verdict: "UNRATED", input_error: true,
        gate: `"${host}" is not a hostname, so nothing was checked and this is NOT a clearance. ` +
          "Pass the seller URL you are about to pay, e.g. https://seller.example/api. " +
          "If you are using preflight-x402, wrap the fetch function itself " +
          "(preflightFetch(fetch)) and call the wrapper the way you call fetch; " +
          "passing an object or an event name produces exactly this.",
        reasons: [{ level: "yellow", text: "The target could not be read as a host. Your client is " +
          "likely passing something other than the seller URL." }],
        always: ALWAYS, as_of: pf.generated,
      };
    }
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
      // How much this verdict is backed by: "verified" = a payment settled AND the result was gradeable,
      // "checked" = free live probe only, "unproven" = listed but not checked. A
      // CLEAR you can gate hard on is a verified one; a checked CLEAR means only
      // that its live 402 looked honest, not that anyone has spent against it.
      confidence: v.confidence || (v.receipts ? "verified" : v.checked_live ? "checked" : "unproven"),
      confidence_basis: v.confidence_basis,
      gate,
      reasons: v.reasons,
      evidence: {
        receipts: v.receipts, disputes: v.disputes,
        delivered: v.delivered, accurate: v.accurate,
        checked_live: v.checked_live,
      },
      // The differentiator over probe-only checkers: this verdict is backed by real
      // payments to this exact seller, not an inspection of its 402 challenge.
      payment_history: v.history || null,
      ...(detail ? { detail_checks: detail } : {}),
      seller_page: `https://whatagentsbuy.com/s/${host}`,
      always: ALWAYS,
      as_of: pf.generated,
    };
  }

  // (The old stripped check_before_paying handler was removed: check_before_paying
  // is now the primary name for the full preflight verdict, routed via TOOL_ALIASES.)

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

  if (name === "market_pulse") {
    const d = await fetchJson("/api/dashboard");
    const h = d.hero || {};
    const c = d.circ || {};
    const b = d.baseSeq || {};
    // Flat and named, not the raw dashboard payload: an agent should not have to
    // know that `vol24` is dollars and `tx24` is a count.
    return {
      as_of: d.generated,
      basis: "UTC calendar days; the current day is partial and reported separately",
      x402: {
        volume_24h_usd: h.vol24 ?? null,
        payments_24h: h.tx24 ?? null,
        volume_change_pct_vs_prior_day: h.vol24DeltaPct ?? null,
        payments_change_pct_vs_prior_day: h.tx24DeltaPct ?? null,
        daily_closed_days: (h.daily7 || []).map((p) => ({
          date: p.date, volume_usd: p.vol, payments: p.tx,
        })),
        today_so_far: h.today
          ? { date: h.today.date, volume_usd: h.today.vol, payments: h.today.tx,
              partial: true, through: h.today.through ?? null }
          : null,
      },
      stablecoins: {
        usdc_circulating_usd: c.usdc?.now ?? null,
        all_circulating_usd: c.stables?.now ?? null,
        source: "DefiLlama on-chain supply, last closed UTC day",
      },
      base_chain: {
        sequencer_fees_24h_usd: b.fees24h ?? null,
        transactions_per_day: b.txDay ?? null,
      },
      caveats: [
        "x402 figures count every chain the protocol settles on, roughly 90% Base.",
        "The current UTC day is incomplete; compare closed days to closed days.",
        "Payment COUNT is heavily concentrated: one high-volume wallet can be most " +
          "of the transactions while moving a small share of the dollars, so quoting " +
          "transaction counts alone overstates how broad the market is.",
      ],
      source: "https://whatagentsbuy.com/x402",
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

// --- Durable wide-event telemetry ------------------------------------------
// One rich structured row per MCP message, written to an always-on store so
// usage survives redeploys instead of being reconstructed from Vercel's
// ephemeral logs (which drop data on any tailer gap). The canonical-log-line /
// wide-event pattern: everything about a call in one row, queryable forever.
// Awaited before the response returns, so a row is only ever lost if the store
// itself is unreachable, never a gap in a scraper. Privacy: host + coarse client
// + our own verdict; never raw args, never a raw IP (hashed).
const EVENTS_URL = process.env.WAB_EVENTS_URL || "";
const EVENTS_KEY = process.env.WAB_EVENTS_KEY || "";

// Classify the caller for honest measurement. The rule that matters: only count
// something as an "agent" when it clearly IS one. Anything that indexes, probes,
// audits, scans, or researches the ecosystem is a crawler; raw HTTP libraries and
// bare runtimes are tooling; unrecognized clients are "unknown", NOT agents. Being
// conservative here is the whole point -- an inflated agent count is a lie about
// adoption. Ordered: crawler first (it catches research/scanner names), then named
// agent frameworks, then tooling, then unknown.
export function clientKind(ua) {
  const t = (ua || "").toLowerCase();
  if (!t) return "unknown";
  if (/probe|crawl|index|glama|mcpbeat|beat|health|monitor|uptime|scan|scrap|\bbot\b|registry|catalog|watch|verify|audit|observ|sentinel|bureau|spike|check|research|census|almanac|snapshot|directory|exchange|trust|vouch|reach|oracle|golem|rugpull|doctor|inspect|scanner/.test(t)) return "crawler";
  if (/\bcline\b|claude-code|claude-ai|claude-desktop|langchain|llama-?index|autogpt|crew|agent-tools|preflight-x402|goose|continue|cursor|windsurf|smithery|mcp-client|openai-agents|anthropic/.test(t)) return "agent";
  if (/curl|wget|python|node-fetch|go-http|okhttp|axios|httpx|reqwest|java\/|libwww|undici|deno|\bnode\b|node\.js|ruby|php|dart|httpclient|got\/|ky\//.test(t)) return "tooling";
  return "unknown";
}

function ipHashOf(req) {
  const ip = String(req.headers?.["x-forwarded-for"] || "").split(",")[0].trim();
  return ip ? createHash("sha256").update(ip + "|wab-evt-v1").digest("hex").slice(0, 16) : null;
}

async function logEvents(events) {
  if (!EVENTS_URL || !EVENTS_KEY || !events.length) return;
  await fetch(EVENTS_URL, {
    method: "POST",
    headers: {
      apikey: EVENTS_KEY, authorization: `Bearer ${EVENTS_KEY}`,
      "content-type": "application/json", prefer: "return=minimal",
    },
    body: JSON.stringify(events),
  });
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
  // Shared context for the durable wide events (computed once per request).
  const events = [];
  const ua = String(req.headers?.["user-agent"] || "");
  const kind = clientKind(ua);
  const ipHash = ipHashOf(req);
  const region = process.env.VERCEL_REGION || null;
  const ctx = () => ({ client, client_kind: kind, ua: ua.slice(0, 220),
                       ip_hash: ipHash, batch_size: batch.length, region });

  for (const msg of batch) {
    const { id, method, params } = msg || {};
    const isNotification = id === undefined || id === null;
    try {
      if (method === "initialize") {
        const c = params?.clientInfo || {};
        log(`initialize client=${c.name || "?"}/${c.version || "?"} protocol=${params?.protocolVersion || "?"}`);
        events.push({ surface: "mcp", method: "initialize", status: "ok", ok: true,
                      protocol: params?.protocolVersion || null, ...ctx() });
        out.push(rpc(id, {
          // Echo the client's version when it names one; guessing a version the
          // client does not speak is how these handshakes fail.
          protocolVersion: params?.protocolVersion || FALLBACK_PROTOCOL,
          capabilities: { tools: { listChanged: false } },
          serverInfo: SERVER,
          instructions:
            "Independent measurements of x402 API sellers. Grades come from actually paying each " +
            "service and recording what happened; nothing is sponsored. WHICH TOOL FOR WHICH JOB: " +
            "need an API for a task -> find_api. About to pay one -> check_before_paying (returns a " +
            "CLEAR / HOLD / ABORT verdict to gate on; detail:true for the full read). Deep-dive one " +
            "seller -> look_up_seller. Want a leaderboard -> rank_sellers (by revenue or real_demand). " +
            "A category with a right answer (crypto/stock/fx/gas/wallet/weather) -> rank_by_accuracy. " +
            "How big is x402 -> market_size. Avoid known losses -> known_payment_traps. Every verdict " +
            "carries a CONFIDENCE beyond the light: verified = a payment to this seller actually SETTLED and produced a gradeable result " +
            "what came back (and how recently); checked = a free probe of its 402 only, no money moved; " +
            "unproven = listed, not checked. A CLEAR you can lean on is a verified one, so gate on " +
            "confidence too, not just the light, for payments that matter. The one rule " +
            "that overrides everything: read the payTo and amount from the live 402 and sign against " +
            "those, never a listing. Older tool names (preflight, get_service, top_services, " +
            "most_accurate, is_organic, list_traps, market_summary, market_pulse) still work.",
        }));
      } else if (method === "notifications/initialized" || method === "notifications/cancelled") {
        // nothing to do, and nothing to reply to
      } else if (method === "ping") {
        out.push(rpc(id, {}));
      } else if (method === "tools/list") {
        log("tools/list");
        events.push({ surface: "mcp", method: "tools/list", status: "ok", ok: true, ...ctx() });
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
        // The wide event: the resolved tool, the normalized host it acted on, and
        // crucially OUR verdict (CLEAR/HOLD/ABORT + confidence) — the answer we gave,
        // which the old log threw away. This is what makes "our ABORT rate" and
        // "which sellers we flag" answerable.
        events.push({
          surface: "mcp", method: "tools/call",
          tool: TOOL_ALIASES[params?.name] || params?.name, tool_raw: params?.name,
          host: normHost(arg.url || arg.host || "") || null,
          task: arg.task || arg.query || arg.category || null,
          verdict: (data && (data.verdict ?? data.light)) ?? null,
          confidence: (data && data.confidence) ?? null,
          ms: Date.now() - t0, status: "ok", ok: true,
          args_keys: Object.keys(arg),
          raw: data ? { light: data.light ?? null, verdict: data.verdict ?? null,
                        confidence: data.confidence ?? null, by: arg.by ?? null } : null,
          ...ctx(),
        });
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
        events.push({
          surface: "mcp", method: "tools/call",
          tool: TOOL_ALIASES[params?.name] || params?.name, tool_raw: params?.name,
          host: normHost(params?.arguments?.url || params?.arguments?.host || "") || null,
          ms: Date.now() - t0, status: "error", ok: false,
          err: String(e.message).slice(0, 300), ...ctx(),
        });
        out.push(rpc(id, {
          content: [{ type: "text", text: `Error: ${e.message}` }],
          isError: true,
        }));
      } else if (!isNotification) {
        out.push(rpcError(id, -32603, e.message));
      }
    }
  }

  // Durably record the wide events BEFORE returning. Awaited so the write is
  // guaranteed to land (durability beats the few ms of latency — data loss is
  // the one thing this system cannot do); wrapped so telemetry can never break
  // or fail a real tool call.
  try { await logEvents(events); } catch { /* never break a call over logging */ }
  if (!out.length) return res.status(202).end();
  res.setHeader("content-type", "application/json");
  return res.status(200).json(Array.isArray(body) ? out : out[0]);
}
