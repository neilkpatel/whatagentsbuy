// preflight-x402: one call before your agent pays an x402 / HTTP 402 API.
//
// Asks whatagentsbuy.com for an independent CLEAR / HOLD / ABORT verdict on a
// seller, so a wrong price, a payTo that does not match the listing, a phantom
// paywall, or a seller caught underdelivering is caught BEFORE money moves. No
// key, no payment, no dependencies.
//
// Design choices that matter:
//  - Fail OPEN. If preflight is unreachable it returns UNRATED, it never blocks a
//    payment. A safety check that halts all spending when it is down is worse than
//    none. The always-rule is the backstop: read the payTo and amount out of the
//    live 402 and sign against THOSE, never a listing (including this one).
//  - Block on RED only, by default. A red light fires only from hard evidence
//    (payTo mismatch, phantom paywall, a reverified severe underdeliver), matching
//    the site. HOLD warns; you decide.

const DEFAULT_ENDPOINT = "https://whatagentsbuy.com/mcp";
const DEFAULT_UA = "preflight-x402/0.1";

export class PreflightAbort extends Error {
  constructor(verdict) {
    const why = (verdict.reasons || []).map((r) => r.text || r).join("; ")
      || "do not pay without checking the live 402";
    super(`preflight ABORT for ${verdict.host}: ${why}`);
    this.name = "PreflightAbort";
    this.verdict = verdict;
  }
}

/**
 * Fetch the pre-payment verdict for a URL or bare host.
 * Returns the verdict object: { host, verdict, light, gate, reasons, evidence, ... }.
 * Never throws on a network problem — returns an UNRATED verdict so a preflight
 * outage cannot block payments.
 */
export async function preflight(url, {
  endpoint = DEFAULT_ENDPOINT, detail = false, timeoutMs = 4000,
  client = DEFAULT_UA, fetchImpl = fetch,
} = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetchImpl(endpoint, {
      method: "POST",
      signal: ctrl.signal,
      headers: { "content-type": "application/json", "user-agent": client },
      body: JSON.stringify({
        jsonrpc: "2.0", id: 1, method: "tools/call",
        params: { name: "preflight", arguments: { url, detail } },
      }),
    });
    const j = await res.json();
    const v = j?.result?.structuredContent;
    if (!v || !v.light) throw new Error("no verdict in response");
    return v;
  } catch (e) {
    return {
      host: hostOf(url), light: "gray", verdict: "UNRATED", unavailable: true,
      gate: "preflight unavailable; read the live 402 and decide.",
      reasons: [], _error: String(e && e.message || e),
    };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Gate a payment on the verdict. Throws PreflightAbort when the light is in
 * `block` (default: ["red"]). Calls `onWarn` for lights in `warn` (default:
 * ["yellow"]). Returns the verdict otherwise. Whatever it returns, still read the
 * payTo and amount out of the live 402 and sign against those.
 */
export async function assertPayable(url, {
  block = ["red"], warn = ["yellow"], onWarn = defaultWarn, ...opts
} = {}) {
  const v = await preflight(url, opts);
  if (block.includes(v.light)) throw new PreflightAbort(v);
  if (warn.includes(v.light)) onWarn(v);
  return v;
}

/**
 * Drop-in wrapper: run your existing x402 payment function `pay` only after
 * preflight clears. On ABORT it throws before `pay` is ever called, so money
 * never moves.  const data = await guardedPay(url, (u) => agentcashFetch(u));
 */
export async function guardedPay(url, pay, opts = {}) {
  await assertPayable(url, opts);
  return pay(url);
}

function hostOf(u) {
  try { return new URL(/^https?:/i.test(u) ? u : "https://" + u).hostname.replace(/^www\./, ""); }
  catch { return String(u || ""); }
}
function defaultWarn(v) {
  try { console.warn(`[preflight] HOLD ${v.host}: ${(v.reasons || []).map((r) => r.text || r).join("; ")}`); }
  catch { /* logging is best-effort */ }
}
