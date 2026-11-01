"""ABI calldata builders for the $ROB mint contract.

Pure, read-only encoding — this module NEVER signs a transaction, broadcasts, holds a key, or
touches the network. It turns (function, args) into the `data` hex that an agentic wallet signs and
submits. Selectors come from eth_utils.keccak; arguments are ABI-encoded with eth_abi so that each
uint256 / address / bytes32 is laid out as a 32-byte word and dynamic `bytes` goes in the tail,
exactly as the Solidity ABI expects.
"""
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

ZERO = "0x" + "0" * 40


def selector(sig: str) -> bytes:
    return keccak(text=sig)[:4]


def _as_bytes(x) -> bytes:
    if isinstance(x, (bytes, bytearray)):
        return bytes(x)
    if isinstance(x, str):
        return bytes.fromhex(x[2:] if x.startswith("0x") else x)
    raise TypeError("expected bytes or hex str")


def _as_bytes32(x) -> bytes:
    b = _as_bytes(x)
    if len(b) != 32:
        raise ValueError("bytes32 must be exactly 32 bytes")
    return b


def encode_call(sig: str, types, args) -> str:
    """0x-hex calldata = 4-byte selector(sig) || abi.encode(types, args)."""
    return "0x" + (selector(sig) + abi_encode(list(types), list(args))).hex()


# --- function signatures (must match RobMint.sol) -----------------------------------------------
SIG_MINT = "mint(uint256,uint256,uint256,uint256,bytes,uint256,uint256)"
SIG_GRANT_QUOTA = "grantQuota(uint256,uint256,bytes32,uint256,bytes)"

# view selectors used for status / quoting
SIG_QUOTE_ETH = "quoteEth(uint256)"
SIG_PRICE_CENTS = "priceCents(uint256)"
SIG_TOTAL_MINTED = "totalMinted()"
SIG_TOTAL_PAID = "totalPaid()"
SIG_OPENED = "opened()"
SIG_RESERVES = "reserves()"
SIG_FLOOR_VALUE = "floorValue()"
SIG_TOTAL_SHARES = "TOTAL_SHARES()"


def mint(shares, agent_id, nonce, deadline, sig, max_cost, min_tokens_out=0) -> str:
    """mint(shares, agentId, nonce, deadline, sig, maxCost, minTokensOut) calldata.
    `sig` is the 65-byte gate-signer voucher (bytes). `max_cost` is max wei the caller will pay.
    `min_tokens_out` is the minimum $ROB the caller accepts (slippage floor; 0 to disable)."""
    return encode_call(
        SIG_MINT,
        ["uint256", "uint256", "uint256", "uint256", "bytes", "uint256", "uint256"],
        [int(shares), int(agent_id), int(nonce), int(deadline), _as_bytes(sig), int(max_cost), int(min_tokens_out)])


def grant_quota(agent_id, extra, reason, deadline, sig) -> str:
    """grantQuota(agentId, extra, reason, deadline, sig) calldata.
    `reason` is a bytes32 (e.g. keccak256("SOCIAL")). `sig` is the 65-byte quota voucher."""
    return encode_call(
        SIG_GRANT_QUOTA,
        ["uint256", "uint256", "bytes32", "uint256", "bytes"],
        [int(agent_id), int(extra), _as_bytes32(reason), int(deadline), _as_bytes(sig)])


# --- view-call data builders (for eth_call reads over RPC) ----------------------------------------
def quote_eth_data(shares) -> str:
    return encode_call(SIG_QUOTE_ETH, ["uint256"], [int(shares)])


def price_cents_data(n) -> str:
    return encode_call(SIG_PRICE_CENTS, ["uint256"], [int(n)])


def _no_arg(sig: str) -> str:
    return "0x" + selector(sig).hex()


def total_minted_data() -> str:
    return _no_arg(SIG_TOTAL_MINTED)


def total_paid_data() -> str:
    return _no_arg(SIG_TOTAL_PAID)


def opened_data() -> str:
    return _no_arg(SIG_OPENED)


def reserves_data() -> str:
    return _no_arg(SIG_RESERVES)


def floor_value_data() -> str:
    return _no_arg(SIG_FLOOR_VALUE)


def total_shares_data() -> str:
    return _no_arg(SIG_TOTAL_SHARES)


def reason_hash(text: str) -> str:
    """keccak256 of a short label, as a 0x-hex bytes32 — used for quota `reason` (e.g. "SOCIAL")."""
    return "0x" + keccak(text=text).hex()


def balanceof_data(addr: str) -> str:
    """ERC721 balanceOf(address) calldata, used for the Agent-ID gate read on BNB Chain."""
    return "0x70a08231" + to_checksum_address(addr)[2:].lower().rjust(64, "0")


def owner_of_data(token_id) -> str:
    """ERC721 ownerOf(uint256) calldata — used to verify the caller actually owns the Agent-ID
    tokenId they claim (binds quota to the NFT, not just to holding >=1 in some wallet)."""
    return encode_call("ownerOf(uint256)", ["uint256"], [int(token_id)])


# --- on-chain signer reads (startup validation) ---------------------------------------------------
def gate_signer_data() -> str:
    """RobMint.gateSigner() -> address."""
    return _no_arg("gateSigner()")


def quota_signer_data() -> str:
    """RobMint.quotaSigner() -> address."""
    return _no_arg("quotaSigner()")


def token_of_owner_by_index_data(addr: str, index: int = 0) -> str:
    """ERC721Enumerable tokenOfOwnerByIndex(owner,index) — used to read the real Agent-ID tokenId
    when the registry supports enumeration."""
    return encode_call("tokenOfOwnerByIndex(address,uint256)",
                       ["address", "uint256"], [to_checksum_address(addr), int(index)])


# --- free/paid quota + refund + open reads (new mint mechanics) -----------------------------------
SIG_FIRST_MINT_TIME = "firstMintTime()"
SIG_OPEN_AT = "OPEN_AT()"
SIG_REFUND_WINDOW = "REFUND_WINDOW()"
SIG_FREE_SHARES = "FREE_SHARES()"
SIG_MAX_PAID = "MAX_PAID_PER_WALLET()"
SIG_FREE_QUOTA_OF = "freeQuotaOf(uint256)"
SIG_FREE_MINTED = "freeMinted(uint256)"
SIG_PAID_MINTED = "paidMinted(address)"
SIG_REFUND_SHARES = "refundShares(address)"
SIG_REFUND_PAID = "refundPaid(address)"
SIG_REFUND = "refund()"


def first_mint_time_data() -> str:
    return _no_arg(SIG_FIRST_MINT_TIME)


def open_at_data() -> str:
    return _no_arg(SIG_OPEN_AT)


def refund_window_data() -> str:
    return _no_arg(SIG_REFUND_WINDOW)


def free_shares_data() -> str:
    return _no_arg(SIG_FREE_SHARES)


def max_paid_data() -> str:
    return _no_arg(SIG_MAX_PAID)


def free_quota_of_data(agent_id) -> str:
    return encode_call(SIG_FREE_QUOTA_OF, ["uint256"], [int(agent_id)])


def free_minted_data(agent_id) -> str:
    return encode_call(SIG_FREE_MINTED, ["uint256"], [int(agent_id)])


def paid_minted_data(addr: str) -> str:
    return encode_call(SIG_PAID_MINTED, ["address"], [to_checksum_address(addr)])


def refund_shares_data(addr: str) -> str:
    return encode_call(SIG_REFUND_SHARES, ["address"], [to_checksum_address(addr)])


def refund_paid_data(addr: str) -> str:
    return encode_call(SIG_REFUND_PAID, ["address"], [to_checksum_address(addr)])


def refund_tx_data() -> str:
    """refund() calldata — permissionless, no args. The wallet sends this to reclaim 100%."""
    return _no_arg(SIG_REFUND)


def minted_topic() -> str:
    """topic0 of Minted(address indexed minter, uint256 indexed agentId, uint256 firstSerial,
    uint256 shares, uint256 paid) — for eth_getLogs ledger scans."""
    return "0x" + keccak(text="Minted(address,uint256,uint256,uint256,uint256)").hex()


# --- dividend (ETH-to-holders) reads/claims on the $ROB token ------------------------------------
SIG_WITHDRAW_DIVIDEND = "withdrawDividend()"
SIG_WITHDRAWABLE_DIVIDEND_OF = "withdrawableDividendOf(address)"
SIG_DIVIDEND_OF = "dividendOf(address)"
SIG_SWEEP_DIVIDENDS = "sweepDividends()"
SIG_TOTAL_DIVIDENDS = "totalDividendsDistributed()"


def withdraw_dividend_data() -> str:
    """withdrawDividend() — holder pulls their accrued ETH. No args."""
    return _no_arg(SIG_WITHDRAW_DIVIDEND)


def withdrawable_dividend_of_data(addr: str) -> str:
    return encode_call(SIG_WITHDRAWABLE_DIVIDEND_OF, ["address"], [to_checksum_address(addr)])


def dividend_of_data(addr: str) -> str:
    return encode_call(SIG_DIVIDEND_OF, ["address"], [to_checksum_address(addr)])


def sweep_dividends_data() -> str:
    """sweepDividends() — permissionless; converts the 2% dividend pot to ETH + distributes."""
    return _no_arg(SIG_SWEEP_DIVIDENDS)


def total_dividends_distributed_data() -> str:
    return _no_arg(SIG_TOTAL_DIVIDENDS)
