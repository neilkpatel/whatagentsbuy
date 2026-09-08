// Tests for preflight-x402. Node's built-in runner, no deps:  node test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { preflight, assertPayable, guardedPay, preflightFetch, PreflightAbort, paymentProof,
         PreflightInputError, targetUrl } from "./index.mjs";

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

// ---------------------------------------------------------------------------
// Input validation. Driven by real production traffic: on 2026-09-05 an
// external integration sent 79 calls in under a second whose targets were
// "[object Object]" (32) and Node event names ("data", "test", "event",
// "message", "ready", "request", "init", "connect"). Every one came back
// UNRATED, which is fail-open, so that integration was paying with NO guard
// running and nothing told them. These lock the fix.
// ---------------------------------------------------------------------------

test("REGRESSION: an object no longer becomes the literal seller '[object Object]'", async () => {
  let reached = false;
  const f = preflightFetch(async () => { reached = true; }, { fetchImpl: fakeFetch("green") });
  await assert.rejects(() => f({ method: "POST" }),        // no .url / .href
    (e) => e instanceof PreflightInputError && /expected a URL string/.test(e.message));
  assert.equal(reached, false, "the inner fetch must NOT run on a wiring error");
});

test("REGRESSION: event names are rejected with wiring guidance, not answered UNRATED", async () => {
  for (const ev of ["data", "ready", "connect", "message", "init", "request", "test"]) {
    await assert.rejects(() => preflight(ev, { fetchImpl: fakeFetch("green") }),
      (e) => e instanceof PreflightInputError && /event name/.test(e.message),
      `"${ev}" should be rejected with an event-name hint`);
  }
});

test("PROPERTY: no non-URL input can reach the network, whatever it is", async () => {
  // This is the test that would have caught the bug before release: exercise the
  // public API with the WRONG TYPES, not just well-formed strings. Every prior
  // test passed a valid host, so the String(input) fallback was never executed.
  const junk = [undefined, null, 0, 1, NaN, true, false, {}, { a: 1 }, [], [1, 2],
                () => {}, Symbol("s"), new Map(), new Set(), "", "   ", "\n",
                "nodot", "[object Object]", "https://", "://x", "?", "/path/only"];
  for (const bad of junk) {
    let sent = null;
    const spy = async (_e, init) => {
      sent = JSON.parse(init.body).params.arguments.url;
      return { json: async () => ({ result: { structuredContent: { host: "x", light: "green" } } }) };
    };
    await assert.rejects(() => preflight(bad, { fetchImpl: spy }),
      (e) => e instanceof PreflightInputError,
      `preflight(${String(bad)}) must throw, not query`);
    assert.equal(sent, null, `nothing should have been sent for ${String(bad)}`);
  }
});

test("CONTRACT: whatever we do send is always a real host", async () => {
  // The other half: for every VALID shape, assert the string on the wire parses
  // to a hostname with a dot. A guard that sends garbage is not a guard.
  const good = ["seller.example", "https://seller.example/api?q=1", " seller.example ",
                "HTTPS://WWW.Seller.Example/x", new URL("https://seller.example/pay"),
                { url: "https://seller.example/pay" }];
  for (const g of good) {
    let sent = null;
    const spy = async (_e, init) => {
      sent = JSON.parse(init.body).params.arguments.url;
      return { json: async () => ({ result: { structuredContent: { host: "seller.example", light: "green" } } }) };
    };
    const f = preflightFetch(async () => "ok", { fetchImpl: spy });
    await f(g);
    const host = new URL(/^[a-z]+:\/\//i.test(sent) ? sent : "https://" + sent).hostname;
    assert.ok(host.includes("."), `sent ${JSON.stringify(sent)} -> host ${host} has no dot`);
  }
});

test("targetUrl accepts the fetch signature and localhost, rejects bare words", () => {
  assert.equal(targetUrl("seller.example"), "seller.example");
  assert.equal(targetUrl(new URL("https://seller.example/x")), "https://seller.example/x");
  assert.equal(targetUrl({ url: "https://seller.example/x" }), "https://seller.example/x");
  assert.equal(targetUrl("  https://seller.example/x  "), "https://seller.example/x");
  assert.equal(targetUrl("localhost:8080"), "localhost:8080");   // local dev is legitimate
  assert.throws(() => targetUrl("banana"), PreflightInputError);
});

test("a wiring error is NOT the fail-open path (the distinction that matters)", async () => {
  const boom = async () => { throw new Error("network down"); };
  // outage on a REAL target: still fails open, payments proceed
  const v = await assertPayable("seller.example", { fetchImpl: boom });
  assert.equal(v.light, "gray");
  // garbage target: throws even though the network is also down, because the
  // caller's bug must never be disguised as our outage
  await assert.rejects(() => assertPayable({}, { fetchImpl: boom }),
    (e) => e instanceof PreflightInputError);
});

test("preflightFetch(nonFunction) explains the correct wiring", () => {
  assert.throws(() => preflightFetch({}), (e) =>
    e instanceof PreflightInputError && /preflightFetch\(fetch\)/.test(e.message));
});
