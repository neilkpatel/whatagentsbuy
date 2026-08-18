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
import handler, { normHost, rank, runTool, clientToken } from "./mcp.js";

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
      receipts: 0, disputes: 0, delivered: 0, accurate: 0, checked_live: true },
  } } };
  const out = await runTool("preflight", { url: "https://bad.example/pay" }, mockFetch(fx));
  assert.equal(out.host, "bad.example");
  assert.equal(out.light, "red");
  assert.equal(out.verdict, "ABORT");
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

test("check_before_paying (alias) still flags a payTo mismatch as CRITICAL", async () => {
  const fx = {
    "/api/probe.json": { generated: "x", origins: [{
      host: "poison.example", answered: true,
      payto_matches_listing: false, price_matches_listing: true,
      phantom_paywall: false, endpoints_checked: 2, returns_402_unpaid: 2,
    }] },
    "/api/ratings.json": { ratings: [] },
    "/api/leaderboard.json": { rows: [] },
  };
  const out = await runTool("check_before_paying", { url: "poison.example" }, mockFetch(fx));
  assert.ok(out.warnings.some((w) => /CRITICAL/.test(w)), "expected a CRITICAL payTo warning");
});

// find_api now fetches the catalog AND the accuracy corpus, so both must be stubbed.
const EMPTY_CORPUS = { "/api/categories.json": { generated: "x", categories: {} } };

test("find_api filters by reliability, drops irrelevant, ranks the relevant", async () => {
  const fx = { ...EMPTY_CORPUS, "/api/catalog.json": { generated: "x", endpoints: [
    { host: "good.example", url: "https://good.example", description: "validate an email address", price_usdc: 0.001, reliability: 90 },
    { host: "off-topic.example", url: "https://off-topic.example", description: "an unrelated widget", price_usdc: 0.001, reliability: 90 },
    { host: "flaky.example", url: "https://flaky.example", description: "email validation service", price_usdc: 0.001, reliability: 10 },
  ] } };
  const out = await runTool("find_api", { task: "validate email" }, mockFetch(fx));
  assert.equal(out.results[0].host, "good.example");          // relevant + reliable ranks first
  assert.ok(!out.results.some((r) => r.host === "off-topic.example")); // irrelevant dropped
  assert.ok(!out.results.some((r) => r.host === "flaky.example"));     // below min reliability
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
