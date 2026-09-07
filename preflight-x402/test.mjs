// Tests for preflight-x402. Node's built-in runner, no deps:  node test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { preflight, assertPayable, guardedPay, preflightFetch, PreflightAbort, paymentProof } from "./index.mjs";

// a fake fetch that returns a given verdict as the MCP would shape it
const fakeFetch = (light, extra = {}) => async () => ({
  json: async () => ({ result: { structuredContent: { host: "seller.example", light, verdict: light.toUpperCase(), reasons: [], ...extra } } }),
});

test("preflight returns the seller verdict", async () => {
  const v = await preflight("https://seller.example/pay", { fetchImpl: fakeFetch("green") });
  assert.equal(v.host, "seller.example");
  assert.equal(v.light, "green");
});

test("assertPayable throws PreflightAbort on red", async () => {
  await assert.rejects(
    () => assertPayable("seller.example", { fetchImpl: fakeFetch("red", { reasons: [{ text: "payTo mismatch" }] }) }),
    (e) => e instanceof PreflightAbort && /payTo mismatch/.test(e.message));
});

test("assertPayable passes on green and returns the verdict", async () => {
  const v = await assertPayable("seller.example", { fetchImpl: fakeFetch("green") });
  assert.equal(v.light, "green");
});

test("assertPayable warns but does not throw on yellow", async () => {
  let warned = false;
  const v = await assertPayable("seller.example", { fetchImpl: fakeFetch("yellow"), onWarn: () => { warned = true; } });
  assert.equal(v.light, "yellow");
  assert.ok(warned, "HOLD should warn");
});

test("guardedPay pays on green, never pays on red", async () => {
  let paid = false;
  await guardedPay("seller.example", () => { paid = true; return "ok"; }, { fetchImpl: fakeFetch("green") });
  assert.ok(paid, "should pay when CLEAR");

  paid = false;
  await assert.rejects(() => guardedPay("seller.example", () => { paid = true; }, { fetchImpl: fakeFetch("red") }),
    (e) => e instanceof PreflightAbort);
  assert.equal(paid, false, "must not pay on ABORT");
});

test("paymentProof states real spend, and is empty when never paid", async () => {
  const v = await preflight("seller.example", { fetchImpl: fakeFetch("green", {
    payment_history: { times_paid: 8, span_days: 4, delivered: 6, accurate: 2, disputed: 0, last_verdict: "accurate" },
  }) });
  const proof = paymentProof(v);
  assert.match(proof, /paid 8x over 4 days/);
  assert.match(proof, /6 delivered, 2 graded accurate/);
  assert.match(proof, /last: accurate/);
  // no history -> empty string, never a fabricated claim
  assert.equal(paymentProof(await preflight("x.example", { fetchImpl: fakeFetch("gray") })), "");
});

test("fail OPEN: a network error yields UNRATED, never blocks", async () => {
  const boom = async () => { throw new Error("network down"); };
  const v = await preflight("seller.example", { fetchImpl: boom });
  assert.equal(v.light, "gray");
  assert.equal(v.verdict, "UNRATED");
  assert.equal(v.unavailable, true);
  // and assertPayable does not throw (red-only block, and this is gray)
  const v2 = await assertPayable("seller.example", { fetchImpl: boom });
  assert.equal(v2.light, "gray");
});

test("minConfidence blocks a checked seller when verified is required", async () => {
  const checkedGreen = fakeFetch("green", { confidence: "checked", confidence_basis: "free live checks only" });
  const v = await assertPayable("seller.example", { fetchImpl: checkedGreen });   // default pays
  assert.equal(v.light, "green");
  await assert.rejects(
    () => assertPayable("seller.example", { fetchImpl: checkedGreen, minConfidence: "verified" }),
    (e) => e instanceof PreflightAbort && /below required "verified"/.test(e.message));
  const verifiedGreen = fakeFetch("green", { confidence: "verified", confidence_basis: "backed by 8 real payments" });
  const ok = await assertPayable("seller.example", { fetchImpl: verifiedGreen, minConfidence: "verified" });
  assert.equal(ok.confidence, "verified");
});

test("minConfidence stays fail-open when preflight is unavailable", async () => {
  const boom = async () => { throw new Error("down"); };
  const v = await assertPayable("seller.example", { fetchImpl: boom, minConfidence: "verified" });
  assert.equal(v.light, "gray");   // no confidence to judge -> must not block
});

test("preflightFetch runs inner fetch when CLEAR", async () => {
  let called = false;
  const inner = async () => { called = true; return "resp"; };
  const f = preflightFetch(inner, { fetchImpl: fakeFetch("green") });
  assert.equal(await f("https://seller.example/pay"), "resp");
  assert.ok(called, "inner fetch should run on CLEAR");
});

test("preflightFetch aborts before inner fetch on ABORT", async () => {
  let called = false;
  const inner = async () => { called = true; return "resp"; };
  const f = preflightFetch(inner, { fetchImpl: fakeFetch("red", { reasons: [{ text: "payTo mismatch" }] }) });
  await assert.rejects(() => f("https://seller.example/pay"), (e) => e instanceof PreflightAbort);
  assert.equal(called, false, "inner fetch must NOT run on ABORT");
});

test("preflightFetch honors minConfidence", async () => {
  const f = preflightFetch(async () => "resp",
    { fetchImpl: fakeFetch("green", { confidence: "checked" }), minConfidence: "verified" });
  await assert.rejects(() => f("https://seller.example/pay"), (e) => e instanceof PreflightAbort);
});

// ---- F1 regression: URL normalization at the package boundary ---------------

test("a whitespace-padded URL is trimmed before it reaches the server", async () => {
  let sent;
  const capture = async (_endpoint, init) => {
    sent = JSON.parse(init.body).params.arguments.url;
    return { json: async () => ({ result: { structuredContent: { host: "seller.example", light: "green", verdict: "CLEAR", reasons: [] } } }) };
  };
  await preflight("  https://seller.example/pay \n", { fetchImpl: capture });
  assert.equal(sent, "https://seller.example/pay");
});

test("the outage fallback derives the real host from a padded URL", async () => {
  const boom = async () => { throw new Error("down"); };
  const v = await preflight(" https://evil.example/pay", { fetchImpl: boom });
  assert.equal(v.host, "evil.example");   // was "https:" before the fix
});

// ---- F2 regression: an explicit confidence floor cannot be bypassed ---------

test("minConfidence fails a HEALTHY verdict with no confidence field", async () => {
  // A healthy unknown-seller response used to bypass the floor entirely
  // because the missing field skipped the check. The caller asked for a
  // minimum standard of evidence; "no confidence reported" meets none.
  const noConf = fakeFetch("green", {});   // healthy verdict, confidence absent
  await assert.rejects(
    () => assertPayable("seller.example", { fetchImpl: noConf, minConfidence: "verified" }),
    (e) => e instanceof PreflightAbort && /"unknown" is below required "verified"/.test(e.message));
  // and the same healthy response passes when no floor was requested
  const v = await assertPayable("seller.example", { fetchImpl: noConf });
  assert.equal(v.light, "green");
});

test("minConfidence with an unrecognized value is a caller error, not a silent pass", async () => {
  await assert.rejects(
    () => assertPayable("seller.example", { fetchImpl: fakeFetch("green"), minConfidence: "verifiedd" }),
    (e) => e instanceof TypeError && /minConfidence/.test(e.message));
});

test("minConfidence still fails open on a true outage (policy preserved)", async () => {
  const boom = async () => { throw new Error("down"); };
  const v = await assertPayable("seller.example", { fetchImpl: boom, minConfidence: "verified" });
  assert.equal(v.unavailable, true);   // unreachable oracle must never block
});
