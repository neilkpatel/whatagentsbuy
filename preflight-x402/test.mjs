// Tests for preflight-x402. Node's built-in runner, no deps:  node test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { preflight, assertPayable, guardedPay, PreflightAbort } from "./index.mjs";

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
