# preflight-x402

One call before your agent pays an x402 / HTTP 402 API. It asks
[whatagentsbuy.com](https://whatagentsbuy.com) for an independent **CLEAR / HOLD /
ABORT** verdict on the seller, so a wrong price, a `payTo` that does not match the
listing, a phantom paywall, or a seller caught underdelivering is caught **before
money moves**. No key, no payment, no dependencies.

**Backed by real spend, not a probe.** Other safety checks inspect the seller's
402 challenge and guess. This verdict comes from a wallet that has actually **paid
the seller** and recorded what came back, so it can tell you *"paid this seller 8
times over 4 days: 6 delivered in full, 2 graded accurate against a primary
source"* — a claim a probe-only checker cannot make. Read it with `paymentProof(v)`.

```
npm install preflight-x402
```

## Use it

Wrap your existing payment function. On ABORT it throws before you ever pay:

```js
import { guardedPay, PreflightAbort } from "preflight-x402";

try {
  const data = await guardedPay(url, (u) => myX402Fetch(u));  // pays only if not ABORT
} catch (e) {
  if (e instanceof PreflightAbort) {
    console.error("not paying:", e.message);   // e.verdict has the full reasons
  } else throw e;
}
```

Or gate manually:

```js
import { assertPayable } from "preflight-x402";

const v = await assertPayable(url);   // throws on ABORT, warns on HOLD
// v.light: "green" (CLEAR) | "yellow" (HOLD) | "red" (ABORT) | "gray" (UNRATED)
// ... now read the payTo and amount out of the LIVE 402 and sign against those.
```

### Drop it into an existing x402 stack (one line)

Already paying with `x402-fetch`, `agentcash`, or any fetch-based client? Wrap it
once and every request is gated on preflight first, no other code changes:

```js
import { wrapFetchWithPayment } from "x402-fetch";
import { preflightFetch } from "preflight-x402";

// your normal paying fetch, now with a preflight gate in front of it
const fetch = preflightFetch(wrapFetchWithPayment(globalThis.fetch, wallet));

await fetch("https://seller.example/paid");  // preflight -> pay only if not ABORT
```

Works the same around agentcash or a plain fetch. Pass options through, e.g.
`preflightFetch(payingFetch, { minConfidence: "verified" })` to pay only sellers a
real wallet has already tested.

### Gate on confidence, not just the light

Every verdict carries `confidence` — how much it is backed by, separate from the
light: **`verified`** (a real wallet has paid this seller), **`checked`** (the free
live probe passed but no money has moved), **`unproven`** (listed, not yet checked).
A CLEAR you can lean on is a *verified* one. Require a floor when the payment is
big enough to matter:

```js
// only pay sellers we have actually paid before; a probe-only CLEAR is refused
await assertPayable(url, { minConfidence: "verified" });
```

It stays fail-open: if preflight is unreachable there is no confidence to judge, so
`minConfidence` never blocks on a missing verdict.

Or just read the verdict and decide yourself:

```js
import { preflight, paymentProof } from "preflight-x402";
const v = await preflight(url, { detail: true });

// The differentiator: real payment history to this exact seller.
console.log(paymentProof(v));   // "" if never paid, so it never fabricates a claim
// -> "paid 8x over 4 days: 6 delivered, 2 graded accurate, 0 underdelivered (last: accurate)"
v.payment_history;              // { times_paid, span_days, delivered, accurate, disputed, last_verdict }
```

## How it behaves (on purpose)

- **Fail open.** If preflight is unreachable it returns `UNRATED` and never blocks a
  payment. A safety check that halts all spending when it is down is worse than none.
- **Block on RED only, by default.** A red light fires only from hard evidence
  (payTo mismatch, phantom paywall, a reverified severe underdeliver). HOLD warns;
  you decide. Change with `block` / `warn` options.
- **A wiring mistake is loud; an outage is quiet.** These are different failures and
  get opposite treatment. If preflight is unreachable, you get `UNRATED` and your
  payment proceeds. If you hand it something that is *not a payment target* (an
  object, an event name, an empty string) it throws `PreflightInputError`
  immediately, because silently accepting it would leave you believing a guard was
  running when nothing was checked. Real traffic on 2026-09-05 did exactly this: 79
  calls whose target was `"[object Object]"` or a Node event name, all answered
  `UNRATED`, all unguarded, with nothing to notice.
- **The always-rule overrides everything.** Whatever the verdict says, read the
  `payTo` and amount out of the **live 402** and sign against those, never a listing
  (including this one).

### Wiring it correctly

`preflightFetch` wraps **a fetch function** and returns a fetch-shaped function. It is
not an event handler and not a middleware factory.

```js
// right: wrap the fetch, call the wrapper like fetch
const f = preflightFetch(fetch);                       // or your paying fetch
await f("https://seller.example/api");                 // string | URL | Request

// wrong: passing a non-target — throws PreflightInputError with the fix in the message
await f({ method: "POST" });                           // -> "expected a URL string..."
emitter.on("data", f);                                 // -> "\"data\" looks like an event name"
```

## Options

```js
preflight(url, {
  endpoint: "https://whatagentsbuy.com/mcp",  // the MCP server
  detail: false,        // true adds the full payment-safety read
  timeoutMs: 4000,      // fail open after this
  client: "preflight-x402/0.2",  // sent as User-Agent, so usage is attributable
  fetchImpl: fetch,     // inject your own fetch for tests
});

assertPayable(url, { block: ["red"], warn: ["yellow"], onWarn: (v) => {...} });
```

## Why it is safe to depend on

Every measurement behind the verdict is free to check, nothing is sponsored, and no
service pays to appear or to be graded. Methodology and code:
<https://github.com/neilkpatel/whatagentsbuy>.

MIT.
