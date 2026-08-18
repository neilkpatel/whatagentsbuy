# preflight-x402

One call before your agent pays an x402 / HTTP 402 API. It asks
[whatagentsbuy.com](https://whatagentsbuy.com) for an independent **CLEAR / HOLD /
ABORT** verdict on the seller, so a wrong price, a `payTo` that does not match the
listing, a phantom paywall, or a seller caught underdelivering is caught **before
money moves**. No key, no payment, no dependencies.

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

Or just read the verdict and decide yourself:

```js
import { preflight } from "preflight-x402";
const v = await preflight(url, { detail: true });
```

## How it behaves (on purpose)

- **Fail open.** If preflight is unreachable it returns `UNRATED` and never blocks a
  payment. A safety check that halts all spending when it is down is worse than none.
- **Block on RED only, by default.** A red light fires only from hard evidence
  (payTo mismatch, phantom paywall, a reverified severe underdeliver). HOLD warns;
  you decide. Change with `block` / `warn` options.
- **The always-rule overrides everything.** Whatever the verdict says, read the
  `payTo` and amount out of the **live 402** and sign against those, never a listing
  (including this one).

## Options

```js
preflight(url, {
  endpoint: "https://whatagentsbuy.com/mcp",  // the MCP server
  detail: false,        // true adds the full payment-safety read
  timeoutMs: 4000,      // fail open after this
  client: "preflight-x402/0.1",  // sent as User-Agent, so usage is attributable
  fetchImpl: fetch,     // inject your own fetch for tests
});

assertPayable(url, { block: ["red"], warn: ["yellow"], onWarn: (v) => {...} });
```

## Why it is safe to depend on

Every measurement behind the verdict is free to check, nothing is sponsored, and no
service pays to appear or to be graded. Methodology and code:
<https://github.com/neilkpatel/whatagentsbuy>.

MIT.
