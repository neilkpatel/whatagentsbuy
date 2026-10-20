# Bounty Objective: Seller submission: money-reactor.makar52nn2016.workers.dev (22 x402 v1+v2 routes on Base, USDC 0.001-0.05)
Target: neilkpatel/whatagentsbuy Issue #6
URL: https://github.com/neilkpatel/whatagentsbuy/issues/6

## Issue Description
Seller: https://money-reactor.makar52nn2016.workers.dev (Cloudflare Workers, x402 v1 AND v2 dual-challenge, CDP facilitator, Bazaar extension on every route)
payTo: 0xf47b7e5912Fd4218aC4c992Cdc6FAE04e1Ff512E (eip155:8453, USDC)
Catalog: https://money-reactor.makar52nn2016.workers.dev/.well-known/x402 , https://money-reactor.makar52nn2016.workers.dev/openapi.json (x402scan-compatible, x-payment-info per op)
MCP (x402 over MCP): https://money-reactor.makar52nn2016.workers.dev/mcp — JSON-RPC 2.0, free tools reactor_status / get_bounty_demo
Free samples: https://money-reactor.makar52nn2016.workers.dev/api/health , https://money-reactor.makar52nn2016.workers.dev/api/bounties/demo , https://money-reactor.makar52nn2016.workers.dev/api/status (public settlement ledger with tx receipts)

Routes (price in USDC):
- GET /api/bounties 0.001 (multi-source funded-bounty feed: Superteam Earn + live GitHub bounty radar, trust-scored)
- GET /api/honeypot?repo=owner/name 0.001 (0-100 repo trust score + GO/CAUTION/HONEYPOT_RISK + reasons)
- GET /api/verdict?repo=owner/name 0.001 (one-line repo safety verdict)
- GET /api/crypto-price?symbol=BTC-USD 0.001 (live USD spot, Coinbase public source, edge-cached 30s)
- GET /api/usd-price?symbols=... 0.001 (multi-asset batch snapshot, max 25)
- GET /api/fear-greed 0.001 (Fear & Greed index + 10-day history)
- GET /api/market-pulse 0.003 (batch prices + sentiment + Base block in one poll)
- POST /api/ai-chat 0.002 (Llama 3.1 8B completion, GET ?q= one-shots)
- GET /api/url-to-text?url=... 0.003 (any public page as clean plain text, up to 8KB)
- GET /api/crypto-news 0.001 (top headlines with source links)
- GET /api/weather?city= 0.002 (current weather anywhere)
- GET /api/token-verdict?address= 0.01 (onchain token safety verdict on Base)
- GET /api/erc20-balance?contract=&address= 0.001 (on-chain ERC20 balance on Base)
- GET /api/block-number 0.001 (latest Base block)
- GET /api/base-gas 0.001 (gas price, EIP-1559 base fee, priority fee in Gwei)
- GET /api/wallet-profile?address= 0.003 (ETH balance, tx count, USDC balance, activity tier — counterparty pre-flight)
- GET /api/coin-convert?from=&to=&amount= 0.002 (cross-asset conversion at Coinbase USD spot)
- GET /api/btc-halving 0.001 (halving countdown: height, blocks left, ETA, reward)
- GET /api/uuid 0.001 · GET /api/time 0.001
- GET /api/starter-kit 0.02 (onboarding bundle: quickstart + catalog + live snapshot + trust proof)
- GET /api/dossier 0.05 (one-call research bundle: prices + sentiment + news + chain tip + AI summary)

Your gradable categories with primary-source ground truth: crypto price (usd-price, coin-convert), gas (base-gas), wallet balance (erc20-balance, wallet-profile), weather. Prices in challenges always match openapi.json; every settlement is on-chain to payTo above; 70+ settled payments with tx receipts in the public /api/status ledger. SDKs: pypi 'x402-money-reactor', npm 'x402-money-reactor'. Docs for agents: /llms-full.txt (complete integration guide with copy-paste snippets).

## Instructions for Kuro (Autonomous Solver):
1. Use skills: `systematic-debugging` and `code-review-and-quality`.
2. Inspect the repository structure, locate relevant files, and understand the bug/feature.
3. Apply the minimal, robust fix or implementation.
4. Run any existing test suites or linters in the workspace.
5. Ensure the fix adheres to the project's existing code style.
6. When done, output a summary of files modified and verification results.
