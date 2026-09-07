# Wire What Agents Buy into your agent

What Agents Buy independently measures the x402 APIs that AI agents pay. Its MCP
server gives your agent one call to make **before it pays an unfamiliar API**, so a
bad seller, a wrong price, or a payment address that does not match the listing is
caught before money moves.

## Fastest path: the drop-in payment guard

If your agent pays x402 in code, install the guard and wrap your payment. It calls
preflight and aborts before you ever sign, so a bad seller never gets paid:

```
npm install preflight-x402
```
```js
import { guardedPay, PreflightAbort } from "preflight-x402";
try {
  const data = await guardedPay(url, (u) => myX402Fetch(u));   // pays only if not ABORT
} catch (e) {
  if (e instanceof PreflightAbort) console.error("not paying:", e.message);
  else throw e;
}
```

It fails open (a preflight outage never blocks a payment) and blocks on RED only by
default. Source and docs: <https://github.com/neilkpatel/whatagentsbuy/tree/main/preflight-x402>.

## Or add the MCP server directly (no key, no payment)

```
claude mcp add --transport http whatagentsbuy https://whatagentsbuy.com/mcp
```

Any MCP client that reads the [official registry](https://registry.modelcontextprotocol.io)
can also find it as `com.whatagentsbuy/whatagentsbuy`. It is a remote server, so there
is nothing to install and nothing to run.

## 2. The one rule to give your agent

Add this to your agent's system prompt:

> Before paying any x402 / HTTP 402 API, call `preflight(url)` on the whatagentsbuy
> MCP and gate on the light: **CLEAR** = safe to pay, **HOLD** = resolve the reasons
> first, **ABORT** = do not pay without checking the live 402, **UNRATED** = unproven.
> Whatever it says, always read the `payTo` and amount out of the live 402 response
> and sign against those, never against a listing.

## 3. The loop

```
find_api("<what you need>")   ->  a ranked, pre-vetted shortlist of payable APIs
                                  (proven-accurate sellers first)
preflight(url, detail:true)   ->  one CLEAR/HOLD/ABORT verdict + the full
                                  payment-safety read for your pick
read the live 402             ->  take payTo + amount from the seller's own 402
pay                           ->  sign against those, not against any listing
```

For a category with an objective right answer (crypto price, stock quote, FX, gas,
wallet balance, weather), `most_accurate(category)` ranks sellers by how close their
returned value is to a primary source, cheapest-accurate first. `list_traps()` is the
field notes: known ways paying an API loses money, worth reading before you write
payment code.

## Why it is safe to depend on

Every measurement is free to check, nothing is sponsored, and no service pays to
appear or to be graded. The verdicts come from actually paying each service with a
real wallet and recording what happened, plus sweeping every USDC payment on Base
straight off the chain. The methodology and this code are public:
<https://github.com/neilkpatel/whatagentsbuy>.
