#!/usr/bin/env python3
"""AgentRob voucher backend — signs EIP-712 vouchers for the $ROB agent-gated mint.

Flow (non-custodial): a wallet asks to mint and tells us which Agent-ID (tokenId) it holds. The
backend verifies `ownerOf(tokenId) == wallet` on the BNB Chain Agent-ID registry (the gate), binding
the quota to that specific NFT. If it checks out, agentId = tokenId; the backend picks a random
nonce, sets a short deadline, and signs a Mint voucher with the mint/gate-signer key. It returns the
voucher plus the exact `sign_this` transaction (to / data / value) for the wallet to sign and submit
itself. The backend never holds funds and never broadcasts.

Quota grants (social/invite) are a separate, privileged path: a (agentId, task) pair must first be
approved by an admin (ADMIN_TOKEN) and is claimable AT MOST ONCE. A user can never self-grant quota
by just hitting the endpoint. Mint and quota vouchers are signed with two DIFFERENT keys.

Security notes:
  * The Agent-ID gate assumes the SAME address controls the wallet on BOTH BNB and Robinhood Chain.
    That holds for EOAs and EIP-7702 accounts, but NOT necessarily for smart-contract wallets, whose
    address can be controlled by different owners on different chains. Only EOA / EIP-7702 wallets
    are safely supported by this gate.
  * Bind behind a reverse proxy on 127.0.0.1 in production (ROB_BACKEND_HOST=127.0.0.1) and run as
    a non-root user. Forwarded-IP headers are only trusted from ROB_TRUSTED_PROXY.

Endpoints:
  GET  /config               -> chain + deployed addresses + signer addresses
  POST /prepare/mint         -> gate check (ownerOf) + Mint voucher + mint() calldata + maxCost
  POST /prepare/social       -> Quota voucher (+1) for an APPROVED, unclaimed (agentId, task)
  POST /admin/approve-social -> admin marks a (agentId, task) approved (ADMIN_TOKEN required)
  GET  /api/status           -> on-chain mint stats (graceful zeros if not deployed)
  GET  /leaderboard          -> invites|mints|holders ranking (on-chain when deployed, else placeholder)
  GET  /health               -> liveness

Config is read from the environment (all addresses default blank / safe):
  ROB_SIGNER_KEY / ROB_SIGNER_KEYFILE        mint/gate signer key (see signer.py; never logged)
  ROB_QUOTA_SIGNER_KEY / ROB_QUOTA_SIGNER_KEYFILE  quota signer key (separate; never logged)
  ROB_ADMIN_TOKEN      bearer token for POST /admin/approve-social (if unset, approvals disabled)
  ROB_MINT_ADDR        deployed RobMint address (verifyingContract + call target)
  ROB_TOKEN_ADDR       deployed $ROB token address (informational, for /config)
  ROB_CHAIN_ID         mint chain id (default 4663, Robinhood Chain)
  ROB_RH_RPC           Robinhood Chain RPC (default https://rpc.mainnet.chain.robinhood.com)
  ROB_BNB_RPC          BNB Chain RPC (default https://bsc-dataseed.binance.org)
  ROB_AGENT_REGISTRY   SPACE ID ".ai Agent ID" ERC721 on BNB (default below)
  ROB_VOUCHER_TTL      voucher lifetime seconds (default 900)
  ROB_MAXCOST_BUFFER_BPS  buffer added to quoteEth for oracle drift (default 200 = +2%)
  ROB_SLIPPAGE_TOLERANCE_BPS  post-open minTokensOut tolerance (default 150 = 1.5%)
  ROB_GATE_CACHE_TTL   seconds to cache a (wallet,tokenId) gate result (default 120)
  ROB_TRUSTED_PROXY    comma-separated IPs/CIDRs whose forwarded-IP headers we trust (default none)
  ROB_DB_PATH          SQLite file for social-claim state (default ./secrets/rob.db)
  ROB_BACKEND_PORT / ROB_BACKEND_HOST  bind address (default 8260 / 0.0.0.0; use 127.0.0.1 in prod)
"""
import hmac
import ipaddress
import json
import os
import re
import secrets
import urllib.parse
import sqlite3
import sys
import time
import urllib.request
from collections import deque

from flask import Flask, jsonify, request

import calldata
import signer

# --- config -------------------------------------------------------------------------------------
CHAIN_ID = int(os.environ.get("ROB_CHAIN_ID", "4663"))
RH_RPC = os.environ.get("ROB_RH_RPC", "https://rpc.mainnet.chain.robinhood.com")
BNB_RPC = os.environ.get("ROB_BNB_RPC", "https://bsc-dataseed.binance.org")
MINT_ADDR = os.environ.get("ROB_MINT_ADDR", "").strip()
TOKEN_ADDR = os.environ.get("ROB_TOKEN_ADDR", "").strip()
# SPACE ID ".ai Agent ID" ERC721 registry on BNB Chain — the identity gate source of truth.
AGENT_REGISTRY = os.environ.get(
    "ROB_AGENT_REGISTRY", "0x60B9262456dE011F7d15a54CdD8c48bAaa8B022F").strip()
VOUCHER_TTL = int(os.environ.get("ROB_VOUCHER_TTL", "900"))
MAXCOST_BUFFER_BPS = int(os.environ.get("ROB_MAXCOST_BUFFER_BPS", "200"))  # +2% default
# Post-open slippage tolerance applied to the computed expected token amount (minTokensOut).
SLIPPAGE_TOLERANCE_BPS = int(os.environ.get("ROB_SLIPPAGE_TOLERANCE_BPS", "150"))  # 1.5% default
MAX_PER_TX = 10  # mirrors RobMint.MAX_PER_TX
MINT_PRICE_BPS = 9_900  # mirrors RobMint.MINT_PRICE_BPS (post-open mint buys at 99% of pool price)
# Bound @X handles per agentId, kept in a small JSON file (see /leaderboard). Optional.
XBIND_PATH = os.environ.get("ROB_XBIND_PATH", "./secrets/xbind.json")
# Admin token gating social-quota approvals. If unset, approvals (and thus social grants) are OFF.
ADMIN_TOKEN = os.environ.get("ROB_ADMIN_TOKEN", "").strip()
# Persistent social-claim ledger (one grant per (agentId, task), ever).
DB_PATH = os.environ.get("ROB_DB_PATH", "./secrets/rob.db")
# How long to cache a (wallet, tokenId) gate result so request spam can't get our IP rate-limited
# by the public BNB node. Clamp to the 60..300s window the audit asked for.
GATE_CACHE_TTL = max(60, min(300, int(os.environ.get("ROB_GATE_CACHE_TTL", "120"))))

# A real browser-style User-Agent header is REQUIRED on the Robinhood Chain RPC (it returns 403
# without one). We send it on every RPC call so both RH and BNB reads work the same way.
_UA = "Mozilla/5.0"

app = Flask(__name__)
_hits = {}           # client-ip -> deque[timestamps] (rate limit); pruned on a TTL (see _prune_hits)
_hits_last_prune = 0.0
_gate_cache = {}     # (wallet_lower, tokenId) -> (expiry_ts, ok_bool)


def _trusted_proxies():
    """Parse ROB_TRUSTED_PROXY (comma-separated IPs/CIDRs) into a list of ip_network objects.
    Forwarded-IP headers are honored ONLY when the direct peer (remote_addr) is in this set."""
    nets = []
    for part in os.environ.get("ROB_TRUSTED_PROXY", "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            pass  # ignore malformed entries rather than crash
    return nets


TRUSTED_PROXIES = _trusted_proxies()


# --- small helpers --------------------------------------------------------------------------------
def _addr(a) -> bool:
    return isinstance(a, str) and a.startswith("0x") and len(a) == 42


def _peer_trusted() -> bool:
    """True when the direct TCP peer (remote_addr) is a configured trusted proxy."""
    peer = request.remote_addr
    if not peer or not TRUSTED_PROXIES:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXIES)


def _client_ip() -> str:
    """Client IP for rate limiting. Forwarded-IP headers (CF-Connecting-IP / X-Forwarded-For) are
    attacker-controlled, so we honor them ONLY when the request actually came from a trusted proxy
    (ROB_TRUSTED_PROXY). Otherwise we use the real peer address. This stops header spoofing from
    minting unlimited rate-limit buckets when the server is reachable directly."""
    if _peer_trusted():
        fwd = (request.headers.get("CF-Connecting-IP")
               or request.headers.get("X-Forwarded-For", "").split(",")[0].strip())
        if fwd:
            return fwd
    return request.remote_addr or "?"


def _prune_hits(now, window=60):
    """Drop stale rate-limit buckets so `_hits` can't grow unbounded under IP churn/spoofing.
    Runs at most once per `window` seconds."""
    global _hits_last_prune
    if now - _hits_last_prune < window:
        return
    _hits_last_prune = now
    for ip in list(_hits.keys()):
        q = _hits[ip]
        while q and q[0] < now - window:
            q.popleft()
        if not q:
            del _hits[ip]


def _rate_ok(ip, limit=30, window=60) -> bool:
    now = time.time()
    _prune_hits(now, window)
    q = _hits.setdefault(ip, deque())
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def _rpc(url, method, params, timeout=12):
    """Minimal JSON-RPC POST with a browser User-Agent (RH RPC needs it). Raises on failure."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "user-agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read())
    if "result" not in j:
        raise RuntimeError(str(j.get("error") or "rpc error"))
    return j["result"]


def _eth_call(url, to, data):
    return _rpc(url, "eth_call", [{"to": to, "data": data}, "latest"])


def _u(hexstr) -> int:
    return int(hexstr, 16) if hexstr and hexstr != "0x" else 0


def _body():
    return request.get_json(force=True, silent=True) or {}


@app.after_request
def _cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "content-type"
    return r


# --- Agent-ID gate (BNB Chain) --------------------------------------------------------------------
# The gate binds quota to a SPECIFIC Agent-ID NFT, not to a wallet: the client sends the tokenId it
# holds and we verify ownerOf(tokenId) == wallet on the BNB registry. agentId = tokenId. There is no
# per-wallet fallback: an attacker can no longer transfer the NFT to a fresh wallet to farm a new
# per-wallet quota, because the on-chain quota ledger is keyed by the tokenId, which travels WITH the
# NFT. A holder of multiple IDs may pick which tokenId to use.
_REVERT_HINTS = ("revert", "nonexistent", "invalid token", "owner query", "erc721")


def _owner_of(token_id):
    """Read ownerOf(tokenId) on the BNB registry. Returns (owner_lower_or_None, err).
    - A clean on-chain revert (token does not exist) -> (None, None): a definite "no such token".
    - Transport / flaky-node errors or empty replies -> retried up to 3 times, then (None, err) so
      the caller returns a retryable 502 (mirrors the previous 3-read flaky-node handling)."""
    data = calldata.owner_of_data(token_id)
    last_err = None
    for _ in range(3):
        try:
            res = _eth_call(BNB_RPC, AGENT_REGISTRY, data)
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if any(h in msg for h in _REVERT_HINTS):
                return None, None  # token doesn't exist -> clean negative, not a flaky read
            last_err = str(e)
            continue
        if not res or res == "0x":
            last_err = "empty ownerOf result from node (flaky RPC)"
            continue
        # address is right-aligned in the 32-byte word; take the last 20 bytes.
        return "0x" + res[-40:].lower(), None
    return None, last_err or "ownerOf read failed"


def _verify_agent_id(wallet, token_id):
    """Verify `wallet` owns Agent-ID `token_id` on BNB Chain. Returns (ok, err).
    Result is cached per (wallet, tokenId) for GATE_CACHE_TTL so request spam can't get our IP
    rate-limited by the public BNB node (a flaky error is NOT cached, so it stays retryable)."""
    if not _addr(AGENT_REGISTRY):
        return False, "agent registry not configured"
    key = (wallet.lower(), int(token_id))
    now = time.time()
    hit = _gate_cache.get(key)
    if hit and hit[0] > now:
        return hit[1], None
    owner, err = _owner_of(token_id)
    if err:
        return False, err  # retryable; do not cache
    ok = owner is not None and owner == wallet.lower()
    _gate_cache[key] = (now + GATE_CACHE_TTL, ok)
    return ok, None


# --- social-quota claim ledger (SQLite) -----------------------------------------------------------
# Quota vouchers are the only way to lift an agent above BASE_PER_ID shares, and the on-chain cap is
# MAX_EXTRA_QUOTA=90. Without a server-side ledger, /prepare/social could be called ~90 times with
# fresh deadlines (each digest differs, so the on-chain replay guard never trips) and then redeemed
# 90 times — defeating the cap for everyone. The ledger makes each (agentId, task) claimable AT MOST
# ONCE, and only after it has been approved. A user can NEVER self-grant quota by hitting /prepare.
def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS social_claims (
            agent_id    TEXT NOT NULL,
            task        TEXT NOT NULL,
            approved    INTEGER NOT NULL DEFAULT 0,
            claimed     INTEGER NOT NULL DEFAULT 0,
            created     REAL,
            approved_at REAL,
            claimed_at  REAL,
            PRIMARY KEY (agent_id, task)
        )""")
    conn.commit()
    return conn


def _social_row(agent_id, task):
    with _db() as conn:
        cur = conn.execute(
            "SELECT approved, claimed FROM social_claims WHERE agent_id=? AND task=?",
            (str(agent_id), task))
        return cur.fetchone()


def _social_approve(agent_id, task):
    """Mark (agentId, task) approved (admin action). Idempotent; never un-claims a claimed row."""
    now = time.time()
    with _db() as conn:
        conn.execute("""
            INSERT INTO social_claims (agent_id, task, approved, claimed, created, approved_at)
            VALUES (?, ?, 1, 0, ?, ?)
            ON CONFLICT(agent_id, task) DO UPDATE SET approved=1,
                approved_at=COALESCE(approved_at, excluded.approved_at)""",
            (str(agent_id), task, now, now))
        conn.commit()


def _social_claim(agent_id, task):
    """Atomically mark an approved, unclaimed (agentId, task) as claimed. Returns True if THIS call
    performed the claim (so exactly one voucher is ever issued), False otherwise."""
    now = time.time()
    with _db() as conn:
        cur = conn.execute(
            "UPDATE social_claims SET claimed=1, claimed_at=? "
            "WHERE agent_id=? AND task=? AND approved=1 AND claimed=0",
            (now, str(agent_id), task))
        conn.commit()
        return cur.rowcount == 1


_X_STATUS_RE = re.compile(r"^https?://(?:www\.)?(?:x|twitter)\.com/[^/]+/status/\d+", re.I)


def verify_task_offchain(agentId, task, proof):  # noqa: N803 - external field name
    """Verify a social task WITHOUT an X API key, via the public X oEmbed endpoint.

    For `post`/`comment`: the proof must be a real x.com/twitter.com status link whose embedded text
    mentions the token ($ROB / AgentRob). oEmbed (publish.twitter.com/oembed) is public and returns
    the tweet HTML, so we can confirm the post exists and names $ROB — no API key, not trivially
    fakeable (a random click can't pass). `follow` has no public proof, so it stays trust-based and
    is handled by the caller. Returns True only on a verified mention."""
    if task == "follow":
        return True  # no public way to verify a follow; caller treats it as trust-based
    if not proof or not isinstance(proof, str):
        return False
    url = proof.strip()
    if not _X_STATUS_RE.match(url):
        return False
    try:
        api = ("https://publish.twitter.com/oembed?omit_script=true&dnt=true&url="
               + urllib.parse.quote(url, safe=""))
        req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0 (AgentRob gate)"})
        with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310 - fixed https host
            data = json.load(r)
    except Exception:  # noqa: BLE001 - any failure = not verified (fail closed)
        return False
    html = (data.get("html") or "").lower()
    return ("$rob" in html) or ("agentrob" in html) or ("agent rob" in html)


# --- on-chain quoting / status (Robinhood Chain) --------------------------------------------------
def _quote_eth(shares):
    """quoteEth(shares) in wei via RH RPC, or None if unavailable."""
    if not _addr(MINT_ADDR):
        return None
    try:
        return _u(_eth_call(RH_RPC, MINT_ADDR, calldata.quote_eth_data(shares)))
    except Exception:  # noqa: BLE001
        return None


def _max_cost(q):
    """Max wei the caller will pay = quoteEth(shares) + buffer (for oracle drift). `q` is a
    pre-fetched quoteEth(shares) in wei (or None). Returns (value_str, note). If the quote is
    unavailable, value is "0" and the note says it must be filled before sending."""
    if q is None:
        return "0", "quoteEth unavailable (mint not deployed or RPC error) — maxCost MUST be filled before sending"
    buffered = q + (q * MAXCOST_BUFFER_BPS) // 10_000
    return str(buffered), None


def _reserves():
    """(rQuote, rToken) from the pool, or (None, None) if unavailable."""
    if not _addr(MINT_ADDR):
        return None, None
    try:
        res = _eth_call(RH_RPC, MINT_ADDR, calldata.reserves_data())
        raw = res[2:] if res and res.startswith("0x") else ""
        if len(raw) >= 128:
            return int(raw[0:64], 16), int(raw[64:128], 16)
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _fmt_eth(wei) -> str:
    """Human ETH string from wei (up to 6 dp, trailing zeros trimmed)."""
    try:
        v = int(wei) / 10**18
    except (TypeError, ValueError):
        return "0"
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    return s or "0"


# --- routes ---------------------------------------------------------------------------------------
@app.route("/health")
def health():
    # Deliberately does NOT echo the client IP (no need to reflect caller-controlled data).
    return jsonify(ok=True, signer=signer.signer_address(), chainId=CHAIN_ID,
                   mintDeployed=_addr(MINT_ADDR))


@app.route("/config")
def config():
    return jsonify(
        chainId=CHAIN_ID,
        rpc=RH_RPC,
        mintAddr=MINT_ADDR,
        tokenAddr=TOKEN_ADDR,
        signer=signer.signer_address(),
        quotaSigner=signer.quota_signer_address(),
        agentRegistry=AGENT_REGISTRY,
    )


@app.route("/prepare/mint", methods=["POST", "OPTIONS"])
def prepare_mint():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _rate_ok(_client_ip()):
        return jsonify(error="rate limited — slow down"), 429
    b = _body()
    wallet = (b.get("wallet") or "").strip()
    if not _addr(wallet):
        return jsonify(error="bad wallet"), 400
    try:
        shares = int(b.get("shares") or 1)
    except (TypeError, ValueError):
        return jsonify(error="bad shares"), 400
    if shares < 1 or shares > MAX_PER_TX:
        return jsonify(error=f"shares must be 1..{MAX_PER_TX}"), 400
    # The client MUST name the Agent-ID (tokenId) it holds. agentId is bound to the NFT, so quota
    # travels with the token and cannot be reset by moving it to a fresh wallet.
    token_id = b.get("tokenId")
    if token_id is None or (isinstance(token_id, str) and not token_id.strip()):
        return jsonify(error="no Agent-ID on BNB Chain — agent-gated, refused"), 403
    try:
        token_id = int(str(token_id).strip(), 0)
    except (TypeError, ValueError):
        return jsonify(error="bad tokenId"), 400
    if token_id < 0:
        return jsonify(error="bad tokenId"), 400

    ok, err = _verify_agent_id(wallet, token_id)
    if err:
        return jsonify(error="gate read failed on BNB Chain: " + err), 502
    if not ok:
        return jsonify(error="no Agent-ID on BNB Chain — agent-gated, refused"), 403

    agent_id = token_id
    nonce = secrets.randbits(252)
    deadline = int(time.time()) + VOUCHER_TTL
    sig = signer.sign_mint(wallet, agent_id, nonce, deadline,
                           verifying_contract=MINT_ADDR or signer.ZERO_ADDR, chain_id=CHAIN_ID)

    q = _quote_eth(shares)
    value, note = _max_cost(q)
    # Slippage floor (minTokensOut):
    #   pre-open  -> each share mints exactly USER_SHARE (50k) $ROB, so pin to shares * USER_SHARE.
    #   post-open -> the minter buys at 99% of pool price: tokens ≈ Σ_pay × t / (q × 0.99) at the
    #                current reserves (t=token reserve, q=quote reserve). We set minTokensOut to that
    #                expected amount minus a small tolerance so the client keeps real slippage
    #                protection instead of the old minTokensOut=0 (which disabled it post-open).
    USER_SHARE = 50_000 * 10**18
    is_open = False
    if MINT_ADDR:
        try:
            is_open = bool(_u(_eth_call(RH_RPC, MINT_ADDR, calldata.opened_data())))
        except Exception:  # noqa: BLE001
            is_open = False
    min_tokens_out = 0
    if not is_open:
        min_tokens_out = shares * USER_SHARE
    else:
        r_quote, r_token = _reserves()
        if q is not None and r_quote and r_token:
            expected = q * r_token * 10_000 // (r_quote * MINT_PRICE_BPS)
            min_tokens_out = expected * (10_000 - SLIPPAGE_TOLERANCE_BPS) // 10_000
        else:
            note = ((note + "; " if note else "")
                    + "post-open minTokensOut could not be computed (reserves/quote unavailable) — "
                      "client MUST set it before sending")
    data = calldata.mint(shares, agent_id, nonce, deadline, sig, int(value), int(min_tokens_out))

    resp = {
        "ok": True,
        "action": "mint",
        "sign_this": {"to": MINT_ADDR or None, "data": data, "value": value},
        "minTokensOut": str(min_tokens_out),
        "voucher": {
            "minter": wallet,
            "agentId": str(agent_id),
            "agentIdSource": "tokenId",
            "shares": shares,
            "nonce": str(nonce),
            "deadline": deadline,
            "signature": sig,
            "signer": signer.signer_address(),
            "verifyingContract": MINT_ADDR or signer.ZERO_ADDR,
            "chainId": CHAIN_ID,
        },
    }
    if note:
        resp["note"] = note
    if not _addr(MINT_ADDR):
        resp["pending_deploy"] = True
    return jsonify(resp)


@app.route("/admin/approve-social", methods=["POST", "OPTIONS"])
def admin_approve_social():
    """Admin marks a (agentId, task) pair as eligible for a single social-quota grant.
    Requires the ADMIN_TOKEN bearer (constant-time compared). This is the live verification gate:
    until real X API verification is wired (see verify_task_offchain), a human/operator approves the
    task here after confirming it off-band. A user can never approve their own task."""
    if request.method == "OPTIONS":
        return ("", 204)
    if not ADMIN_TOKEN:
        return jsonify(error="admin approvals disabled (ROB_ADMIN_TOKEN not set)"), 503
    if not _rate_ok(_client_ip()):
        return jsonify(error="rate limited — slow down"), 429
    b = _body()
    token = (b.get("token") or "").strip()
    if not token or not hmac.compare_digest(token, ADMIN_TOKEN):
        return jsonify(error="unauthorized"), 401
    agent_id = b.get("agentId")
    task = (b.get("task") or "").strip()
    if agent_id is None or str(agent_id).strip() == "":
        return jsonify(error="bad agentId"), 400
    try:
        agent_id = int(str(agent_id).strip(), 0)
    except (TypeError, ValueError):
        return jsonify(error="bad agentId"), 400
    if agent_id < 0 or not task:
        return jsonify(error="bad agentId or task"), 400
    row = _social_row(agent_id, task)
    if row and row[1] == 1:
        return jsonify(ok=True, agentId=str(agent_id), task=task, approved=True, claimed=True,
                       note="already claimed — approval is a no-op"), 200
    _social_approve(agent_id, task)
    return jsonify(ok=True, agentId=str(agent_id), task=task, approved=True, claimed=False)


@app.route("/prepare/social", methods=["POST", "OPTIONS"])
def prepare_social():
    if request.method == "OPTIONS":
        return ("", 204)
    if not _rate_ok(_client_ip()):
        return jsonify(error="rate limited — slow down"), 429
    b = _body()
    wallet = (b.get("wallet") or "").strip()
    task = (b.get("task") or "").strip()
    proof = b.get("proof")  # reserved for real off-chain verification (see verify_task_offchain)
    if not _addr(wallet):
        return jsonify(error="bad wallet"), 400
    if not task:
        return jsonify(error="missing task"), 400
    # Bind to a specific Agent-ID NFT (same as mint): the client names its tokenId and we verify
    # ownership on BNB. agentId = tokenId, so quota can't be farmed by moving the NFT around.
    token_id = b.get("tokenId")
    if token_id is None or (isinstance(token_id, str) and not token_id.strip()):
        return jsonify(error="no Agent-ID on BNB Chain — agent-gated, refused"), 403
    try:
        token_id = int(str(token_id).strip(), 0)
    except (TypeError, ValueError):
        return jsonify(error="bad tokenId"), 400
    if token_id < 0:
        return jsonify(error="bad tokenId"), 400

    ok, err = _verify_agent_id(wallet, token_id)
    if err:
        return jsonify(error="gate read failed on BNB Chain: " + err), 502
    if not ok:
        return jsonify(error="no Agent-ID on BNB Chain — agent-gated, refused"), 403

    agent_id = token_id

    # Self-serve: no X API / manual approval. A submission is accepted on trust because each
    # (agentId, task) can be claimed EXACTLY ONCE (the ledger below enforces it) and the on-chain
    # MAX_EXTRA_QUOTA = 90 cap bounds total bonus. `proof` (the post/comment link the user pasted)
    # is accepted but not gated on an external API. verify_task_offchain stays as the optional hook
    # for a future live X-API check.
    # post/comment must pass X-oEmbed verification (a real link mentioning $ROB); follow is
    # trust-based. Each (agentId, task) is still claimable exactly once (ledger below).
    if task in ("post", "comment"):
        if not verify_task_offchain(agent_id, task, proof):
            return jsonify(error="could not verify — paste the link to your X " + task
                                 + " that mentions $ROB"), 400
    _social_approve(agent_id, task)  # approve on verified submit (follow = self-serve)
    row = _social_row(agent_id, task)
    if not row or row[0] != 1:
        return jsonify(error="could not record the task — try again"), 500
    if row[1] == 1:
        return jsonify(error="task already claimed — each task is +1 share, once per Agent-ID"), 409
    # Atomically claim so exactly one voucher can ever be issued for this (agentId, task).
    if not _social_claim(agent_id, task):
        return jsonify(error="task already claimed — each (agentId, task) is claimable once"), 409

    extra = 1  # social task grants +1 quota
    reason = calldata.reason_hash("SOCIAL")
    deadline = int(time.time()) + VOUCHER_TTL
    sig = signer.sign_quota(agent_id, extra, reason, deadline,
                            verifying_contract=MINT_ADDR or signer.ZERO_ADDR, chain_id=CHAIN_ID)
    data = calldata.grant_quota(agent_id, extra, reason, deadline, sig)

    resp = {
        "ok": True,
        "action": "social",
        "task": task,
        "sign_this": {"to": MINT_ADDR or None, "data": data, "value": "0"},
        "voucher": {
            "agentId": str(agent_id),
            "agentIdSource": "tokenId",
            "extra": extra,
            "reason": reason,
            "reasonLabel": "SOCIAL",
            "deadline": deadline,
            "signature": sig,
            "signer": signer.quota_signer_address(),
            "verifyingContract": MINT_ADDR or signer.ZERO_ADDR,
            "chainId": CHAIN_ID,
        },
    }
    if not _addr(MINT_ADDR):
        resp["pending_deploy"] = True
    return jsonify(resp)


@app.route("/api/status")
def api_status():
    """On-chain mint snapshot. Returns graceful zeros if the mint is not deployed or RPC fails."""
    base = {
        "ok": True,
        "mintAddr": MINT_ADDR or None,
        "deployed": _addr(MINT_ADDR),
        "totalMinted": 0,
        "totalShares": 0,
        "opened": False,
        "reserves": {"quote": "0", "token": "0"},
        "floorValue": "0",
        "priceCents": 0,
        "sold": 0,
        "openAt": 0,
        "firstMintTime": 0,
        "refundWindow": 0,
        "ca": TOKEN_ADDR or None,
    }
    if not _addr(MINT_ADDR):
        base["note"] = "mint not deployed — set ROB_MINT_ADDR"
        return jsonify(base)
    try:
        total_minted = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.total_minted_data()))
        base["totalMinted"] = total_minted
        try:
            base["totalShares"] = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.total_shares_data()))
        except Exception:  # noqa: BLE001
            pass
        try:
            base["opened"] = bool(_u(_eth_call(RH_RPC, MINT_ADDR, calldata.opened_data())))
        except Exception:  # noqa: BLE001
            pass
        try:
            res = _eth_call(RH_RPC, MINT_ADDR, calldata.reserves_data())
            raw = res[2:] if res and res.startswith("0x") else ""
            if len(raw) >= 128:
                base["reserves"] = {"quote": str(int(raw[0:64], 16)),
                                    "token": str(int(raw[64:128], 16))}
        except Exception:  # noqa: BLE001
            pass
        try:
            base["floorValue"] = str(_u(_eth_call(RH_RPC, MINT_ADDR, calldata.floor_value_data())))
        except Exception:  # noqa: BLE001
            pass
        # current tier price = priceCents(totalMinted + 1), the next share to be sold
        try:
            base["priceCents"] = _u(_eth_call(
                RH_RPC, MINT_ADDR, calldata.price_cents_data(total_minted + 1)))
        except Exception:  # noqa: BLE001
            pass
        base["sold"] = total_minted
        for key, data in (("openAt", calldata.open_at_data()),
                          ("firstMintTime", calldata.first_mint_time_data()),
                          ("refundWindow", calldata.refund_window_data())):
            try:
                base[key] = _u(_eth_call(RH_RPC, MINT_ADDR, data))
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001 - never 500 on a read; report zeros + note
        base["note"] = "status read failed: " + str(e)[:160]
    return jsonify(base)


@app.route("/refund/status", methods=["POST", "OPTIONS"])
def refund_status():
    """Read a wallet's refundable escrow and whether the refund window is open. Read-only."""
    if request.method == "OPTIONS":
        return ("", 204)
    b = _body()
    addr = (b.get("addr") or b.get("wallet") or "").strip()
    if not _addr(addr):
        return jsonify(error="bad address"), 400
    if not _addr(MINT_ADDR):
        return jsonify(ok=True, shares=0, refundWei="0", refundEth="0", open=False,
                       note="mint not deployed")
    try:
        shares = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.refund_shares_data(addr)))
        wei = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.refund_paid_data(addr)))
        opened = bool(_u(_eth_call(RH_RPC, MINT_ADDR, calldata.opened_data())))
        first = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.first_mint_time_data()))
        window = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.refund_window_data()))
    except Exception as e:  # noqa: BLE001
        return jsonify(error="refund read failed: " + str(e)[:160]), 502
    now = int(time.time())
    is_open = (not opened) and first > 0 and now >= first + window
    return jsonify(ok=True, shares=shares, refundWei=str(wei), refundEth=_fmt_eth(wei),
                   open=is_open, windowEnd=(first + window) if first else 0)


@app.route("/prepare/refund", methods=["POST", "OPTIONS"])
def prepare_refund():
    """Build refund() calldata for a wallet to send. refund() is permissionless and takes no args;
    we only hand it out once the window is actually open and the wallet has escrow to reclaim."""
    if request.method == "OPTIONS":
        return ("", 204)
    b = _body()
    wallet = (b.get("wallet") or b.get("addr") or "").strip()
    if not _addr(wallet):
        return jsonify(error="bad wallet"), 400
    if not _addr(MINT_ADDR):
        return jsonify(error="mint not deployed"), 400
    try:
        shares = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.refund_shares_data(wallet)))
        opened = bool(_u(_eth_call(RH_RPC, MINT_ADDR, calldata.opened_data())))
        first = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.first_mint_time_data()))
        window = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.refund_window_data()))
    except Exception as e:  # noqa: BLE001
        return jsonify(error="refund read failed: " + str(e)[:160]), 502
    now = int(time.time())
    if shares == 0:
        return jsonify(error="nothing to refund for this wallet"), 400
    if opened:
        return jsonify(error="pool opened — refunds are closed"), 400
    if not (first > 0 and now >= first + window):
        return jsonify(error="refund window is not open yet"), 400
    return jsonify(ok=True, to=MINT_ADDR, data=calldata.refund_tx_data(), value="0x0",
                   chainId=CHAIN_ID)


@app.route("/ledger")
def ledger():
    """Recent mints from the Minted event log. Optional ?wallet= filter. Graceful empty pre-deploy."""
    wallet = (request.args.get("wallet") or "").strip()
    if not _addr(MINT_ADDR):
        return jsonify(ok=True, rows=[], note="mint not deployed")
    try:
        topics = [calldata.minted_topic()]
        if _addr(wallet):
            topics.append("0x" + to_checksum_address(wallet)[2:].lower().rjust(64, "0"))
        logs = _rpc(RH_RPC, "eth_getLogs", [{
            "address": MINT_ADDR, "fromBlock": "earliest", "toBlock": "latest",
            "topics": topics}])
        rows = []
        for lg in (logs or [])[-200:]:
            data = (lg.get("data") or "0x")[2:]
            tp = lg.get("topics") or []
            agent_id = int(tp[2], 16) if len(tp) > 2 else 0
            minter = ("0x" + tp[1][-40:]) if len(tp) > 1 else None
            serial = int(data[0:64], 16) if len(data) >= 64 else 0
            shares = int(data[64:128], 16) if len(data) >= 128 else 0
            paid = int(data[128:192], 16) if len(data) >= 192 else 0
            row = {"serial": serial, "shares": shares, "agentId": agent_id,
                   "wallet": minter, "time": None}
            if paid == 0:
                row["cents"] = 0
            else:
                row["paidEth"] = _fmt_eth(paid)
            rows.append(row)
        rows.reverse()  # newest first
        return jsonify(ok=True, rows=rows)
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=True, rows=[], note="ledger read failed: " + str(e)[:160])


@app.route("/wallet/allowance", methods=["POST", "GET", "OPTIONS"])
def wallet_allowance():
    """Live mint allowance for a wallet / Agent-ID, read straight from the mint contract.

    Free cap is PER AGENT-ID (`freeQuotaOf` = base 3 + invite/social grants) minus `freeMinted`.
    Paid cap is PER WALLET (`MAX_PAID_PER_WALLET`) minus `paidMinted`. The contract only lets a
    mint take free shares while the global serial is still inside the free zone (serial <=
    FREE_SHARES); once `sold` passes it, every new share is paid. So the real ceiling the UI must
    clamp to is phase-aware: in the free phase it is the remaining free quota, in the paid phase it
    is the remaining paid cap. Read-only; never signs. Graceful defaults pre-deploy."""
    if request.method == "OPTIONS":
        return ("", 204)
    b = _body() if request.method == "POST" else request.args
    wallet = (b.get("wallet") or b.get("addr") or "").strip()
    agent_raw = (str(b.get("agentId") or b.get("agent_id") or "")).strip()
    agent_id = int(agent_raw) if agent_raw.isdigit() else None

    # defaults mirror the contract constants so the UI has honest caps before connect / deploy
    out = {
        "ok": True, "connected": False, "deployed": _addr(MINT_ADDR),
        "freeCap": 1, "freeUsed": 0, "freeRemaining": 1,
        "paidCap": 25, "paidUsed": 0, "paidRemaining": 25,
        "sold": 0, "freeShares": 1000, "openAt": 10000,
        "phase": "free", "maxShares": 3, "agentId": agent_id, "wallet": wallet or None,
    }
    if not _addr(MINT_ADDR):
        out["note"] = "mint not deployed — showing base caps (1 free / 25 paid)"
        return jsonify(out)

    try:
        out["freeShares"] = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.free_shares_data()))
        out["paidCap"] = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.max_paid_data()))
        out["openAt"] = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.open_at_data()))
        out["sold"] = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.total_minted_data()))
    except Exception as e:  # noqa: BLE001
        out["note"] = "config read failed: " + str(e)[:140]
        return jsonify(out)

    # best-effort: resolve the Agent-ID from the wallet if the caller did not pass one
    if agent_id is None and _addr(wallet) and _addr(AGENT_REGISTRY):
        try:
            res = _eth_call(BNB_RPC, AGENT_REGISTRY,
                            calldata.token_of_owner_by_index_data(wallet, 0))
            tid = _u(res)
            if tid > 0:
                agent_id = tid
                out["agentId"] = tid
        except Exception:  # noqa: BLE001
            pass

    if agent_id is not None:
        try:
            out["freeCap"] = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.free_quota_of_data(agent_id)))
            out["freeUsed"] = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.free_minted_data(agent_id)))
            out["connected"] = True
        except Exception:  # noqa: BLE001
            pass
    if _addr(wallet):
        try:
            out["paidUsed"] = _u(_eth_call(RH_RPC, MINT_ADDR, calldata.paid_minted_data(wallet)))
            out["connected"] = True
        except Exception:  # noqa: BLE001
            pass

    out["freeRemaining"] = max(0, out["freeCap"] - out["freeUsed"])
    out["paidRemaining"] = max(0, out["paidCap"] - out["paidUsed"])
    global_free_left = max(0, out["freeShares"] - out["sold"])
    if global_free_left > 0:
        out["phase"] = "free"
        # in the free phase a mint can only take as many free shares as the agent has left,
        # bounded by what remains of the global free zone
        out["maxShares"] = max(0, min(out["freeRemaining"], global_free_left))
    else:
        out["phase"] = "paid"
        out["maxShares"] = out["paidRemaining"]
    return jsonify(out)


# --- connect: pairing session --------------------------------------------------------------------
@app.route("/gate/check", methods=["POST", "OPTIONS"])
def gate_check():
    """Agent-ID gate: does this wallet hold a verified .ai Agent-ID on BNB? Reads the registry
    balanceOf and best-effort returns the first tokenId via tokenOfOwnerByIndex. Read-only, no key
    material. Fails CLOSED (isAgent=false) on any read error so the gate can't be bypassed by
    flaking the node."""
    if request.method == "OPTIONS":
        return ("", 204)
    b = _body()
    wallet = (b.get("wallet") or b.get("addr") or "").strip()
    if not _addr(wallet):
        return jsonify(error="bad wallet"), 400
    if not _addr(AGENT_REGISTRY):
        return jsonify(ok=True, isAgent=False, tokenId=None, note="agent registry not configured")
    try:
        bal = _u(_eth_call(BNB_RPC, AGENT_REGISTRY, calldata.balanceof_data(wallet)))
    except Exception as e:  # noqa: BLE001
        return jsonify(ok=True, isAgent=False, tokenId=None, note="gate read failed: " + str(e)[:140])
    if bal <= 0:
        return jsonify(ok=True, isAgent=False, tokenId=None)
    token_id = None
    try:
        tid = _u(_eth_call(BNB_RPC, AGENT_REGISTRY,
                           calldata.token_of_owner_by_index_data(wallet, 0)))
        if tid > 0:
            token_id = str(tid)
    except Exception:  # noqa: BLE001
        pass  # holds >=1 but registry is not enumerable; isAgent stays true, tokenId unknown
    return jsonify(ok=True, isAgent=True, tokenId=token_id)


@app.route("/verify/social", methods=["POST", "OPTIONS"])
def verify_social():
    """On-page task verification (no wallet needed). post/comment: the pasted link must be a real X
    status whose public oEmbed text mentions $ROB. follow: verified if a valid @X handle is bound.
    Returns {verified}. The +1 is still granted on-chain via /prepare/social from the agent, which
    re-checks the same proof — this just gates the UI so a user can't claim without doing the task."""
    if request.method == "OPTIONS":
        return ("", 204)
    if not _rate_ok(_client_ip()):
        return jsonify(error="rate limited — slow down"), 429
    b = _body()
    task = (b.get("task") or "").strip()
    proof = b.get("proof")
    handle = (b.get("handle") or "").strip()
    if task == "follow":
        return jsonify(ok=True, verified=bool(re.match(r"^@?[A-Za-z0-9_]{1,15}$", handle)),
                       note=("bind your @X handle first" if not handle else ""))
    if task in ("post", "comment"):
        return jsonify(ok=True, verified=bool(verify_task_offchain(0, task, proof)))
    return jsonify(ok=True, verified=False, error="unknown task")


@app.route("/dividend/status", methods=["POST", "GET", "OPTIONS"])
def dividend_status():
    """A holder's claimable ETH dividend (their pro-rata share of the 2% sell-tax pot). Read-only."""
    if request.method == "OPTIONS":
        return ("", 204)
    b = _body() if request.method == "POST" else request.args
    wallet = (b.get("wallet") or b.get("addr") or "").strip()
    out = {"ok": True, "claimableWei": "0", "claimableEth": "0", "totalDistributedWei": "0",
           "deployed": _addr(TOKEN_ADDR)}
    if not _addr(wallet):
        return jsonify(error="bad wallet"), 400
    if not _addr(TOKEN_ADDR):
        out["note"] = "token not deployed"
        return jsonify(out)
    try:
        wei = _u(_eth_call(RH_RPC, TOKEN_ADDR, calldata.withdrawable_dividend_of_data(wallet)))
        out["claimableWei"] = str(wei)
        out["claimableEth"] = _fmt_eth(wei)
        try:
            out["totalDistributedWei"] = str(
                _u(_eth_call(RH_RPC, TOKEN_ADDR, calldata.total_dividends_distributed_data())))
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        return jsonify(error="dividend read failed: " + str(e)[:140]), 502
    return jsonify(out)


@app.route("/prepare/claim", methods=["POST", "OPTIONS"])
def prepare_claim():
    """Build the ETH-dividend claim for a holder. `sweep` converts the pending 2% pot to ETH (anyone
    may call it); `sign_this` is withdrawDividend() to pull the holder's accrued ETH. Non-custodial:
    the wallet sends both. Read-only on the server; no key material."""
    if request.method == "OPTIONS":
        return ("", 204)
    b = _body()
    wallet = (b.get("wallet") or "").strip()
    if not _addr(wallet):
        return jsonify(error="bad wallet"), 400
    if not _addr(TOKEN_ADDR):
        return jsonify(error="token not deployed"), 400
    resp = {
        "ok": True, "action": "claim",
        "sweep": {"to": TOKEN_ADDR, "data": calldata.sweep_dividends_data(), "value": "0x0"},
        "sign_this": {"to": TOKEN_ADDR, "data": calldata.withdraw_dividend_data(), "value": "0x0"},
        "chainId": CHAIN_ID,
    }
    try:
        wei = _u(_eth_call(RH_RPC, TOKEN_ADDR, calldata.withdrawable_dividend_of_data(wallet)))
        resp["claimableWei"] = str(wei)
        resp["claimableEth"] = _fmt_eth(wei)
    except Exception:  # noqa: BLE001
        pass
    return jsonify(resp)


# --- leaderboard ----------------------------------------------------------------------------------
def _load_xbind():
    """agentId(str) -> bound @X handle. Missing/invalid file is fine (returns {})."""
    try:
        with open(XBIND_PATH, "r") as fh:
            data = json.load(fh)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _short_id(agent_id: int) -> str:
    """Fallback display handle when a real .ai handle isn't resolvable."""
    h = hex(int(agent_id))[2:]
    if len(h) <= 10:
        return "agent-" + h
    return "agent-" + h[:6] + "…" + h[-4:]


def _resolve_handle(agent_id: int, cache: dict) -> str:
    """Best-effort resolve an Agent-ID (tokenId) to its `.ai` handle via the BNB registry.
    Many SPACE ID deployments expose a tokenURI / name; if we can't read one cleanly we fall back
    to a shortened id. Never raises."""
    key = str(agent_id)
    if key in cache:
        return cache[key]
    handle = _short_id(agent_id)
    try:
        # tokenURI(uint256) -> string; try to pull a human name out of it.
        data = calldata.encode_call("tokenURI(uint256)", ["uint256"], [int(agent_id)])
        res = _eth_call(BNB_RPC, AGENT_REGISTRY, data)
        if res and res != "0x":
            from eth_abi import decode as abi_decode
            uri = abi_decode(["string"], bytes.fromhex(res[2:]))[0]
            if uri and ".ai" in uri:
                # grab the last path/name-ish token that ends in .ai
                for tok in uri.replace('"', " ").replace("/", " ").split():
                    if tok.endswith(".ai") and len(tok) > 3:
                        handle = tok
                        break
    except Exception:  # noqa: BLE001 - registry may not expose tokenURI
        pass
    cache[key] = handle
    return handle


def _placeholder_rows(metric, range_):
    """Same shape the front-end uses when the backend is absent, so the site still renders when the
    mint is not yet deployed. Deterministic, clearly synthetic .ai handles — not on-chain data."""
    names = ["nocturne", "crimson", "vesper", "mordecai", "lazlo", "strix", "orlok", "carfax",
             "renfield", "ligeia", "abraxas", "nyx", "sable", "harker"]
    base = 14 if metric == "invites" else 9 if metric == "mints" else 22
    mult = 3 if range_ == "week" else 1
    rows = []
    for i in range(9):
        v = max(1, round((base - i * 1.4) * mult - (i % 2)))
        nm = names[(i * 5 + len(metric) + len(range_)) % len(names)]
        rows.append({"rank": i + 1, "id": nm + ".ai", "value": int(v)})
    return rows


def _scan_leaderboard(metric, range_):
    """Read on-chain logs to rank agents by mints or invites. Best-effort: on any failure the
    caller falls back to placeholder rows. Holders is not cheaply computable from logs, so it is
    stubbed. RPC calls go through _rpc(), which sends the required browser User-Agent."""
    from eth_utils import keccak
    minted_topic = "0x" + keccak(text="Minted(address,uint256,uint256,uint256,uint256)").hex()
    quota_topic = "0x" + keccak(text="QuotaGranted(uint256,uint256,bytes32)").hex()
    invite_reason = "0x" + keccak(text="INVITE").hex()

    def get_logs(topic0):
        return _rpc(RH_RPC, "eth_getLogs",
                    [{"address": MINT_ADDR, "fromBlock": "0x0", "toBlock": "latest",
                      "topics": [topic0]}])

    agg = {}  # agentId(int) -> value
    if metric == "mints":
        # Minted(address indexed minter, uint256 agentId, uint256 shares, uint256 serial, uint256 paid)
        # minter is indexed (topic1); agentId/shares/serial/paid are 4 data words.
        for lg in get_logs(minted_topic):
            raw = (lg.get("data") or "0x")[2:]
            if len(raw) < 64 * 4:
                continue
            agent_id = int(raw[0:64], 16)
            shares = int(raw[64:128], 16)
            agg[agent_id] = agg.get(agent_id, 0) + max(1, shares)
    elif metric == "invites":
        # QuotaGranted(uint256 agentId, uint256 extra, bytes32 reason) — all three in data.
        for lg in get_logs(quota_topic):
            raw = (lg.get("data") or "0x")[2:]
            if len(raw) < 64 * 3:
                continue
            agent_id = int(raw[0:64], 16)
            extra = int(raw[64:128], 16)
            reason = "0x" + raw[128:192]
            if reason.lower() == invite_reason.lower():
                agg[agent_id] = agg.get(agent_id, 0) + max(1, extra)
    else:
        return []  # holders: not derivable from logs here (stubbed)

    xbind = _load_xbind()
    cache = {}
    ranked = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:100]
    rows = []
    for i, (agent_id, val) in enumerate(ranked):
        row = {"rank": i + 1, "id": _resolve_handle(agent_id, cache), "value": int(val)}
        if str(agent_id) in xbind:
            row["x"] = xbind[str(agent_id)]
        rows.append(row)
    return rows


@app.route("/leaderboard")
def leaderboard():
    """GET /leaderboard?metric=invites|mints|holders&range=day|week.
    On-chain ranking when the mint is deployed (ROB_MINT_ADDR set); otherwise the placeholder
    shape so the site still renders. Never 500s on a read failure — degrades to placeholder."""
    metric = (request.args.get("metric") or "invites").lower()
    if metric not in ("invites", "mints", "holders"):
        metric = "invites"
    range_ = (request.args.get("range") or "day").lower()
    if range_ not in ("day", "week"):
        range_ = "day"

    deployed = _addr(MINT_ADDR)
    resp = {"ok": True, "metric": metric, "range": range_, "deployed": deployed}
    if not deployed:
        resp["rows"] = _placeholder_rows(metric, range_)
        resp["note"] = "mint not deployed — placeholder leaderboard"
        return jsonify(resp)
    try:
        rows = _scan_leaderboard(metric, range_)
        if rows:
            resp["rows"] = rows
        else:
            resp["rows"] = _placeholder_rows(metric, range_)
            resp["note"] = ("holders metric is best-effort/stubbed on-chain"
                            if metric == "holders" else "no on-chain activity yet — placeholder")
        if metric == "holders":
            resp["note"] = "holders metric is best-effort/stubbed (not computed from logs)"
    except Exception as e:  # noqa: BLE001
        resp["rows"] = _placeholder_rows(metric, range_)
        resp["note"] = "leaderboard read failed: " + str(e)[:160]
    return jsonify(resp)


# --- startup validation ---------------------------------------------------------------------------
def _read_signer(data) -> str:
    """Read an address-returning view (gateSigner/quotaSigner) over the RH RPC."""
    res = _eth_call(RH_RPC, MINT_ADDR, data)
    return calldata.to_checksum_address("0x" + res[-40:])


def _startup_checks():
    """Fail hard before serving: load both signer keys (never auto-generated) and, when the mint is
    deployed, assert the on-chain gateSigner()/quotaSigner() match the loaded keys. A silent key
    mismatch would brick every mint with BadSignature, so we exit loudly instead."""
    try:
        mint_signer = signer.signer_address()
        quota_signer = signer.quota_signer_address()
    except Exception as e:  # noqa: BLE001 - missing/empty key: refuse to start
        print("FATAL: signer key not available:", e, file=sys.stderr)
        sys.exit(1)

    if not _addr(MINT_ADDR):
        print("startup: mint not deployed (ROB_MINT_ADDR unset) — skipping on-chain signer assert",
              file=sys.stderr)
        return
    try:
        on_gate = _read_signer(calldata.gate_signer_data())
        on_quota = _read_signer(calldata.quota_signer_data())
    except Exception as e:  # noqa: BLE001
        print("FATAL: could not read gateSigner()/quotaSigner() over RH RPC:", e, file=sys.stderr)
        sys.exit(1)
    if on_gate.lower() != mint_signer.lower():
        print(f"FATAL: on-chain gateSigner {on_gate} != loaded mint signer {mint_signer} — "
              "mints would fail with BadSignature. Fix the key/contract before serving.",
              file=sys.stderr)
        sys.exit(1)
    if on_quota.lower() != quota_signer.lower():
        print(f"FATAL: on-chain quotaSigner {on_quota} != loaded quota signer {quota_signer} — "
              "quota grants would fail with BadSignature. Fix the key/contract before serving.",
              file=sys.stderr)
        sys.exit(1)
    print("startup: signer addresses match on-chain gateSigner/quotaSigner", file=sys.stderr)


# Run startup validation at import so it fires under any launcher (python3 app.py OR a WSGI server
# such as gunicorn, which imports this module). Set ROB_DEFER_STARTUP=1 to skip (test harnesses).
if os.environ.get("ROB_DEFER_STARTUP") != "1":
    _startup_checks()


if __name__ == "__main__":
    port = int(os.environ.get("ROB_BACKEND_PORT", "8260"))
    host = os.environ.get("ROB_BACKEND_HOST", "0.0.0.0")
    app.run(host=host, port=port, threaded=True)
