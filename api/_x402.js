// The seller half of x402, written the way this site keeps telling other people
// to write it. Every rule here exists because we watched somebody break it and
// wrote a field note about it.
//
//  - The challenge goes in all three places. Measured 2026-08-08: 63% of sellers
//    put it only in a header, so a client parsing the body alone sees nothing and
//    reports the endpoint as broken. We publish body, `payment-required` header
//    and `WWW-Authenticate`, so any of the three parsers works.
//  - The quote says what it will actually charge. No surprise multiplier, no
//    minimum that only appears after payment.
//  - A wrong method gets a 405, never a 402. One seller charged $0.003 for a
//    GET to a POST-only route and returned its own homepage.
//  - Nothing is charged for an error. If we cannot serve the goods we do not
//    take the money, and we say so in the challenge.

export const NETWORK = "eip155:8453";                                    // Base
export const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";        // 6 decimals
export const PAY_TO = "0xC533Bf5268A2F64aDDe58dcE380651f70Aa92D7A";
export const SITE = "https://whatagentsbuy.com";

// Facilitators verify a signed EIP-3009 authorization and submit it. Base
// mainnet settlement needs CDP credentials; without them we can still issue a
// correct challenge, we just cannot accept the payment yet.
const FACILITATOR = process.env.X402_FACILITATOR || "";
const FACILITATOR_KEY = process.env.X402_FACILITATOR_KEY || "";
export const CAN_SETTLE = Boolean(FACILITATOR && FACILITATOR_KEY);

/** One priced option, shaped exactly like the ones we read out of the registry. */
export function accepts({ priceUsdc, resource, description, maxTimeoutSeconds = 300 }) {
  return {
    scheme: "exact",
    network: NETWORK,
    amount: String(Math.round(priceUsdc * 1e6)),
    asset: USDC,
    currency: USDC,
    payTo: PAY_TO,
    recipient: PAY_TO,
    resource,
    description,
    mimeType: "application/json",
    maxTimeoutSeconds,
    extra: { name: "USD Coin", version: "2", credentialTypes: ["authorization"] },
  };
}

/** 402 with the challenge in all three locations. */
export function challenge(res, opts) {
  const opt = accepts(opts);
  const body = {
    x402Version: 1,
    error: "payment required",
    accepts: [opt],
    // Said out loud because most sellers do not say it and buyers cannot tell.
    terms: {
      refund: "Nothing is charged for an error. A failed lookup returns 4xx and takes no payment.",
      freeAlternative: `${SITE}/api/ratings.json`,
      license: "CC BY 4.0, attribute What Agents Buy",
    },
  };
  const b64 = Buffer.from(JSON.stringify({ x402Version: 1, accepts: [opt] })).toString("base64");
  res.setHeader("payment-required", b64);
  res.setHeader(
    "WWW-Authenticate",
    `x402 network="${NETWORK}", asset="${USDC}", amount="${opt.amount}", payTo="${PAY_TO}"`
  );
  res.setHeader("content-type", "application/json");
  res.setHeader("cache-control", "no-store");
  return res.status(402).json(body);
}

/**
 * Verify and settle a presented payment.
 * Returns {ok, txHash, reason}. With no facilitator configured this returns
 * ok:false with a reason that says so, rather than pretending to settle.
 */
export async function settle(paymentHeader, opts) {
  if (!paymentHeader) return { ok: false, reason: "no payment presented" };
  if (!CAN_SETTLE) {
    return { ok: false, reason: "settlement not configured on this deployment" };
  }
  let payload;
  try {
    payload = JSON.parse(Buffer.from(paymentHeader, "base64").toString("utf8"));
  } catch {
    return { ok: false, reason: "X-PAYMENT header is not valid base64 JSON" };
  }
  const req = { x402Version: 1, paymentPayload: payload, paymentRequirements: accepts(opts) };
  const headers = { "content-type": "application/json", authorization: `Bearer ${FACILITATOR_KEY}` };
  try {
    const v = await fetch(`${FACILITATOR}/verify`, {
      method: "POST", headers, body: JSON.stringify(req),
    }).then((r) => r.json());
    if (!v?.isValid) return { ok: false, reason: v?.invalidReason || "verification failed" };
    const s = await fetch(`${FACILITATOR}/settle`, {
      method: "POST", headers, body: JSON.stringify(req),
    }).then((r) => r.json());
    if (!s?.success) return { ok: false, reason: s?.errorReason || "settlement failed" };
    return { ok: true, txHash: s.transaction || s.txHash || null };
  } catch (e) {
    return { ok: false, reason: `facilitator unreachable: ${e.message}` };
  }
}

/** Our own published data, read from the static site so there is one source. */
const cache = new Map();
export async function siteJson(path, ttlMs = 300000) {
  const hit = cache.get(path);
  if (hit && Date.now() - hit.at < ttlMs) return hit.data;
  const data = await fetch(`${SITE}${path}`, {
    headers: { "user-agent": "whatagentsbuy-x402/0.1" },
  }).then((r) => r.json());
  cache.set(path, { at: Date.now(), data });
  return data;
}

export function methodGuard(req, res, allowed) {
  if (allowed.includes(req.method)) return false;
  res.setHeader("allow", allowed.join(", "));
  res.status(405).json({
    error: "method not allowed",
    allowed,
    note: "A wrong method returns 405 here and is never charged.",
  });
  return true;
}
