// Tests for the agent-facing MCP server (api/mcp.js). Node's built-in runner,
// no dependencies:  node --test api/
//
// The tool handlers fetch site JSON; runTool takes an injectable fetcher so
// these drive it with fixtures, no network. The invariant that matters most:
// preflight passes the site's verdict through faithfully and an unknown host is
// UNRATED, never a false claim, and check_before_paying surfaces a payTo
// mismatch as CRITICAL.

import test from "node:test";
import assert from "node:assert/strict";
import handler, { normHost, rank, runTool, clientToken, clientKind } from "./mcp.js";

test("clientKind counts only real agents as agents (honest measurement)", () => {
  // research/scanner/monitor tools are NOT agents, however they name themselves
  for (const ua of ["mcp-rugpull-research/1.0", "mcp-scraper/1", "x402-observer",
                    "sentineloracle/0.1", "vouch-census", "golemreachtrustbot", "AgentCommonsDoctor/0.1"]) {
    assert.equal(clientKind(ua), "crawler", `${ua} should be crawler, not agent`);
  }
  // raw HTTP libraries and bare runtimes are tooling, not agents
  for (const ua of ["curl/8.7", "python-httpx/0.27", "node", "go-http-client/2", "undici"]) {
    assert.equal(clientKind(ua), "tooling", `${ua} should be tooling`);
  }
  // only clearly-identified agent clients count as agents
  for (const ua of ["claude-code/2.1", "cline/3.0", "langchain-mcp", "preflight-x402/0.2"]) {
    assert.equal(clientKind(ua), "agent", `${ua} should be agent`);
  }
  // unrecognized clients are unknown, never silently counted as agents
  assert.equal(clientKind("zevruna/1.0"), "unknown");
  assert.equal(clientKind(""), "unknown");
});

// Minimal Vercel-style req/res so we can exercise the real handler, including the
// raw-body read and the batch cap, with no network and no server.
function mockRes() {
  const r = { statusCode: null, body: null, headers: {}, ended: false };
  r.setHeader = (k, v) => { r.headers[k.toLowerCase()] = v; };
  r.status = (n) => { r.statusCode = n; return r; };
  r.json = (o) => { r.body = o; return r; };
  r.end = () => { r.ended = true; return r; };
  return r;
}
function mockReq(method, bodyStr) {
  // async-iterable request stream, like Vercel's IncomingMessage with the parser off
  return { method, [Symbol.asyncIterator]: async function* () { if (bodyStr != null) yield Buffer.from(bodyStr); } };
}

test("normHost normalizes urls and bare hosts to the data key", () => {
  assert.equal(normHost("https://blockrun.ai/api/v1/exa/search"), "blockrun.ai");
  assert.equal(normHost("blockrun.ai"), "blockrun.ai");
  assert.equal(normHost("https://www.Example.com/x"), "example.com");
  assert.equal(normHost("WWW.Foo.com"), "foo.com");
  assert.equal(normHost("  Blockrun.AI  "), "blockrun.ai");
  assert.equal(normHost(""), "");
  assert.equal(normHost(null), "");
});

test("normHost: every equivalent spelling of a seller resolves to the same host", () => {
  // The bypass this guards against: a whitespace-padded URL used to skip URL
  // parsing (the scheme regex saw the raw string) and return "https:" as the
  // host, turning a known ABORT into UNRATED before the payment callback ran.
  assert.equal(normHost(" https://evil.example/pay"), "evil.example");
  assert.equal(normHost("\thttps://evil.example/pay \n"), "evil.example");
  assert.equal(normHost("HTTPS://WWW.EVIL.EXAMPLE:8443/x"), "evil.example");
  assert.equal(normHost("https://user:pass@evil.example/x"), "evil.example");
  assert.equal(normHost("evil.example:8443"), "evil.example");
  assert.equal(normHost("  bare.example  "), "bare.example");
  // distinct subdomains must NOT be over-normalized together
  assert.notEqual(normHost("api.evil.example"), normHost("evil.example"));
});

test("clientToken keeps only the coarse client family, no versions/PII", () => {
  assert.equal(clientToken("cline/3.4.0"), "cline");
  assert.equal(clientToken("claude-code/1.2 (macOS)"), "claude-code");
  assert.equal(clientToken("python-httpx/0.27"), "python-httpx");
  assert.equal(clientToken("curl/8.4.0"), "curl");
  assert.equal(clientToken(""), "unknown");
  assert.equal(clientToken(undefined), "unknown");
  assert.ok(clientToken("x".repeat(200)).length <= 24);   // bounded
});

test("rank orders grades A+ (best) to F (worst)", () => {
  assert.ok(rank("A+") < rank("B"));
  assert.ok(rank("B") < rank("F"));
  assert.equal(rank("not-a-grade"), 99);
});

// a fetcher that returns fixtures by path; throws on an unexpected path so a
// tool that fetches something the test did not stub fails loudly.
function mockFetch(fixtures) {
  return async (path) => {
    if (!(path in fixtures)) throw new Error("unexpected fetch: " + path);
    return fixtures[path];
  };
}

test("preflight passes the seller's verdict through, with the always-rule", async () => {
  const fx = { "/api/preflight.json": { generated: "2026-08-16", sellers: {
    "bad.example": { host: "bad.example", light: "red", score: 55,
      reasons: [{ level: "red", text: "payTo mismatch" }],
      receipts: 0, disputes: 0, delivered: 0, accurate: 0, checked_live: true,
      confidence: "checked", confidence_basis: "free live checks only" },
  } } };
  const out = await runTool("preflight", { url: "https://bad.example/pay" }, mockFetch(fx));
  assert.equal(out.host, "bad.example");
  assert.equal(out.light, "red");
  assert.equal(out.verdict, "ABORT");
  assert.equal(out.confidence, "checked");           // depth of evidence flows through
  assert.match(out.gate, /do not pay/i);
  assert.ok(out.always.includes("live 402"));
});

test("preflight on an unknown host is UNRATED, never a false accusation", async () => {
  const fx = { "/api/preflight.json": { generated: "x", sellers: {} } };
  const out = await runTool("preflight", { url: "never-seen.example" }, mockFetch(fx));
  assert.equal(out.verdict, "UNRATED");
  assert.equal(out.light, "gray");
});

// A green host the free probe never reached must NOT be sold as payment-safety
// verified. This is the gate-honesty fix: the wording has to disclose that.
test("preflight green with checked_live:false does not claim payment-safety verified", async () => {
  const fx = { "/api/preflight.json": { generated: "x", sellers: {
    "clean.example": { host: "clean.example", light: "green", score: 80, reasons: [],
      receipts: 3, disputes: 0, delivered: 0, accurate: 3, checked_live: false },
  } } };
  const out = await runTool("preflight", { url: "clean.example" }, mockFetch(fx));
  assert.equal(out.verdict, "CLEAR");
  assert.doesNotMatch(out.gate, /safe to pay/i);         // must not over-promise
  assert.match(out.gate, /has not reached|live 402/i);   // discloses the gap
});

test("preflight with detail:true attaches the payment-safety detail", async () => {
  const fx = {
    "/api/preflight.json": { generated: "x", sellers: {
      "seller.example": { host: "seller.example", light: "green", score: 90, reasons: [],
        receipts: 1, disputes: 0, delivered: 1, accurate: 0, checked_live: true },
    } },
    "/api/probe.json": { generated: "x", origins: [{
      host: "seller.example", answered: true, payto_matches_listing: true,
      price_matches_listing: true, phantom_paywall: false, endpoints_checked: 1, returns_402_unpaid: 1,
    }] },
    "/api/ratings.json": { ratings: [] },
    "/api/leaderboard.json": { rows: [] },
  };
  const bare = await runTool("preflight", { url: "seller.example" }, mockFetch(fx));
  assert.equal(bare.detail_checks, undefined);           // off by default
  const out = await runTool("preflight", { url: "seller.example", detail: true }, mockFetch(fx));
  assert.ok(out.detail_checks, "detail:true should attach detail_checks");
  assert.equal(out.detail_checks.checked_live, true);
});

test("check_before_paying is the primary name for the preflight verdict (ABORT on payTo mismatch)", async () => {
  const fx = { "/api/preflight.json": { generated: "x", sellers: {
    "poison.example": { host: "poison.example", light: "red", score: 40,
      reasons: [{ level: "red", text: "its live payment address does not match its listing" }],
      receipts: 0, disputes: 0, delivered: 0, accurate: 0, checked_live: true, confidence: "checked" },
  } } };
  const out = await runTool("check_before_paying", { url: "https://poison.example/pay" }, mockFetch(fx));
  assert.equal(out.verdict, "ABORT");           // the outcome-named tool routes to preflight
  assert.equal(out.light, "red");
  assert.ok(out.always.includes("live 402"));
});

test("outcome-named aliases route to the right handler", async () => {
  const fx = { "/api/leaderboard.json": { rows: [
    { host: "a.example", usdc_received: 100, settlements: 5, paying_wallets: 3, organic_demand_score: 90 },
  ] } };
  const ranked = await runTool("rank_sellers", { by: "real_demand" }, mockFetch(fx));  // was top_services
  assert.equal(ranked.ranked_by, "organic_demand");
  const seller = await runTool("look_up_seller", { host: "a.example" }, mockFetch({    // was get_service
    "/api/ratings.json": { ratings: [] },
    "/api/leaderboard.json": fx["/api/leaderboard.json"] }));
  assert.equal(seller.host, "a.example");
});

// find_api now fetches the catalog AND the accuracy corpus, so both must be stubbed.
const EMPTY_CORPUS = { "/api/categories.json": { generated: "x", categories: {} } };

test("find_api drops irrelevant, ranks the relevant, and covers the long tail by default", async () => {
  const fx = { ...EMPTY_CORPUS, "/api/catalog.json": { generated: "x", endpoints: [
    { host: "good.example", url: "https://good.example", description: "validate an email address", price_usdc: 0.001, reliability: 90 },
    { host: "off-topic.example", url: "https://off-topic.example", description: "an unrelated widget", price_usdc: 0.001, reliability: 90 },
    { host: "flaky.example", url: "https://flaky.example", description: "email validation service", price_usdc: 0.001, reliability: 10 },
  ] } };
  const out = await runTool("find_api", { task: "validate email" }, mockFetch(fx));
  assert.equal(out.results[0].host, "good.example");          // relevant + reliable ranks first
  assert.ok(!out.results.some((r) => r.host === "off-topic.example")); // irrelevant dropped
  // NO reliability floor by default: the low-reliability but RELEVANT seller is
  // included, not hidden. A discovery tool covers the whole reachable market.
  assert.ok(out.results.some((r) => r.host === "flaky.example"));
  // ...but a caller can opt into a floor, and then it is excluded.
  const floored = await runTool("find_api", { task: "validate email", min_reliability: 70 }, mockFetch(fx));
  assert.ok(!floored.results.some((r) => r.host === "flaky.example"));
});

// A seller we PAID and found returns the correct number outranks a merely-reliable
// one, even when the reliable one has a better description match.
test("find_api boosts a proven-accurate seller to the top", async () => {
  const fx = {
    "/api/categories.json": { generated: "x", categories: {
      "crypto-price": { metric: "BTC/USD", unit: "USD", sellers: [
        { host: "accurate.example", value: 60000, deviation: 1, price_usdc: 0.002, verdict: "accurate" },
      ] },
    } },
    "/api/catalog.json": { generated: "x", endpoints: [
      { host: "wordy.example", url: "https://wordy.example", description: "btc price crypto price bitcoin price feed", price_usdc: 0.001, reliability: 95 },
      { host: "accurate.example", url: "https://accurate.example", description: "btc price", price_usdc: 0.002, reliability: 80 },
    ] },
  };
  const out = await runTool("find_api", { task: "btc price" }, mockFetch(fx));
  assert.equal(out.results[0].host, "accurate.example");    // proven-accurate wins over word-match
  assert.ok(out.results[0].graded_accurate, "should carry the accuracy grade");
  assert.equal(out.results[0].graded_accurate.unit, "USD");
  assert.equal(out.results[0].graded_accurate.category, "crypto-price");
});

// The bug this guards: a host proven accurate at crypto-price had that grade
// stamped onto its UNRELATED endpoints (e.g. email validation) and jumped to the
// top, telling agents an email validator was "accurate ... vs BTC/USD spot".
test("find_api never attaches a cross-category accuracy grade", async () => {
  const fx = {
    "/api/categories.json": { generated: "x", categories: {
      "crypto-price": { metric: "BTC/USD", unit: "USD", sellers: [
        { host: "multi.example", value: 60000, deviation: 1, price_usdc: 0.002, verdict: "accurate" },
      ] },
    } },
    "/api/catalog.json": { generated: "x", endpoints: [
      { host: "multi.example", url: "https://multi.example/validate/email", description: "email validation verify email address disposable check", price_usdc: 0.002, reliability: 90 },
      { host: "plain.example", url: "https://plain.example", description: "email validation verify email address disposable check", price_usdc: 0.001, reliability: 95 },
    ] },
  };
  const out = await runTool("find_api", { task: "validate an email address" }, mockFetch(fx));
  const multi = out.results.find((r) => r.host === "multi.example");
  assert.ok(multi, "the seller still shows for the email task it matches");
  assert.equal(multi.graded_accurate, null, "its crypto grade must NOT ride along on an email endpoint");
  assert.equal(out.results[0].host, "plain.example", "the crypto grade must not boost it over a better email match");
});

test("most_accurate ranks a category and lists categories when asked bare", async () => {
  const fx = { "/api/categories.json": { generated: "x", categories: {
    "crypto-price": { metric: "BTC/USD", source: "Coinbase/Kraken median", reference: "exchange", unit: "USD",
      sellers: [
        { host: "close.example", value: 60001, deviation: 1, price_usdc: 0.003, verdict: "accurate" },
        { host: "far.example", value: 60500, deviation: 500, price_usdc: 0.001, verdict: "off" },
      ] },
  } } };
  const bare = await runTool("most_accurate", {}, mockFetch(fx));
  assert.deepEqual(bare.available_categories, ["crypto-price"]);
  const out = await runTool("most_accurate", { category: "crypto-price" }, mockFetch(fx));
  assert.equal(out.count, 2);
  assert.equal(out.ranked[0].host, "close.example");        // corpus is pre-ranked by deviation
  assert.equal(out.ranked[0].unit, "USD");
  const miss = await runTool("most_accurate", { category: "no-such-cat" }, mockFetch(fx));
  assert.equal(miss.found, false);
});

test("is_organic ranks by demand-vs-money gap and never labels high payout as recycled", async () => {
  const fx = { "/api/organic.json": { windows: { "7d": {
    wash_share_pct: 31.3,
    ranking: [
      { demand_rank: 1, money_rank: 44, rank_gap: 43, service: "OneSource", host: "api.onesource.io",
        organic_demand_score: 100, organic_demand_parts: { breadth: 40 }, confidence: "high",
        usdc_received: 100, paying_wallets: 99, sends_back_pct: 5, recycled: false, inflated: false },
      { demand_rank: 23, money_rank: 2, rank_gap: -21, service: "Laso", host: "laso.finance",
        organic_demand_score: 39, organic_demand_parts: {}, confidence: "high",
        usdc_received: 50000, paying_wallets: 6, sends_back_pct: 8, recycled: false, inflated: true },
      { demand_rank: 7, money_rank: 3, rank_gap: -4, service: "Boats", host: "mcp.x402.boats",
        organic_demand_score: 76, organic_demand_parts: {}, confidence: "high",
        usdc_received: 40000, paying_wallets: 30, sends_back_pct: 100, recycled: true, inflated: true },
    ] } } } };
  const genuine = await runTool("is_organic", { host: "api.onesource.io" }, mockFetch(fx));
  assert.equal(genuine.found, true);
  assert.match(genuine.verdict, /underrated/);
  assert.equal(genuine.inflated, false);

  const inflated = await runTool("is_organic", { host: "https://laso.finance/pay" }, mockFetch(fx));
  assert.match(inflated.verdict, /inflated/);
  assert.equal(inflated.inflated, true);

  // Boats sends back 100% of what it takes in but has a small rank gap. The
  // retired payout-ratio flag would have called this "recycled"; the honest
  // read is that its money and demand ranks roughly agree. Payout never verdicts.
  const highPayout = await runTool("is_organic", { host: "mcp.x402.boats" }, mockFetch(fx));
  assert.match(highPayout.verdict, /consistent/);
  assert.equal(highPayout.verdict.includes("recycl"), false);

  const missing = await runTool("is_organic", { host: "unknown.example" }, mockFetch(fx));
  assert.equal(missing.found, false);
  assert.equal(missing.market_wash_share_pct, 31.3);
});

test("an unknown tool name throws", async () => {
  await assert.rejects(() => runTool("no_such_tool", {}, mockFetch({})));
});

// ---- handler-level: raw-body parse errors and the batch cap (#4, #5) ----

test("handler: malformed JSON body returns a proper JSON-RPC -32700", async () => {
  const res = mockRes();
  await handler(mockReq("POST", "{not json"), res);
  assert.equal(res.statusCode, 400);
  assert.equal(res.body.error.code, -32700);        // not an opaque empty 400
});

test("handler: empty body returns -32700", async () => {
  const res = mockRes();
  await handler(mockReq("POST", ""), res);
  assert.equal(res.statusCode, 400);
  assert.equal(res.body.error.code, -32700);
});

test("handler: an oversized batch is refused before any tool runs", async () => {
  const big = JSON.stringify(Array.from({ length: 51 }, (_, i) => ({ jsonrpc: "2.0", id: i, method: "ping" })));
  const res = mockRes();
  await handler(mockReq("POST", big), res);
  assert.equal(res.statusCode, 400);
  assert.equal(res.body.error.code, -32600);
  assert.match(res.body.error.message, /batch too large/);
});

test("handler: a legal 50-message batch is accepted, streamed body parsed", async () => {
  const ok = JSON.stringify(Array.from({ length: 50 }, (_, i) => ({ jsonrpc: "2.0", id: i, method: "ping" })));
  const res = mockRes();
  await handler(mockReq("POST", ok), res);
  assert.equal(res.statusCode, 200);
  assert.equal(res.body.length, 50);
});

test("handler: a valid single ping over the stream works", async () => {
  const res = mockRes();
  await handler(mockReq("POST", JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping" })), res);
  assert.equal(res.statusCode, 200);
  assert.deepEqual(res.body, { jsonrpc: "2.0", id: 1, result: {} });
});

test("preflight names a non-host target instead of shrugging UNRATED", async () => {
  // 2026-09-05: 79 real calls for "[object Object]" and event names each came
  // back a bare UNRATED, so a broken integration kept paying unguarded with no
  // signal. Older clients still send these; the server must diagnose them.
  const fx = { "/api/preflight.json": { generated: "2026-09-07", sellers: {} } };
  for (const bad of ["[object Object]", "data", "ready", "connect"]) {
    const r = await runTool("preflight", { url: bad }, mockFetch(fx));
    assert.equal(r.verdict, "UNRATED");
    assert.equal(r.input_error, true, `${bad} should be flagged as an input error`);
    assert.match(r.gate, /is not a hostname/);
    assert.match(r.gate, /NOT a clearance/);
  }
  // a real host with no data still gets the ordinary "no data" answer
  const ok = await runTool("preflight", { url: "unknown-seller.example" }, mockFetch(fx));
  assert.equal(ok.verdict, "UNRATED");
  assert.equal(ok.input_error, undefined);
  assert.match(ok.gate, /No data on this host yet/);
  // localhost stays usable for local development
  const local = await runTool("preflight", { url: "http://localhost:8080/x" }, mockFetch(fx));
  assert.equal(local.input_error, undefined);
});
