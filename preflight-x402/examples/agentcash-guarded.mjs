// Dogfood: preflight before agentcash pays. This is the reference integration
// that seeds real, attributed usage of the MCP (client "agentcash-guarded").
//
//   node examples/agentcash-guarded.mjs <url>
//
// Flow: ask preflight for a verdict, ABORT stops before any spend, otherwise hand
// off to your real agentcash payment. Always read the payTo + amount out of the
// live 402 and sign against those.
import { assertPayable, PreflightAbort } from "../index.mjs";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const run = promisify(execFile);
const url = process.argv[2] || "https://blockrun.ai/api/v1/exa/search";
const LIVE = process.env.AGENTCASH_LIVE === "1";   // set to actually spend

try {
  // A distinctive client so this shows up as real agent usage, not tooling.
  const v = await assertPayable(url, { client: "agentcash-guarded/0.1", detail: true });
  console.log(`preflight: ${v.verdict} (${v.light}) for ${v.host}`);
  if (v.reasons?.length) console.log("  reasons:", v.reasons.map((r) => r.text || r).join("; "));

  if (!LIVE) {
    console.log("\nDRY RUN. Verdict is not ABORT, so a payment WOULD proceed.");
    console.log("Set AGENTCASH_LIVE=1 to actually pay via agentcash.");
    process.exit(0);
  }

  // Real payment. agentcash reads the live 402, signs EIP-3009 off-chain, and a
  // facilitator settles. Swap this for your agentcash call (CLI or MCP).
  console.log("\nPaying via agentcash...");
  const { stdout } = await run("npx", ["-y", "agentcash@latest", "fetch", url], { timeout: 60000 });
  console.log(stdout);
} catch (e) {
  if (e instanceof PreflightAbort) {
    console.error("ABORTED, not paying:", e.message);
    process.exit(1);
  }
  throw e;
}
