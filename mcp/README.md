# AgentRob MCP Server

A minimal [Model Context Protocol](https://modelcontextprotocol.io) server named **`agentrob`**.
It exposes thin tools over the AgentRob ($ROB) fair-mint backend API so that an MCP-capable agent
can read mint progress and prepare mints, task claims, dividend claims, and refunds.

The server holds **no keys** and signs/broadcasts **nothing**. Every tool is a single HTTP call to
the backend; the backend signs EIP-712 vouchers and the user's own agentic wallet broadcasts.

## Install

```bash
cd mcp
npm i
```

Requires Node.js >= 18 (uses the built-in global `fetch`).

## Configuration

| Env var        | Default                 | Description                      |
| -------------- | ----------------------- | -------------------------------- |
| `AGENTROB_API` | `https://agentrob.live` | Base URL of the AgentRob backend |

## Tools

| Tool                  | Backend call               | Purpose                                              |
| --------------------- | -------------------------- | ---------------------------------------------------- |
| `rob_status`          | `GET /api/status`          | Mint progress: totalMinted, shares, price, opened.   |
| `rob_config`          | `GET /config`              | Addresses, chain id, public signer addresses.        |
| `rob_quote_mint`      | `POST /prepare/mint`       | Signed voucher + tx to mint $ROB shares.             |
| `rob_verify_task`     | `POST /verify/social`      | Verify a follow / post / comment task.               |
| `rob_claim_task`      | `POST /prepare/social`     | Claim verified task as extra quota (voucher).        |
| `rob_dividend_status` | `POST /dividend/status`    | Claimable ETH dividend status for a holder.          |
| `rob_prepare_claim`   | `POST /prepare/claim`      | Prepare an ETH dividend claim tx.                    |
| `rob_refund_status`   | `GET /refund/status`       | 14-day 100% refund eligibility for a paid minter.    |

Each tool returns the backend's JSON response as text (prefixed with the request line and status).

## Add to an MCP client

Example client config (Claude Desktop / any MCP client that launches servers over stdio):

```json
{
  "mcpServers": {
    "agentrob": {
      "command": "node",
      "args": ["/absolute/path/to/agentrob-public/mcp/index.js"],
      "env": {
        "AGENTROB_API": "https://agentrob.live"
      }
    }
  }
}
```

After `npm i` you can also run it directly for a smoke test:

```bash
AGENTROB_API=https://agentrob.live node index.js
```

It speaks MCP over stdio and logs a single startup line to stderr.
