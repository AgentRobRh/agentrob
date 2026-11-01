# AgentRob Backend — $ROB voucher signer

A small Flask service that authorizes agent-gated mints of **$ROB** on **Robinhood Chain
(chainId 4663)**. It is **non-custodial**: it only signs EIP-712 vouchers and builds the exact
transaction for a wallet to sign and submit itself. It never holds funds and never broadcasts.

The on-chain contract it serves is `../src/RobMint.sol`. The EIP-712 domain, type hashes, and
`abi.encode` layout here match that contract byte for byte (proven by `self_test.py`).

## How it works

1. A wallet asks to mint and names the **Agent-ID `tokenId`** it holds.
2. The backend verifies `ownerOf(tokenId) == wallet` on the **BNB Chain** Agent-ID registry
   (SPACE ID ".ai Agent ID" ERC721). This **binds quota to the specific NFT**: `agentId = tokenId`,
   so the on-chain quota ledger (keyed by `agentId`) travels with the token. Moving the NFT to a
   fresh wallet does **not** reset quota — the old per-wallet `keccak(wallet)` fallback is gone.
3. If eligible, the backend picks a random nonce, sets a short deadline, and signs a `Mint` voucher
   with the **mint/gate-signer** key.
4. It returns the voucher plus `sign_this` (`to` / `data` / `value`) and a computed `minTokensOut`
   for the wallet to sign.

If `tokenId` is missing or not owned by the wallet, the mint is refused:
`no Agent-ID on BNB Chain — agent-gated, refused` (403). A flaky BNB node is a retryable 502.

> **Cross-chain identity assumption.** The gate assumes the **same address** controls the wallet on
> both BNB and Robinhood Chain. That holds for **EOAs and EIP-7702** accounts. It does **not**
> necessarily hold for smart-contract wallets, whose address can be controlled by different owners
> on different chains — so only EOA / EIP-7702 wallets are safely supported by this gate.

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask server + endpoints + Agent-ID gate + on-chain reads |
| `signer.py` | EIP-712 voucher signer (Mint + Quota). Builds the digest by hand to match the contract |
| `calldata.py` | Pure ABI calldata encoders (no keys, no network) |
| `self_test.py` | Proves the EIP-712 is correct (sign/recover + 3 independent digest checks) |
| `requirements.txt` | Python dependencies |

## EIP-712 (matches `RobMint.sol`)

```
Domain:          name="AgentRobMint", version="1", chainId=4663, verifyingContract=<ROB_MINT_ADDR>
DOMAIN_TYPEHASH  keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
MINT_TYPEHASH    keccak256("Mint(address minter,uint256 agentId,uint256 nonce,uint256 deadline)")
QUOTA_TYPEHASH   keccak256("Quota(uint256 agentId,uint256 extra,bytes32 reason,uint256 deadline)")
domainSeparator  keccak256(abi.encode(DOMAIN_TYPEHASH, keccak256(name), keccak256(version), chainId, verifyingContract))
structHash       keccak256(abi.encode(TYPEHASH, ...fields...))
digest           keccak256(0x1901 || domainSeparator || structHash)
signature        r||s||v, 65 bytes, 0x-hex
```

## Endpoints

### `GET /config`
```json
{"chainId":4663,"rpc":"https://rpc.mainnet.chain.robinhood.com","mintAddr":"","tokenAddr":"",
 "signer":"0x...","agentRegistry":"0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F"}
```
Addresses come from the environment and default to blank.

### `POST /prepare/mint`  `{ "wallet": "0x...", "shares": 1, "tokenId": 777 }`
Verifies `ownerOf(tokenId) == wallet` on BNB, then returns a signed `Mint` voucher and the
`mint(...)` call. **`tokenId` is required** (the Agent-ID the wallet holds); `agentId = tokenId`.
```json
{"ok":true,"action":"mint",
 "sign_this":{"to":"<ROB_MINT_ADDR>","data":"0x9f23ea82...","value":"<maxCostWei>"},
 "minTokensOut":"<wei>",
 "voucher":{"minter":"0x...","agentId":"<tokenId>","agentIdSource":"tokenId","shares":1,
            "nonce":"...","deadline":...,"signature":"0x...","signer":"0x...",
            "verifyingContract":"0x...","chainId":4663}}
```
- `data` = selector `0x9f23ea82` of `mint(uint256,uint256,uint256,uint256,bytes,uint256,uint256)` +
  `abi.encode(shares, agentId, nonce, deadline, sig, maxCost, minTokensOut)`.
- `value` (`maxCostWei`) = `quoteEth(shares)` read live over the RH RPC, **+2% buffer** for oracle
  drift (`ROB_MAXCOST_BUFFER_BPS`). If the mint is not deployed yet the quote is unavailable, so
  `value` is `"0"` and a `note` says it must be filled before sending.
- `minTokensOut` (slippage floor): **pre-open** = `shares × 50M` $ROB (exact). **Post-open** =
  expected tokens `≈ Σ_pay × t / (q × 0.99)` from the live `reserves()` minus a tolerance
  (`ROB_SLIPPAGE_TOLERANCE_BPS`, default 1.5%). Previously this was `0` post-open, which removed
  slippage protection exactly when the pool price can move.
- Refusals: bad wallet / bad `tokenId` → 400; missing or unowned `tokenId` → 403; BNB RPC error →
  502 (retryable). `ownerOf` results are cached per `(wallet, tokenId)` for `ROB_GATE_CACHE_TTL`.

### `POST /prepare/social`  `{ "wallet": "0x...", "task": "...", "tokenId": 777 }`
Verifies `ownerOf(tokenId) == wallet`, then issues a `Quota` voucher granting **+1** (with
`reason = keccak256("SOCIAL")`, signed by the **quota** key) **only** if the `(agentId, task)` pair
has been **approved** (see below) and **not yet claimed**. The claim is recorded in a persistent
SQLite ledger (`ROB_DB_PATH`), so each `(agentId, task)` yields **at most one** voucher — ever.
This closes the quota-farming hole (repeatedly hitting the endpoint can no longer self-grant quota).
Returns `sign_this` for `grantQuota(uint256,uint256,bytes32,uint256,bytes)`.
- Refusals: not approved → 403; already claimed → 409.

### `POST /admin/approve-social`  `{ "agentId": 777, "task": "...", "token": "<ADMIN_TOKEN>" }`
Admin-only (requires `ROB_ADMIN_TOKEN`, compared in constant time). Marks a `(agentId, task)` pair
eligible for a single social grant. This is the **live verification gate**: an operator approves a
task after confirming it off-band. `verify_task_offchain(agentId, task, proof)` in `app.py` is a
clearly-commented stub for wiring real X/Twitter API verification later (auto-approve on success);
it is disabled by default. If `ROB_ADMIN_TOKEN` is unset, approvals (and thus all social grants)
are off: 503.

### `GET /api/status`
Reads `totalMinted`, `TOTAL_SHARES`, `opened`, `reserves`, `floorValue`, and the current tier
`priceCents(totalMinted+1)` over the RH RPC. Returns graceful zeros (never 500) if the mint is not
deployed or a read fails.

### `GET /health`
Liveness + signer address + whether the mint is deployed.

## `agentId` derivation

`agentId` **is** the Agent-ID `tokenId` the client sends, after the backend verifies
`ownerOf(tokenId) == wallet` on the BNB registry. There is **no** per-wallet fallback: the removed
`keccak(wallet)` path let an attacker transfer the NFT to a fresh wallet and farm a new per-wallet
quota repeatedly. Binding `agentId` to the NFT means quota follows the token. A holder of multiple
Agent-IDs may choose which `tokenId` to mint under. `voucher.agentIdSource` is always `"tokenId"`.

## Configuration (environment)

| Var | Default | Meaning |
|-----|---------|---------|
| `ROB_SIGNER_KEY` | — | mint/gate-signer private key (0x-hex). Takes priority over the keyfile |
| `ROB_SIGNER_KEYFILE` | `./secrets/signer.key` | mint key file (0600). **Never auto-created** |
| `ROB_QUOTA_SIGNER_KEY` | — | quota-signer private key (0x-hex). Separate from the mint key |
| `ROB_QUOTA_SIGNER_KEYFILE` | `./secrets/quota_signer.key` | quota key file (0600). **Never auto-created** |
| `ROB_ADMIN_TOKEN` | `""` | bearer token for `POST /admin/approve-social`. Unset → approvals disabled |
| `ROB_MINT_ADDR` | `""` | deployed `RobMint` address (verifyingContract + call target) |
| `ROB_TOKEN_ADDR` | `""` | deployed $ROB token address (informational) |
| `ROB_CHAIN_ID` | `4663` | mint chain id (Robinhood Chain) |
| `ROB_RH_RPC` | `https://rpc.mainnet.chain.robinhood.com` | Robinhood Chain RPC |
| `ROB_BNB_RPC` | `https://bsc-dataseed.binance.org` | BNB Chain RPC (Agent-ID gate) |
| `ROB_AGENT_REGISTRY` | `0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F` | SPACE ID Agent-ID ERC721 on BNB |
| `ROB_VOUCHER_TTL` | `900` | voucher lifetime (seconds) |
| `ROB_MAXCOST_BUFFER_BPS` | `200` | buffer added to `quoteEth` (+2%) |
| `ROB_SLIPPAGE_TOLERANCE_BPS` | `150` | post-open `minTokensOut` tolerance (1.5%) |
| `ROB_GATE_CACHE_TTL` | `120` | seconds to cache a `(wallet,tokenId)` gate result (clamped 60–300) |
| `ROB_TRUSTED_PROXY` | `""` | comma-separated IPs/CIDRs whose forwarded-IP headers are trusted |
| `ROB_DB_PATH` | `./secrets/rob.db` | SQLite social-claim ledger |
| `ROB_BACKEND_PORT` / `ROB_BACKEND_HOST` | `8260` / `0.0.0.0` | bind address (use `127.0.0.1` in prod) |

> The Robinhood Chain RPC returns **403** without a browser-style `User-Agent` header, so every RPC
> call here sends `User-Agent: Mozilla/5.0`.

## Key handling

There are **two independent signer keys** — a mint/gate key and a quota key — so a leak of one does
not grant the other's power. Each is loaded from its env var or keyfile (`0600`). Keys are **never
auto-generated**: if a key is configured by neither env nor an existing keyfile, the service
**refuses to start**. (A silently-minted key would not match the on-chain `gateSigner`/`quotaSigner`
and would brick signing with `BadSignature`.) Keys are **never logged, printed, or returned** — only
the derived public addresses are exposed. The signers authorize vouchers only; they cannot move
user funds.

On startup, if `ROB_MINT_ADDR` is set, the service reads the contract's `gateSigner()` and
`quotaSigner()` over the RH RPC and **asserts** they equal the loaded mint/quota signer addresses,
logging a fatal error and exiting on mismatch (set `ROB_DEFER_STARTUP=1` to skip in tests).

`Deploy.s.sol` requires `QUOTA_SIGNER` to be set explicitly (no fallback to `GATE_SIGNER`); the two
should be different addresses.

## Hardening / deployment

- Run as a **non-root** user; the key files and `ROB_DB_PATH` only need to be readable/writable by
  that user (`0600` / `0700` dir).
- Bind to **`127.0.0.1`** behind a reverse proxy (`ROB_BACKEND_HOST=127.0.0.1`). Forwarded-IP
  headers (`CF-Connecting-IP` / `X-Forwarded-For`) are **only** honored when the direct peer is in
  `ROB_TRUSTED_PROXY`; otherwise the real peer address is used for rate limiting. This prevents
  header spoofing from minting unlimited rate-limit buckets.

## Run

```bash
pip install -r requirements.txt
python3 self_test.py          # proves the EIP-712 matches RobMint.sol
ROB_SIGNER_KEY=0x... ROB_QUOTA_SIGNER_KEY=0x... ROB_ADMIN_TOKEN=... \
  ROB_MINT_ADDR=0x... ROB_BACKEND_HOST=127.0.0.1 python3 app.py
```
