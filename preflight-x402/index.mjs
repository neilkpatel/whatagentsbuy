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
const DEFAULT_UA = "preflight-x402/0.2";

/**
 * One-line proof that this verdict is backed by real spend, not a probe of the
 * seller's paywall — the thing a probe-only checker cannot show. Returns "" when
 * the seller has never been paid.  e.g. "paid 8x over 4 days: 6 delivered, 2
 * graded accurate, 0 underdelivered (last: accurate)".
 */
export function paymentProof(verdict) {
  const h = verdict && verdict.payment_history;
  if (!h || !h.times_paid) return "";
  const span = h.span_days ? ` over ${h.span_days} day${h.span_days === 1 ? "" : "s"}` : "";
  const last = h.last_verdict ? ` (last: ${h.last_verdict})` : "";
  return `paid ${h.times_paid}x${span}: ${h.delivered} delivered, ${h.accurate} graded accurate, ` +
    `${h.disputed} underdelivered${last}`;
}

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
 * Thrown when the caller hands preflight something that is not a payment target.
 * This is a WIRING mistake in your code, surfaced immediately and loudly, and it
 * is deliberately NOT the same as a preflight outage: an outage fails open (your
 * payments proceed), but silently accepting a non-URL would leave you believing
 * a guard was running when it was not.
 */
export class PreflightInputError extends TypeError {
  constructor(msg, received) {
    super(msg);
    this.name = "PreflightInputError";
    this.received = received;
  }
}

// Event names seen in the wild when a caller wires this into an emitter instead
// of a fetch. Real hosts always contain a dot, so these are unambiguous.
const EVENTISH = new Set(["data", "end", "error", "close", "connect", "ready", "init",
  "message", "open", "request", "response", "finish", "drain", "abort", "test"]);

/**
 * Pull the payment target out of whatever the caller passed, accepting exactly
 * the shapes the fetch API itself accepts (string | URL | Request), and reject
 * anything that cannot be a seller. Returns a trimmed URL/host string.
 */
export function targetUrl(input) {
  let raw = null;
  if (typeof input === "string") raw = input;
  else if (input && typeof input.href === "string") raw = input.href;   // URL
  else if (input && typeof input.url === "string") raw = input.url;     // Request
  const shown = typeof input === "string" ? input : Object.prototype.toString.call(input);

  if (raw === null) {
    throw new PreflightInputError(
      `preflight: expected a URL string, a URL, or a Request, but received ${shown}. ` +
      "If you are wrapping a fetch, pass the function itself: " +
      "preflightFetch(fetch) — then call the wrapper the way you call fetch(url).",
      input);
  }
  const s = String(raw).trim();
  if (!s) {
    throw new PreflightInputError("preflight: received an empty URL.", input);
  }
  let host;
  try {
    host = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(s) ? s : "https://" + s).hostname.toLowerCase();
  } catch {
    throw new PreflightInputError(
      `preflight: ${JSON.stringify(s.slice(0, 80))} is not a URL or hostname.`, input);
  }
  const ok = host === "localhost" || host.endsWith(".localhost") || host.includes(".");
  if (!ok) {
    const hint = EVENTISH.has(host)
      ? ` "${host}" looks like an event name. preflightFetch wraps a FETCH function, not an ` +
        "event handler: const f = preflightFetch(fetch); await f('https://seller.example/api')."
      : " A seller host contains a dot, e.g. seller.example or https://seller.example/api.";
    throw new PreflightInputError(
      `preflight: ${JSON.stringify(s.slice(0, 80))} is not a payment target.${hint}`, input);
  }
  return s;
}

/**
 * Fetch the pre-payment verdict for a URL or bare host.
 * Returns the verdict object: { host, verdict, light, gate, reasons, evidence, ... }.
 * Never throws on a network problem — returns an UNRATED verdict so a preflight
 * outage cannot block payments. It DOES throw PreflightInputError when the target
 * itself is not a URL or host, because that is your bug, not our downtime, and
 * failing open on it would hand you an unguarded payment path with no signal.
 */
export async function preflight(url, {
  endpoint = DEFAULT_ENDPOINT, detail = false, timeoutMs = 4000,
  client = DEFAULT_UA, fetchImpl = fetch,
} = {}) {
  // Validate and normalize at the boundary. Two real failures drove this: a
  // whitespace-padded URL once parsed to host "https:" and downgraded a known
  // ABORT to UNRATED, and on 2026-09-05 an integration sent 79 calls whose
  // targets were "[object Object]" and Node event names ("data", "ready",
  // "connect"), every one answered UNRATED — a guard that was doing nothing
  // while looking like it worked.
  url = targetUrl(url);
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
const CONF_RANK = { unproven: 0, checked: 1, verified: 2 };

export async function assertPayable(url, {
  block = ["red"], warn = ["yellow"], onWarn = defaultWarn, minConfidence, ...opts
} = {}) {
  if (minConfidence !== undefined && !(minConfidence in CONF_RANK)) {
    throw new TypeError(`minConfidence must be one of ${Object.keys(CONF_RANK).join(", ")}, got ${JSON.stringify(minConfidence)}`);
  }
  const v = await preflight(url, opts);
  if (block.includes(v.light)) throw new PreflightAbort(v);
  // Opt-in: refuse sellers below a confidence floor ("verified" = we paid them,
  // "checked" = free probe only, "unproven" = listed). Off by default. Two
  // different kinds of "no confidence" get opposite treatment, deliberately:
  //  - a preflight OUTAGE (v.unavailable) keeps the documented fail-open
  //    policy: an unreachable oracle must never block payments;
  //  - a HEALTHY verdict whose confidence is missing or unrecognized FAILS the
  //    floor. The caller explicitly asked for a minimum standard of evidence,
  //    and "we don't know" meets no minimum. (This used to be skipped: a
  //    response without a confidence field bypassed the check entirely.)
  if (minConfidence && !v.unavailable) {
    const rank = CONF_RANK[v.confidence];
    if (rank === undefined || rank < CONF_RANK[minConfidence]) {
      throw new PreflightAbort({ ...v, reasons: [{ level: "red",
        text: `confidence "${v.confidence ?? "unknown"}" is below required "${minConfidence}": `
          + (v.confidence_basis || "not enough evidence to meet your bar") }] });
    }
  }
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

/**
 * Drop-in for ANY fetch-based x402 client (x402-fetch, agentcash, plain fetch):
 * wrap it once and every request is gated on preflight first. On ABORT it throws
 * PreflightAbort before your client ever pays. This is the one-line way to make
 * preflight the default on an existing payment stack:
 *
 *   import { wrapFetchWithPayment } from "x402-fetch";
 *   import { preflightFetch } from "preflight-x402";
 *   const fetch = preflightFetch(wrapFetchWithPayment(globalThis.fetch, wallet));
 *   // every fetch() now runs preflight, then pays only if the seller is not ABORT.
 *
 * Pass options straight through, e.g. { minConfidence: "verified" } to pay only
 * sellers a real wallet has already tested.
 */
export function preflightFetch(innerFetch, opts = {}) {
  if (typeof innerFetch !== "function") {
    throw new PreflightInputError(
      "preflightFetch(innerFetch): innerFetch must be the fetch function you want guarded, " +
      "e.g. preflightFetch(fetch) or preflightFetch(wrapFetchWithPayment(fetch, wallet)). " +
      `Received ${Object.prototype.toString.call(innerFetch)}.`, innerFetch);
  }
  return async function preflightedFetch(input, init) {
    // targetUrl accepts exactly what fetch accepts (string | URL | Request) and
    // throws PreflightInputError on anything else. The old form fell back to
    // String(input), which turned an object into the literal "[object Object]",
    // sent that as the seller, got UNRATED back, and passed the payment
    // through unguarded — a broken integration with no error to notice.
    const url = targetUrl(input);
    await assertPayable(url, opts);   // throws PreflightAbort on ABORT, warns on HOLD
    return innerFetch(input, init);
  };
}

function hostOf(u) {
  // Same boundary rule as the server's normHost: trim FIRST, then parse, so a
  // padded or oddly-cased URL still resolves to the real seller host.
  const s = String(u ?? "").trim();
  try {
    return new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(s) ? s : "https://" + s)
      .hostname.toLowerCase().replace(/^www\./, "");
  } catch { return s.toLowerCase(); }
}
function defaultWarn(v) {
  try {
    const proof = paymentProof(v);
    console.warn(`[preflight] HOLD ${v.host}: ${(v.reasons || []).map((r) => r.text || r).join("; ")}`
      + (proof ? ` [${proof}]` : ""));
  } catch { /* logging is best-effort */ }
}
