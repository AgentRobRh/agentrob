"""AgentRob voucher signer — EIP-712 vouchers for the $ROB mint contract.

The signer keys ONLY authorize mints and quota grants (they sign vouchers). They never hold or move
user funds — the user signs the actual on-chain transaction with their own agentic wallet. This
backend is non-custodial: it authorizes, it does not execute.

There are TWO independent keys, so a leak of one does not grant the other's power:
  - the MINT (gate) key signs Mint vouchers,
  - the QUOTA key signs Quota (invite/social) grants.

The mint key is loaded from ROB_SIGNER_KEY (a 0x-hex private key) or, if unset, from the file at
ROB_SIGNER_KEYFILE (default ./secrets/signer.key). The quota key is loaded from
ROB_QUOTA_SIGNER_KEY or, if unset, from ROB_QUOTA_SIGNER_KEYFILE (default
./secrets/quota_signer.key).

Keys are NEVER auto-generated: if a key is configured by neither env nor an existing keyfile, loading
raises and the service refuses to start. (Silently minting a fresh key would brick the contract with
BadSignature once it is wired to a different on-chain gateSigner/quotaSigner.) The private keys are
NEVER logged, printed, or returned to any caller — only the derived public addresses are exposed.

The EIP-712 digest is built by hand with eth_abi.encode + keccak so that it matches RobMint.sol
byte for byte:

    DOMAIN_TYPEHASH   = keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
    domainSeparator   = keccak256(abi.encode(DOMAIN_TYPEHASH, keccak256(name), keccak256(version), chainId, verifyingContract))
    MINT_TYPEHASH     = keccak256("Mint(address minter,uint256 agentId,uint256 nonce,uint256 deadline)")
    QUOTA_TYPEHASH    = keccak256("Quota(uint256 agentId,uint256 extra,bytes32 reason,uint256 deadline)")
    structHash        = keccak256(abi.encode(TYPEHASH, ...fields...))
    digest            = keccak256(0x1901 || domainSeparator || structHash)
"""
import os

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

# --- key location (env first, then file; never the key itself in code) ---------------------------
# Mint (gate) key and quota key are separate on purpose (see module docstring).
MINT_KEYFILE = os.environ.get("ROB_SIGNER_KEYFILE", "./secrets/signer.key")
QUOTA_KEYFILE = os.environ.get("ROB_QUOTA_SIGNER_KEYFILE", "./secrets/quota_signer.key")

# --- EIP-712 domain constants (must match RobMint.sol exactly) -----------------------------------
DOMAIN_NAME = "AgentRobMint"
DOMAIN_VERSION = "1"
DEFAULT_CHAIN_ID = int(os.environ.get("ROB_CHAIN_ID", "4663"))  # Robinhood Chain
ZERO_ADDR = "0x" + "0" * 40

DOMAIN_TYPEHASH = keccak(
    b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
MINT_TYPEHASH = keccak(b"Mint(address minter,uint256 agentId,uint256 nonce,uint256 deadline)")
QUOTA_TYPEHASH = keccak(b"Quota(uint256 agentId,uint256 extra,bytes32 reason,uint256 deadline)")


def _load_key(env_var: str, keyfile: str, label: str) -> bytes:
    """Return a signer private key as bytes. Priority: env var, then the keyfile. NEVER generates
    a key: if neither source has one, raise so the service refuses to start (a silently-minted key
    would not match the on-chain gateSigner/quotaSigner and would brick signing with BadSignature).
    The returned value is used only to sign in-process and is never logged or exposed to callers."""
    env_key = os.environ.get(env_var, "").strip()
    if env_key:
        return bytes.fromhex(env_key[2:] if env_key.startswith("0x") else env_key)
    if os.path.exists(keyfile):
        with open(keyfile) as f:
            raw = f.read().strip()
        if not raw:
            raise RuntimeError(f"{label} keyfile {keyfile} is empty — refusing to start")
        return bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
    raise RuntimeError(
        f"{label} key missing: set {env_var} or create {keyfile} (0600). "
        "Keys are never auto-generated.")


def _mint_key() -> bytes:
    return _load_key("ROB_SIGNER_KEY", MINT_KEYFILE, "mint/gate signer")


def _quota_key() -> bytes:
    return _load_key("ROB_QUOTA_SIGNER_KEY", QUOTA_KEYFILE, "quota signer")


def signer_address() -> str:
    """Public address of the mint/gate signer. Safe to expose."""
    return Account.from_key(_mint_key()).address


def quota_signer_address() -> str:
    """Public address of the quota signer. Safe to expose."""
    return Account.from_key(_quota_key()).address


# --- EIP-712 digest construction ------------------------------------------------------------------
def domain_separator(verifying_contract: str, chain_id: int) -> bytes:
    return keccak(abi_encode(
        ["bytes32", "bytes32", "bytes32", "uint256", "address"],
        [DOMAIN_TYPEHASH, keccak(DOMAIN_NAME.encode()), keccak(DOMAIN_VERSION.encode()),
         int(chain_id), to_checksum_address(verifying_contract or ZERO_ADDR)]))


def _digest(struct_hash: bytes, verifying_contract: str, chain_id: int) -> bytes:
    sep = domain_separator(verifying_contract, chain_id)
    return keccak(b"\x19\x01" + sep + struct_hash)


def mint_struct_hash(minter: str, agent_id: int, nonce: int, deadline: int) -> bytes:
    return keccak(abi_encode(
        ["bytes32", "address", "uint256", "uint256", "uint256"],
        [MINT_TYPEHASH, to_checksum_address(minter), int(agent_id), int(nonce), int(deadline)]))


def quota_struct_hash(agent_id: int, extra: int, reason: bytes, deadline: int) -> bytes:
    reason = _as_bytes32(reason)
    return keccak(abi_encode(
        ["bytes32", "uint256", "uint256", "bytes32", "uint256"],
        [QUOTA_TYPEHASH, int(agent_id), int(extra), reason, int(deadline)]))


def _as_bytes32(x) -> bytes:
    if isinstance(x, (bytes, bytearray)):
        b = bytes(x)
    elif isinstance(x, str):
        b = bytes.fromhex(x[2:] if x.startswith("0x") else x)
    else:
        raise TypeError("reason must be bytes or hex str")
    if len(b) != 32:
        raise ValueError("reason must be exactly 32 bytes")
    return b


def _sign_digest(digest: bytes, key: bytes) -> str:
    """Sign a 32-byte digest with `key` and return r||s||v as a 0x-prefixed 65-byte hex string."""
    signed = Account.unsafe_sign_hash(digest, key)
    sig = signed.signature
    return "0x" + (bytes(sig).hex())


# --- public signing API ---------------------------------------------------------------------------
def sign_mint(minter: str, agent_id: int, nonce: int, deadline: int,
              verifying_contract: str, chain_id: int = DEFAULT_CHAIN_ID) -> str:
    """Sign a Mint voucher with the MINT (gate) key. Returns a 65-byte r||s||v signature (0x-hex)."""
    digest = _digest(mint_struct_hash(minter, agent_id, nonce, deadline),
                     verifying_contract, chain_id)
    return _sign_digest(digest, _mint_key())


def sign_quota(agent_id: int, extra: int, reason, deadline: int,
               verifying_contract: str, chain_id: int = DEFAULT_CHAIN_ID) -> str:
    """Sign a Quota voucher with the QUOTA key. Returns a 65-byte r||s||v signature (0x-hex)."""
    digest = _digest(quota_struct_hash(agent_id, extra, reason, deadline),
                     verifying_contract, chain_id)
    return _sign_digest(digest, _quota_key())


# --- recovery (self-test / verification only) -----------------------------------------------------
def recover_mint(minter: str, agent_id: int, nonce: int, deadline: int, signature: str,
                 verifying_contract: str, chain_id: int = DEFAULT_CHAIN_ID) -> str:
    digest = _digest(mint_struct_hash(minter, agent_id, nonce, deadline),
                     verifying_contract, chain_id)
    return Account._recover_hash(digest, signature=signature)


def recover_quota(agent_id: int, extra: int, reason, deadline: int, signature: str,
                  verifying_contract: str, chain_id: int = DEFAULT_CHAIN_ID) -> str:
    digest = _digest(quota_struct_hash(agent_id, extra, reason, deadline),
                     verifying_contract, chain_id)
    return Account._recover_hash(digest, signature=signature)
