#!/usr/bin/env node
/**
 * AgentRob ($ROB) MCP server.
 *
 * Exposes a thin set of tools over the AgentRob fair-mint backend API. Each tool performs a
 * single HTTP call to the backend and returns the raw JSON response as text. The server holds
 * no keys and never signs or broadcasts anything itself — the backend signs EIP-712 vouchers,
 * and the user's own agentic wallet broadcasts. This server is pure read/prepare glue.
 *
 * Transport: stdio.
 * Backend base URL: env AGENTROB_API, default https://agentrob.live
 */
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const API_BASE = (process.env.AGENTROB_API || "https://agentrob.live").replace(/\/+$/, "");

/**
 * Call the backend and return an MCP tool result wrapping the response body.
 * Always returns text content; never throws out of a tool handler.
 */
async function call(method, path, { query, body } = {}) {
  let url = `${API_BASE}${path}`;
  if (query) {
    const qs = new URLSearchParams(
      Object.entries(query).filter(([, v]) => v !== undefined && v !== null)
    ).toString();
    if (qs) url += `?${qs}`;
  }

  const init = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  try {
    const res = await fetch(url, init);
    const text = await res.text();
    let payload = text;
    try {
      payload = JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      /* non-JSON body — return as-is */
    }
    const header = `${method} ${url} -> ${res.status} ${res.statusText}`;
    return {
      isError: !res.ok,
      content: [{ type: "text", text: `${header}\n\n${payload}` }],
    };
  } catch (err) {
    return {
      isError: true,
      content: [{ type: "text", text: `Request to ${method} ${url} failed: ${err?.message || err}` }],
    };
  }
}

const server = new McpServer({ name: "agentrob", version: "0.1.0" });

server.tool(
  "rob_status",
  "Mint progress for $ROB: totalMinted, shares, current price, and whether the pool has opened.",
  {},
  async () => call("GET", "/api/status")
);

server.tool(
  "rob_config",
  "AgentRob deployment config: contract addresses, chain id, and public signer addresses.",
  {},
  async () => call("GET", "/config")
);

server.tool(
  "rob_quote_mint",
  "Prepare a $ROB mint: returns the signed EIP-712 voucher and the transaction for the wallet to sign/broadcast.",
  {
    wallet: z.string().describe("The minter wallet address (0x...)."),
    tokenId: z.union([z.string(), z.number()]).describe("The .ai Agent-ID token id held by the wallet."),
    shares: z.union([z.string(), z.number()]).describe("Number of shares to mint."),
  },
  async ({ wallet, tokenId, shares }) =>
    call("POST", "/prepare/mint", { body: { wallet, tokenId, shares } })
);

server.tool(
  "rob_verify_task",
  "Verify a social task (follow / post / comment) for free-share bonus eligibility.",
  {
    task: z.enum(["follow", "post", "comment"]).describe("The social task type."),
    proof: z.string().optional().describe("Proof URL / post id for the task."),
    handle: z.string().optional().describe("The social handle used for the task."),
  },
  async ({ task, proof, handle }) =>
    call("POST", "/verify/social", { body: { task, proof, handle } })
);

server.tool(
  "rob_claim_task",
  "Claim a verified social/invite task as extra free-mint quota: returns a signed Quota voucher.",
  {
    wallet: z.string().describe("The minter wallet address (0x...)."),
    tokenId: z.union([z.string(), z.number()]).describe("The .ai Agent-ID token id."),
    task: z.string().describe("The task being claimed (follow / post / comment / invite)."),
    proof: z.string().optional().describe("Proof URL / post id / invite reference."),
  },
  async ({ wallet, tokenId, task, proof }) =>
    call("POST", "/prepare/social", { body: { wallet, tokenId, task, proof } })
);

server.tool(
  "rob_dividend_status",
  "Claimable ETH dividend status for a $ROB holder wallet.",
  {
    wallet: z.string().describe("The holder wallet address (0x...)."),
  },
  async ({ wallet }) => call("POST", "/dividend/status", { body: { wallet } })
);

server.tool(
  "rob_prepare_claim",
  "Prepare an ETH dividend claim: returns the transaction for the wallet to sign/broadcast.",
  {
    wallet: z.string().describe("The holder wallet address (0x...)."),
  },
  async ({ wallet }) => call("POST", "/prepare/claim", { body: { wallet } })
);

server.tool(
  "rob_refund_status",
  "Refund eligibility for a paid minter (100% refund if the pool never opened within 14 days).",
  {
    wallet: z.string().describe("The paid-minter wallet address (0x...)."),
  },
  async ({ wallet }) => call("POST", "/refund/status", { body: { wallet } })
);

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  // Log to stderr only — stdout is the MCP transport channel.
  console.error(`agentrob MCP server running (backend: ${API_BASE})`);
}

main().catch((err) => {
  console.error("Fatal error starting agentrob MCP server:", err);
  process.exit(1);
});
