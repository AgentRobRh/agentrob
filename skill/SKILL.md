# AgentRob ($ROB) — Agent Skill

A guide for an AI agent (e.g. the Binance Agentic Wallet / `baw`) to mint **AgentRob ($ROB)** on
**Robinhood Chain** for its operator. Minting is **agent-gated**: only a wallet whose operator holds
a verified **.ai Agent-ID** can mint. The flow is **non-custodial** — this skill never holds a key;
the operator's own wallet/agent signs and broadcasts. Nothing executes without the operator's
explicit confirmation.

> This is a third-party, experimental project document. It is **not** affiliated with Binance or
> Robinhood. Nothing here is financial advice. Crypto is risky — never spend what you can't afford
> to lose. Always verify every address on a block explorer before signing.

---

## Chain & assets

| | |
|---|---|
| Chain | **Robinhood Chain**, chainId **4663** (`0x1237`), gas in **ETH** |
| RPC | `https://rpc.mainnet.chain.robinhood.com` |
| Explorer | `https://robin.etherscan.io` |
| Settlement | priced in **USD**, paid in **ETH** (converted at mint time by a Chainlink ETH/USD oracle) |

## Contracts

| Role | Address |
|---|---|
| Mint (`RobMint`) | `0x6a0837e93ee51fb77425573cb42269298fc8fd3a` |
| Token (`$ROB`, `RobToken`) | `0x68b93afa31d59a0fddfac13576952ccc95f13df0` |
| Pool pair (self-deployed Uniswap V2) | `0x4b78ed4d04f58cdc1f6f3a02ae66df64b449e9b9` |
| Treasury | `0xd8d4ed318ad3a5709895ce9662c428ec2557dc0c` |
| **.ai Agent-ID registry (on BNB Chain 56)** | `0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F` |

The mint lives on Robinhood Chain; the **Agent-ID lives on BNB Chain**. The gate is an on-chain
read of the BNB registry for the operator's wallet. Always re-confirm these addresses against the
live site config (`GET /config`) and the token's on-chain `pair()` / `treasury()` before trusting them.

## Backend API (base `https://<site>`; signs the gate voucher, never holds keys)

- `GET  /api/status` → live mint snapshot: `sold`, `openAt`, `priceCents`, `opened`, `firstMintTime`, `refundWindow`, `ca`.
- `GET  /config` → `chainId`, `rpc`, `mintAddr`, `tokenAddr`, `agentRegistry`, signer addresses.
- `POST /gate/check` `{ wallet }` → `{ isAgent, tokenId }` — does this wallet hold a .ai Agent-ID.
- `POST /wallet/allowance` `{ wallet | agentId }` → `{ freeCap, freeUsed, paidCap, paidUsed, phase, maxShares }`.
- `POST /prepare/mint` `{ wallet, tokenId, shares }` → `{ sign_this: { to, data, value } }` — the mint
  calldata **with a signed EIP-712 gate voucher baked in**. The agent broadcasts this from the
  operator's wallet (`eth_sendTransaction`); `value` is the ETH cost in wei.
- `POST /prepare/social` `{ wallet, tokenId, task, proof? }` → `{ sign_this }` for `grantQuota` (+1 free, once per task).
- `POST /refund/status` `{ addr }` and `POST /prepare/refund` `{ wallet }` → refund read + `refund()` calldata.

## Mint flow (always preview, never auto-execute)

1. **Verify the chain & contracts.** Read `GET /config`; confirm chainId 4663 and that `mintAddr`
   matches the on-chain token's expectations. Confirm the addresses on `robin.etherscan.io`.
2. **Gate.** `POST /gate/check { wallet }`. If `isAgent` is false, STOP — the operator's wallet holds
   no .ai Agent-ID and cannot mint. Keep the returned `tokenId`.
3. **Allowance.** `POST /wallet/allowance { wallet, agentId: tokenId }` → how many free shares remain
   (base **1 per Agent-ID**, lifted by invites/tasks, on-chain bonus cap **90**) and the paid cap
   (**25 per wallet**). A single transaction mints at most 10 shares.
4. **Quote.** From `GET /api/status` read the current tier; the first **1,000 shares are free**
   ($0), then the price climbs a **247-tier ladder from $0.50 to $8.07** per share. Compute the ETH
   cost for the requested `shares`.
5. **Prepare.** `POST /prepare/mint { wallet, tokenId, shares }` → `{ sign_this: { to, data, value } }`.
6. **Preview to the operator**: wallet, Agent-ID, share count, free vs paid split, total ETH cost,
   remaining quota. **Wait for an explicit "yes".**
7. **Execute only on yes.** Broadcast `sign_this` via `eth_sendTransaction` from the operator's
   wallet. Never move funds or sign anything before the operator confirms.

## Rules (the mechanics, as code)

1. **Agent-gated.** Only a verified .ai Agent-ID can mint. Plain wallets revert at the door.
2. **Free then paid.** The first **1,000 shares are free**, **1 per Agent-ID**. Earn more free by
   inviting agents (who land their first mint) and clearing social tasks — **+1 each, capped at 90**.
   After the free pool, shares climb the **$0.50 → $8.07** USD ladder, **max 25 paid per wallet**.
3. **Each share** mints the operator **50,000 $ROB** and parks **40,000 $ROB** for the pool.
4. **The pool opens at share 10,000.** 80% of every paid coin seeds a self-deployed Uniswap-V2 pool
   and its **LP tokens are burned to the dead address** (liquidity can never be pulled); 20% goes to
   the treasury.
5. **Refundable.** If the pool has not opened within **14 days** of the first mint, every paid minter
   can call **`refund()`** and reclaim **100%** of the ETH they paid. Pausing cannot block a refund.
6. **Admin renounced** right after the pool is seeded — no pause, no reseed, no override. Verify
   `renounced() == true` on the mint contract.
7. **Trade tax** (after the pool opens): **3% buy** → treasury; **3% sell** = 1% treasury + **2%
   distributed to $ROB holders as ETH** (claimable). Transfer tax 0%.

## Claim ETH dividends (holders)

Holders of $ROB accrue ETH from the 2% sell tax. To claim:
- Read `dividendOf(holder)` / `withdrawableDividendOf(holder)` on the token, then call
  `withdrawDividend()` from the operator's wallet to pull the accrued ETH. Anyone may call
  `sweepDividends()` first to convert the pending tax into ETH before claiming.

## Invite & social tasks (earn extra free shares)

- Hand out the operator's invite link; each invited Agent-ID that lands its first mint adds +1 to the
  operator's free allowance.
- Social tasks (follow / post / comment on the pinned post) each grant +1 once: `POST /prepare/social
  { wallet, tokenId, task, proof }` → broadcast the returned `grantQuota` calldata. Capped at 90 bonus.
