// GET /x402/service?host=blockrun.ai
//
// One question, answered in one call: what do we know about this seller? Joins
// the grade we earned by paying, the demand shape from the settlement tape, and
// the Organic Demand Score, which otherwise means fetching three files and
// matching them up yourself.
//
// The free bulk files stay free and the 402 challenge names them, because this
// site has a field note about sellers who charge for what they publish for
// nothing on another path, and it would be absurd to commit that here.

import { challenge, settle, siteJson, methodGuard, CAN_SETTLE, PAY_TO } from "../_x402.js";

const PRICE = 0.001;

export default async function handler(req, res) {
  if (methodGuard(req, res, ["GET", "HEAD"])) return;

  const url = new URL(req.url, "https://whatagentsbuy.com");
  const host = (url.searchParams.get("host") || "").trim().toLowerCase();
  const resource = "https://whatagentsbuy.com/x402/service";
  const opts = {
    priceUsdc: PRICE,
    resource,
    description: "Grade, demand shape and Organic Demand Score for one x402 seller, by host.",
  };

  // Nothing is charged for a bad request. Validate before quoting a price.
  if (!host) {
    return res.status(400).json({
      error: "missing host",
      usage: `${resource}?host=blockrun.ai`,
      note: "Bad requests return 400 and are never charged.",
    });
  }

  const paid = await settle(req.headers["x-payment"], opts);
  const forcing = url.searchParams.get("preview402") === "1";

  // While settlement is unconfigured we do not advertise a price we cannot
  // collect. Quoting for money that cannot be taken is exactly the failure this
  // site grades other people down for.
  if (forcing || (CAN_SETTLE && !paid.ok)) {
    if (paid.reason && paid.reason !== "no payment presented") {
      res.setHeader("x-payment-error", paid.reason);
    }
    return challenge(res, opts);
  }

  let ratings, board;
  try {
    [ratings, board] = await Promise.all([
      siteJson("/api/ratings.json"),
      siteJson("/api/leaderboard.json"),
    ]);
  } catch (e) {
    // We failed, so we keep nothing. If a payment settled we say so plainly
    // rather than quietly pocketing it.
    return res.status(502).json({
      error: "could not read our own published data",
      detail: e.message,
      charged: paid.ok ? "yes, and this is our fault: contact @crowdturtle for a refund" : "no",
    });
  }

  // ratings.json keys the host as `service`; leaderboard.json calls it `host`.
  const rows = (ratings.ratings || []).filter(
    (r) => (r.service || "").toLowerCase() === host
  );
  const lb = (board.rows || []).find((r) => (r.host || "").toLowerCase() === host) || null;

  if (!rows.length && !lb) {
    return res.status(404).json({
      error: "not found",
      host,
      note: "We have neither bought from nor measured settlement for this host. Not charged.",
      free: "https://whatagentsbuy.com/api/ratings.json",
    });
  }

  res.setHeader("cache-control", "public, max-age=300");
  if (paid.ok && paid.txHash) {
    res.setHeader("x-payment-response", Buffer.from(JSON.stringify({
      success: true, transaction: paid.txHash, payer: PAY_TO,
    })).toString("base64"));
  }
  return res.status(200).json({
    host,
    grades: rows.map((r) => ({
      grade: r.grade, behaviour: r.graded_on, verdict: r.verdict || null,
      quoted: r.quoted || null, charged: r.charged || null,
      delivered: r.delivered, free_to_call: r.free_to_call,
      date: r.date, title: r.title, url: r.url || null,
    })),
    settlement: lb && {
      usdc_received: lb.usdc_received, settlements: lb.settlements,
      paying_wallets: lb.paying_wallets, repeat_buyers: lb.repeat_buyers,
      sends_back_pct: lb.sends_back_pct,
      demand: lb.demand,
      organic_demand_score: lb.organic_demand_score,
      organic_demand_parts: lb.organic_demand_parts,
      organic_demand_confidence: lb.organic_demand_confidence,
    },
    caveats: [
      "Grades describe one named behaviour, not a vendor. A service can hold two.",
      "Organic Demand Score measures shape, not honesty. Read it beside sends_back_pct.",
      "Settlement is USDC landing on Base at the payTo the seller advertises, swept daily.",
    ],
    as_of: board.as_of || null,
    paid: paid.ok ? { settled: true, tx: paid.txHash } : { settled: false, price_usdc: PRICE,
      note: CAN_SETTLE ? "unpaid" : "free while settlement is being configured" },
    license: "CC BY 4.0, attribute What Agents Buy",
    free_bulk: "https://whatagentsbuy.com/api/ratings.json",
  });
}
