# What Agents Buy

Independent, measured reviews of the x402 APIs that AI agents pay for.

Live at **[whatagentsbuy.com](https://whatagentsbuy.com)** · MCP server at `https://whatagentsbuy.com/mcp` · listed in the [official MCP Registry](https://registry.modelcontextprotocol.io) as `com.whatagentsbuy/whatagentsbuy`.

Most numbers quoted about agentic payments come from registries and press releases, and they are wrong by orders of magnitude. This project measures the market directly. It pays x402 endpoints with a real wallet and publishes the receipt (what was quoted, what was actually charged, whether the goods arrived), sweeps every USDC payment agents make on Base straight off the chain, and grades the number a seller returns against a primary source that cannot be a reseller. Nothing is sponsored. No service pays to appear or to be graded.

This repository is the **open methodology**: the measurement pipeline and the agent-facing MCP server. The accumulated settlement history and the archive of paid responses stay private; everything they produce is published on the site.

## What it measures

- **Settlement.** USDC `Transfer` logs on Base, addressed to the payment wallet each service declares in its own x402 challenge. Measured from the chain, not from a registry's self-reported counters, which have been wrong by 30x. (`sweep.py`, `leaderboard.py`)
- **Delivery.** Every graded call is a paid call. A field counts as delivered only if it is actually present in the response, checked against the seller's own schema, and every short verdict is re-verified with a second independent call before it is recorded. (`conform.py`, `receipts.py`)
- **Accuracy.** For categories with an objective ground truth (crypto price, stock quote, FX rate, gas, wallet balance, weather), the number a seller returns is graded against a primary source that cannot be a reseller: an exchange median, a professional feed, the ECB, the chain itself. (`lab.py`, `categorize.py`)
- **Preflight.** One CLEAR / HOLD / ABORT verdict per seller, folding live price and payTo honesty versus the listing, phantom paywalls, and delivery receipts into a single signal an agent can gate a payment on. A red light fires only from hard evidence. (`preflight.py`)

Every harsh finding is verified before it is published. Several early accusations turned out to be measurement bugs in this code, so the pipeline runs known-good canaries before it grades anyone, treats "we could not measure it" as inconclusive rather than a negative verdict, and re-verifies every shortfall twice.

## The MCP server

`api/mcp.js` is a stateless, dependency-free MCP server (streamable HTTP, no auth, no payment) that exposes the measurements as tools an agent calls before it pays:

- `preflight(url)` — the one pre-payment verdict; `detail:true` adds the full payment-safety read.
- `find_api(task)` — discover a payable API for a task, proven-accurate sellers ranked first.
- `most_accurate(category)` — rank an objective category by how close each seller's returned value is to a primary source.
- `get_service`, `search_services`, `top_services`, `list_traps`, `market_summary`.

Add it with:

```
claude mcp add --transport http whatagentsbuy https://whatagentsbuy.com/mcp
```

## Pipeline

```
fetch_registry.py    # discover the payable endpoints
sweep.py             # Base USDC settlement tape (sweep_solana.py for Solana)
whatsnew.py          # registry diff, first-seen tracking
probe.py             # free directory probe: who answers, price/payTo honesty
conform.py           # the paid-call chokepoint: archive raw, judge, reconcile
lab.py               # daily accuracy corpus vs primary sources
receipts.py          # content-hashed, self-verifying dispute receipts
leaderboard.py       # settlement to a leaderboard + Organic Demand Score
preflight.py         # the pre-payment verdict engine
build.py             # everything to a static site
```

The `*_shootout.py` scripts buy the same thing from every seller in a category (email validation, gas, WHOIS, price, weather) and publish the contrast.

## Tests

```
python3 tests.py            # pipeline invariants, fixture-based, no network
node --test api/*.test.js   # the MCP server
```

Both run in CI on every push, and `tests.py` runs at three gates locally: before every build, in the pre-commit hook, and before any script spends money.

## Author

Neil K. Patel — [neilkpatel.com](https://www.neilkpatel.com/) · [LinkedIn](https://www.linkedin.com/in/neilkiranpatel/)

Feedback and corrections are welcome on LinkedIn.

## License

The measurements published on the site are CC BY 4.0 (attribute What Agents Buy). This code is shared for transparency and independent review of the methodology.
