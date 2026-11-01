// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {IDexPair, IWETH, DexMath} from "./interfaces/IDex.sol";
import {IAggregatorV3} from "./interfaces/IOracle.sol";
import {RobToken} from "./RobToken.sol";

/// @title ROB mint
/// @notice Agent-gated fair mint. Shares are sold on a rising price ladder. Up to the
///         open threshold each payment parks its liquidity share and mints inventory
///         for the pool; the threshold share opens the official pair, sends the LP to
///         the dead address, and switches trading on. After the pool opens, every mint
///         buys at a small discount to the pool price and pairs its liquidity share
///         into the pool, again burning the LP, while a floor line burns pool inventory
///         to keep the price from falling below a level set by total paid.
///
///         Eligibility is proven by an EIP-712 voucher from the gate signer (the signer
///         checks the caller's Agent-ID off-chain). The voucher binds the caller, the
///         Agent-ID and a nonce, so it cannot be replayed or used by another wallet.
contract RobMint is ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ------------------------------------------------------------- constants
    uint256 public constant TOTAL_SHARES = 4_440_000;
    uint256 public constant MAX_SUPPLY = 444_000_000_000 ether; // 444B

    uint256 public constant USER_SHARE = 50_000 ether; // minter tokens per share, pre-open
    uint256 public constant POOL_SHARE = 40_000 ether; // pool inventory per share, pre-open

    // Payment split: 80% to the pool, 20% to the treasury.
    uint256 public constant TREASURY_BPS = 2_000;
    // Post-open mint buys at 99% of the pool price.
    uint256 public constant MINT_PRICE_BPS = 9_900;

    // Floor line = totalPaid * FLOOR_LINE / LINE_DENOM. These three are deploy params
    // (tuned to the token's price scale); see immutables below.
    uint256 public constant RISE_NUM = 1_008; // at most +0.8% per transaction
    uint256 public constant RISE_DEN = 1_000;

    uint256 public constant TAX_SWAP_BPS = 10; // at most 0.1% of pool per swap
    uint256 public constant FREE_PER_ID = 1; // base free shares per Agent-ID (1, lifted by invites/tasks)
    uint256 public constant MAX_PER_TX = 10;
    uint256 public constant MAX_EXTRA_QUOTA = 90; // on-chain cap on signer-granted bonus FREE shares
    uint256 public constant PRICE_DIV = 4; // price-ladder divisor: $0.50 entry (200c base / 4)
    uint256 public constant REFUND_WINDOW = 14 days; // if the pool never opens, pre-open mints refund after this (experimental: longer runway)
    address public constant DEAD = 0x000000000000000000000000000000000000dEaD;

    // EIP-712
    bytes32 private constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 private constant MINT_TYPEHASH =
        keccak256("Mint(address minter,uint256 agentId,uint256 nonce,uint256 deadline)");
    bytes32 private constant QUOTA_TYPEHASH =
        keccak256("Quota(uint256 agentId,uint256 extra,bytes32 reason,uint256 deadline)");

    // ------------------------------------------------------------- immutables
    RobToken public immutable token;
    IDexPair public immutable pair;
    address public immutable weth;
    address public immutable gateSigner; // off-chain mint-eligibility signer
    address public immutable quotaSigner; // separate signer for invite/social quota grants
    address public immutable treasury;
    uint256 public immutable swapFeeBps;
    uint256 public immutable FREE_SHARES; // first N shares are free ($0); the rest are paid on the ladder
    uint256 public immutable MAX_PAID_PER_WALLET; // paid shares cap, per wallet
    uint256 public immutable OPEN_AT; // threshold share that opens the pool
    uint256 public immutable OPEN_TOKENS; // pool inventory seeded at open (= OPEN_AT * POOL_SHARE)
    uint256 public immutable FLOOR_LINE; // floor-line numerator (tuned to price scale)
    uint256 public immutable LINE_DENOM; // floor-line denominator
    uint256 public immutable POOL_TOKEN_FLOOR; // min pool token reserve kept by floor burns
    // USD pricing oracle (shares are priced in USD, paid in ETH converted at mint time).
    address public immutable ethUsdFeed; // Chainlink ETH/USD aggregator proxy
    address public immutable sequencerFeed; // L2 sequencer uptime feed (0 to skip)
    uint256 public immutable feedHeartbeat; // max age of a fresh answer
    uint256 public immutable feedMin; // sanity floor for the answer
    uint256 public immutable feedMax; // sanity ceiling for the answer
    uint256 public immutable sequencerGrace; // grace period after sequencer restart
    bool private immutable _tokenIs0;

    // ------------------------------------------------------------- state
    uint256 public totalMinted;
    uint256 public totalPaid;
    bool public paused = true;
    bool public seeded;
    bool public renounced;
    uint256 public pendingQuote; // parked quote (WETH) for the opening pool
    uint256 public pendingTokens; // parked token inventory for the opening pool
    uint256 public openedTime;
    uint256 public lastTaxSwapBlock;
    uint256 public firstMintTime; // timestamp of the first mint; starts the refund clock

    mapping(uint256 => uint256) public freeMinted; // free shares taken, by Agent-ID
    mapping(address => uint256) public paidMinted; // paid shares taken, by wallet
    mapping(uint256 => uint256) public extraQuota; // invite/social bonus FREE allocation, by Agent-ID
    mapping(bytes32 => bool) public spentNonce; // mint voucher replay guard
    mapping(bytes32 => bool) public spentQuota; // quota grant replay guard
    // Pre-open refund ledger: shares minted and ETH paid, per minter, while the pool is closed.
    // 100% of pre-open payments are escrowed here; if the pool never opens, these are refundable.
    mapping(address => uint256) public refundShares;
    mapping(address => uint256) public refundPaid;

    event Minted(address indexed minter, uint256 indexed agentId, uint256 firstSerial, uint256 shares, uint256 paid);
    event QuotaGranted(uint256 indexed agentId, uint256 extra, bytes32 reason);
    event Opened(uint256 quoteIn, uint256 tokensIn, uint256 lp);
    event PoolBurned(uint256 atShare, uint256 amount);
    event TaxSwapped(uint256 tokensIn, uint256 ethOut);
    event Seeded();
    event PausedSet(bool paused);
    event Renounced();
    event Refunded(address indexed minter, uint256 shares, uint256 amount);

    error NotTreasury();
    error AdminRenounced();
    error Paused();
    error NotSeeded();
    error BadShares();
    error SoldOut();
    error Quota();
    error OverMaxCost();
    error BadPayment();
    error StalePrice();
    error BadSignature();
    error Slippage();
    error BadConfig();
    error Replayed();
    error NotOpen();
    error RefundClosed();
    error NothingToRefund();
    error NothingToSwap();
    error SwappedThisBlock();
    error OnlySelf();
    error ZeroAddress();
    error PayoutFailed();

    struct Config {
        address token;
        address weth;
        address gateSigner;
        address quotaSigner;
        address treasury;
        uint256 swapFeeBps;
        uint256 freeShares;
        uint256 maxPaidPerWallet;
        uint256 openAt;
        uint256 floorLineNum;
        uint256 floorLineDenom;
        uint256 poolTokenFloor;
        address ethUsdFeed;
        address sequencerFeed;
        uint256 feedHeartbeat;
        uint256 feedMin;
        uint256 feedMax;
        uint256 sequencerGrace;
    }

    constructor(Config memory c) {
        if (
            c.token == address(0) || c.weth == address(0) || c.gateSigner == address(0)
                || c.quotaSigner == address(0) || c.treasury == address(0) || c.ethUsdFeed == address(0)
        ) revert ZeroAddress();
        if (c.openAt == 0 || c.openAt > TOTAL_SHARES || c.floorLineDenom == 0) revert BadConfig();
        if (c.freeShares >= c.openAt) revert BadConfig(); // free must end before the pool opens so paid shares fund it
        if (c.maxPaidPerWallet == 0) revert BadConfig();
        if (c.feedMin == 0 || c.feedMin >= c.feedMax || c.feedHeartbeat == 0) revert BadConfig();
        token = RobToken(payable(c.token));
        pair = IDexPair(token.pair());
        weth = c.weth;
        gateSigner = c.gateSigner;
        quotaSigner = c.quotaSigner;
        treasury = c.treasury;
        swapFeeBps = c.swapFeeBps;
        FREE_SHARES = c.freeShares;
        MAX_PAID_PER_WALLET = c.maxPaidPerWallet;
        OPEN_AT = c.openAt;
        OPEN_TOKENS = c.openAt * POOL_SHARE;
        FLOOR_LINE = c.floorLineNum;
        LINE_DENOM = c.floorLineDenom;
        POOL_TOKEN_FLOOR = c.poolTokenFloor;
        ethUsdFeed = c.ethUsdFeed;
        sequencerFeed = c.sequencerFeed;
        feedHeartbeat = c.feedHeartbeat;
        feedMin = c.feedMin;
        feedMax = c.feedMax;
        sequencerGrace = c.sequencerGrace;
        _tokenIs0 = pair.token0() == address(token);
    }

    receive() external payable {
        if (msg.sender != weth) revert BadPayment();
    }

    // ------------------------------------------------------------- views
    /// @notice Price of share `n` in whole US cents (scaled by PRICE_DIV in priceUsd). The first
    ///         FREE_SHARES are free ($0); the rest climb a 247-tier ladder from $0.50 that finishes
    ///         at 3,228 cents ($8.07) on the final share. It rises slowly: the first 9 tiers ramp
    ///         $0.50 -> $1.00 (the share that opens the pool, OPEN_AT), then 1,000-share tiers to
    ///         $2.00, then 10,000-share tiers to $4.25, then 60,000-share tiers to $8.07.
    function priceCents(uint256 n) public view returns (uint256) {
        if (n <= FREE_SHARES) return 0;
        uint256 k = n - FREE_SHARES; // 1-based paid position
        if (k <= 9_000) return _band(k, 1, 200, 400, 1_000, 9); // $0.50 -> $1.00 (opens at $1/share)
        if (k <= 99_000) return _band(k, 9_001, 400, 800, 1_000, 90); // $1.00 -> $2.00
        if (k <= 999_000) return _band(k, 99_001, 800, 1_700, 10_000, 90); // $2.00 -> $4.25
        return _band(k, 999_001, 1_700, 3_228, 60_000, 58); // $4.25 -> $8.07 (final share = $8.07)
    }

    /// @dev Interpolates a band's price in whole cents from `base` (its first tier) to `cap`
    ///      (its last tier) across `blocks` tiers of `size` paid shares each.
    function _band(uint256 k, uint256 from, uint256 base, uint256 cap, uint256 size, uint256 blocks)
        private
        pure
        returns (uint256)
    {
        if (blocks <= 1) return base; // single-tier band: flat price (guards blocks-1 div-by-zero)
        uint256 i = (k - from) / size; // 0-based tier index within the band
        if (i >= blocks) i = blocks - 1;
        return base + (cap - base) * i / (blocks - 1);
    }

    /// @notice Price of share `n` in USD, 18 decimals. The cents ladder is scaled by
    ///         PRICE_DIV so paid shares start at $0.50 (= 200c base / 4) and rise to $8.07.
    function priceUsd(uint256 n) public view returns (uint256) {
        return priceCents(n) * 1e16 / PRICE_DIV;
    }

    /// @notice Total USD cost (18 dec) for the next `shares`.
    function costUsd(uint256 shares) public view returns (uint256 cost) {
        uint256 n = totalMinted;
        for (uint256 i; i < shares; i++) {
            cost += priceUsd(n + 1 + i);
        }
    }

    /// @notice ETH (wei) the next `shares` cost at the current oracle price.
    function quoteEth(uint256 shares) public view returns (uint256) {
        (uint256 px, uint256 unit) = _ethUsd();
        return _costEth(shares, px, unit);
    }

    /// @dev Reads and validates the ETH/USD feed; returns (answer, 10**decimals).
    function _ethUsd() internal view returns (uint256 px, uint256 unit) {
        if (sequencerFeed != address(0)) {
            (, int256 up, uint256 since,,) = IAggregatorV3(sequencerFeed).latestRoundData();
            // up == 0 means the sequencer is live; require a valid round up past the grace period.
            if (up != 0 || since == 0 || block.timestamp - since <= sequencerGrace) revert StalePrice();
        }
        (uint80 roundId, int256 answer,, uint256 updatedAt, uint80 answeredInRound) =
            IAggregatorV3(ethUsdFeed).latestRoundData();
        if (
            answer <= 0 || updatedAt == 0 || answeredInRound < roundId
                || block.timestamp > updatedAt + feedHeartbeat
        ) revert StalePrice();
        px = uint256(answer);
        if (px < feedMin || px > feedMax) revert StalePrice();
        unit = 10 ** IAggregatorV3(ethUsdFeed).decimals();
    }

    /// @dev ETH wei for a USD (18-dec) amount at price `px` scaled by `unit` (= 10**decimals).
    function _ethAt(uint256 usd18, uint256 px, uint256 unit) internal pure returns (uint256) {
        return usd18 * unit / px;
    }

    /// @dev ETH cost summed share-by-share so it matches the per-share settlement exactly.
    function _costEth(uint256 shares, uint256 px, uint256 unit) internal view returns (uint256 cost) {
        uint256 n = totalMinted;
        for (uint256 i; i < shares; i++) {
            cost += _ethAt(priceUsd(n + 1 + i), px, unit);
        }
    }

    /// @notice Free-share allowance for an Agent-ID: the base FREE_PER_ID plus any
    ///         invite/social bonus granted on-chain. Only limits shares priced at $0.
    function freeQuotaOf(uint256 agentId) public view returns (uint256) {
        return FREE_PER_ID + extraQuota[agentId];
    }

    function opened() public view returns (bool) {
        return totalMinted >= OPEN_AT;
    }

    function reserves() public view returns (uint256 rQuote, uint256 rToken) {
        (uint112 r0, uint112 r1,) = pair.getReserves();
        (rToken, rQuote) = _tokenIs0 ? (uint256(r0), uint256(r1)) : (uint256(r1), uint256(r0));
    }

    function circulating() public view returns (uint256) {
        return token.totalSupply() - token.balanceOf(address(pair)) - token.balanceOf(DEAD)
            - token.balanceOf(address(this));
    }

    function floorValue() public view returns (uint256) {
        return totalPaid * FLOOR_LINE / LINE_DENOM;
    }

    // ------------------------------------------------------------- treasury admin
    function _onlyTreasury() private view {
        if (msg.sender != treasury) revert NotTreasury();
        if (renounced) revert AdminRenounced();
    }

    function seed() external {
        _onlyTreasury();
        seeded = true;
        emit Seeded();
    }

    function setPaused(bool next) external {
        _onlyTreasury();
        if (!next && !seeded) revert NotSeeded();
        paused = next;
        emit PausedSet(next);
    }

    function setPoolBlocked(address pool, bool blocked) external {
        _onlyTreasury();
        token.setPoolBlocked(pool, blocked);
    }

    /// @notice Gives up pause and pool-blocking for good. Minting keeps running.
    function renounce() external {
        _onlyTreasury();
        if (!seeded) revert NotSeeded();
        renounced = true;
        paused = false;
        token.disableBlocks();
        emit Renounced();
    }

    // ------------------------------------------------------------- quota grants
    /// @notice Credit an invite or social bonus. The gate signer attests the grant so
    ///         off-chain invite/social verification stays flexible while the quota
    ///         ledger lives on-chain.
    function grantQuota(uint256 agentId, uint256 extra, bytes32 reason, uint256 deadline, bytes calldata sig)
        external
    {
        if (block.timestamp > deadline) revert BadSignature();
        if (extra == 0 || extraQuota[agentId] + extra > MAX_EXTRA_QUOTA) revert Quota();
        bytes32 digest = _digest(keccak256(abi.encode(QUOTA_TYPEHASH, agentId, extra, reason, deadline)));
        if (spentQuota[digest]) revert Replayed();
        _requireSigner(digest, sig, quotaSigner);
        spentQuota[digest] = true;
        extraQuota[agentId] += extra;
        emit QuotaGranted(agentId, extra, reason);
    }

    // ------------------------------------------------------------- mint
    /// @param shares   number of shares to mint (1..MAX_PER_TX)
    /// @param agentId  the caller's Agent-ID, attested by the voucher
    /// @param nonce    voucher nonce (single use)
    /// @param deadline voucher expiry
    /// @param sig      gate-signer EIP-712 signature over Mint(minter,agentId,nonce,deadline)
    /// @param maxCost  max wei the caller will pay
    function mint(
        uint256 shares,
        uint256 agentId,
        uint256 nonce,
        uint256 deadline,
        bytes calldata sig,
        uint256 maxCost,
        uint256 minTokensOut
    ) external payable nonReentrant {
        if (paused) revert Paused();
        if (shares == 0 || shares > MAX_PER_TX) revert BadShares();
        if (totalMinted + shares > TOTAL_SHARES) revert SoldOut();
        if (block.timestamp > deadline) revert BadSignature();

        bytes32 digest = _digest(keccak256(abi.encode(MINT_TYPEHASH, msg.sender, agentId, nonce, deadline)));
        if (spentNonce[digest]) revert Replayed();
        _requireSigner(digest, sig, gateSigner);
        spentNonce[digest] = true;

        // Split the request into free (price $0, the first FREE_SHARES globally) and paid
        // shares, then enforce the two caps: free is per Agent-ID, paid is per wallet.
        uint256 firstShare = totalMinted + 1;
        uint256 freeCount;
        if (firstShare <= FREE_SHARES) {
            uint256 lastFree = firstShare + shares - 1;
            if (lastFree > FREE_SHARES) lastFree = FREE_SHARES;
            freeCount = lastFree - firstShare + 1;
        }
        uint256 paidCount = shares - freeCount;
        if (freeCount > 0 && freeMinted[agentId] + freeCount > freeQuotaOf(agentId)) revert Quota();
        if (paidCount > 0 && paidMinted[msg.sender] + paidCount > MAX_PAID_PER_WALLET) revert Quota();
        freeMinted[agentId] += freeCount;
        paidMinted[msg.sender] += paidCount;

        // USD-priced, paid in ETH at the current oracle price. `maxCost` is max ETH wei.
        (uint256 px, uint256 unit) = _ethUsd();
        uint256 cost = _costEth(shares, px, unit);
        if (cost > maxCost || msg.value < cost) revert OverMaxCost();

        IWETH(weth).deposit{value: cost}();
        uint256 got = _mintShares(agentId, shares, cost, px, unit);
        if (got < minTokensOut) revert Slippage();

        // Refund surplus ETH last (checks-effects-interactions).
        uint256 surplus = msg.value - cost;
        if (surplus > 0) {
            (bool ok,) = msg.sender.call{value: surplus}("");
            if (!ok) revert PayoutFailed();
        }
    }

    // ------------------------------------------------------------- refund
    /// @notice If the pool never opens, every pre-open minter can reclaim 100% of what they
    ///         paid once REFUND_WINDOW has passed since the first mint. The minter's tokens
    ///         and the matching parked pool inventory are burned, the escrowed ETH is
    ///         returned, and the shares are released. Permanently off once the pool opens.
    function refund() external nonReentrant {
        if (opened()) revert RefundClosed();
        if (firstMintTime == 0 || block.timestamp < firstMintTime + REFUND_WINDOW) revert RefundClosed();
        uint256 shares = refundShares[msg.sender];
        uint256 amount = refundPaid[msg.sender];
        if (shares == 0 || amount == 0) revert NothingToRefund();

        // effects
        refundShares[msg.sender] = 0;
        refundPaid[msg.sender] = 0;
        totalMinted -= shares;
        totalPaid -= amount;
        pendingQuote -= amount;
        pendingTokens -= shares * POOL_SHARE;

        // un-mint the minter's stake and the parked pool inventory for these shares
        token.burnFromHolder(msg.sender, shares * USER_SHARE);
        token.burnFromHolder(address(this), shares * POOL_SHARE);

        // return the escrowed ETH (unwrap WETH), interactions last
        IWETH(weth).withdraw(amount);
        (bool ok,) = msg.sender.call{value: amount}("");
        if (!ok) revert PayoutFailed();
        emit Refunded(msg.sender, shares, amount);
    }

    struct Pool {
        uint256 q; // quote reserve (working)
        uint256 t; // token reserve (working)
        uint256 startQ;
        uint256 startT;
        uint256 circ;
        uint256 supply;
        uint256 addQ; // quote to add to the pool this tx
        uint256 addT; // tokens to add to the pool this tx
        uint256 burnT; // pool tokens to burn this tx (floor line)
        uint256 userT; // tokens owed to the minter this tx
    }

    function _mintShares(uint256 agentId, uint256 shares, uint256 cost, uint256 px, uint256 unit)
        private
        returns (uint256 minted)
    {
        if (firstMintTime == 0) firstMintTime = block.timestamp;
        uint256 first = totalMinted + 1;
        uint256 toTreasury;
        uint256 preShares; // pre-open shares this tx (refundable)
        uint256 prePaid; // pre-open ETH this tx (refundable)
        Pool memory p;
        bool live = opened();
        if (live) _loadPool(p);

        for (uint256 n = first; n < first + shares; n++) {
            uint256 pay = _ethAt(priceUsd(n), px, unit);
            totalPaid += pay;
            if (n <= OPEN_AT) {
                // Pre-open: escrow the FULL payment. The treasury cut is deferred to _open()
                // so a launch that never opens can refund 100% of what each minter paid.
                p.userT += USER_SHARE;
                pendingQuote += pay;
                pendingTokens += POOL_SHARE;
                token.mint(address(this), POOL_SHARE);
                preShares += 1;
                prePaid += pay;
                if (n == OPEN_AT) {
                    token.mint(msg.sender, p.userT);
                    minted += p.userT;
                    p.userT = 0;
                    _open();
                    live = true;
                    _loadPool(p);
                }
            } else {
                uint256 cut = pay * TREASURY_BPS / 10_000;
                toTreasury += cut;
                _curveShare(p, n, pay, pay - cut);
            }
        }
        totalMinted = first + shares - 1;
        // Record refundable stake only if the pool did NOT open in this tx. If it opened,
        // the pre-open funds are committed to the pool and refunds are permanently off.
        if (preShares > 0 && !live) {
            refundShares[msg.sender] += preShares;
            refundPaid[msg.sender] += prePaid;
        }

        if (p.userT > 0) {
            token.mint(msg.sender, p.userT);
            minted += p.userT;
        }
        if (live && p.addQ > 0) _settle(p);
        if (toTreasury > 0) IERC20(weth).safeTransfer(treasury, toTreasury);
        emit Minted(msg.sender, agentId, first, shares, cost);

        if (live && lastTaxSwapBlock != block.number && token.balanceOf(address(this)) > 0) {
            try this.swapTaxSelf() {} catch {}
        }
    }

    function _loadPool(Pool memory p) private view {
        (p.q, p.t) = reserves();
        p.startQ = p.q;
        p.startT = p.t;
        p.circ = circulating();
        p.supply = token.totalSupply();
    }

    /// @dev Settles one post-open share against the working pool: mint tokens to the
    ///      buyer at 99% of pool price, pair the liquidity share in at pool price, then
    ///      apply the floor line by reducing the token reserve (burn) toward the target,
    ///      capped so the price rises at most RISE_NUM/RISE_DEN per transaction and never
    ///      below POOL_TOKEN_FLOOR. Equivalent to settling share by share.
    function _curveShare(Pool memory p, uint256 n, uint256 pay, uint256 lpQuote) private {
        uint256 userT = pay * p.t * 10_000 / (p.q * MINT_PRICE_BPS);
        uint256 lpT = (lpQuote * p.t + p.q - 1) / p.q;
        p.supply += userT + lpT;
        if (p.supply > MAX_SUPPLY) revert SoldOut();
        p.userT += userT;
        p.circ += userT;
        p.q += lpQuote;
        p.t += lpT;
        p.addQ += lpQuote;
        p.addT += lpT;

        // Floor line: keep circulating value (priced in quote) at or above the line.
        uint256 line = totalPaid * FLOOR_LINE / LINE_DENOM;
        if (p.circ * p.q / p.t >= line) return;
        uint256 byTarget = p.q * p.circ / line;
        uint256 byRise = (p.q * p.startT * RISE_DEN + p.startQ * RISE_NUM - 1) / (p.startQ * RISE_NUM);
        uint256 next = byTarget > byRise ? byTarget : byRise;
        if (next < POOL_TOKEN_FLOOR) next = POOL_TOKEN_FLOOR;
        if (next >= p.t) return;
        uint256 burn = p.t - next;
        p.t = next;
        p.burnT += burn;
        emit PoolBurned(n, burn);
    }

    function _settle(Pool memory p) private {
        IERC20(weth).safeTransfer(address(pair), p.addQ);
        token.mint(address(pair), p.addT);
        pair.mint(DEAD);
        if (p.burnT > 0) token.burnFromPair(p.burnT);
        pair.sync();
    }

    function _open() private {
        uint256 topUp = OPEN_TOKENS - pendingTokens;
        if (topUp > 0) token.mint(address(this), topUp);
        uint256 escrowed = pendingQuote; // 100% of pre-open payments, held in escrow
        pendingQuote = 0;
        pendingTokens = 0;
        uint256 treasuryCut = escrowed * TREASURY_BPS / 10_000; // the deferred 20%, paid now
        uint256 quoteIn = escrowed - treasuryCut; // 80% seeds the pool
        if (treasuryCut > 0) IERC20(weth).safeTransfer(treasury, treasuryCut);
        // Seed the pool with exactly `quoteIn`. If anyone donated WETH to the empty pair before
        // open, send only the shortfall so the opening price stays authoritative (not inflated by
        // the donation); the escrow we did not need to send then goes to the treasury. (audit L1)
        uint256 donated = IERC20(weth).balanceOf(address(pair));
        uint256 seed = quoteIn > donated ? quoteIn - donated : 0;
        if (seed > 0) IERC20(weth).safeTransfer(address(pair), seed);
        uint256 leftover = quoteIn - seed;
        if (leftover > 0) IERC20(weth).safeTransfer(treasury, leftover);
        IERC20(address(token)).safeTransfer(address(pair), OPEN_TOKENS);
        uint256 lp = pair.mint(DEAD);
        token.enableTrading();
        pair.sync();
        openedTime = block.timestamp;
        emit Opened(quoteIn, OPEN_TOKENS, lp);
    }

    // ------------------------------------------------------------- tax swap
    /// @notice Swap held tax tokens to ETH for the treasury, once per block, at most
    ///         0.1% of the pool token reserve each time.
    function swapTax() external nonReentrant {
        if (!opened()) revert NotOpen();
        if (lastTaxSwapBlock == block.number) revert SwappedThisBlock();
        _swapTax();
    }

    function swapTaxSelf() external {
        if (msg.sender != address(this)) revert OnlySelf();
        _swapTax();
    }

    function _swapTax() private {
        (, uint256 rToken) = reserves();
        uint256 amount = token.balanceOf(address(this));
        uint256 cap = rToken * TAX_SWAP_BPS / 10_000;
        if (amount > cap) amount = cap;
        if (amount == 0) revert NothingToSwap();
        lastTaxSwapBlock = block.number;
        IERC20(address(token)).safeTransfer(address(pair), amount);
        uint256 before = IERC20(weth).balanceOf(address(this));
        _swapOnPair(address(token), address(this));
        uint256 wethOut = IERC20(weth).balanceOf(address(this)) - before;
        IERC20(weth).safeTransfer(treasury, wethOut);
        emit TaxSwapped(amount, wethOut);
    }

    function _swapOnPair(address tokenIn, address to) private {
        (uint112 r0, uint112 r1,) = pair.getReserves();
        bool inIs0 = pair.token0() == tokenIn;
        (uint256 rIn, uint256 rOut) = inIs0 ? (uint256(r0), uint256(r1)) : (uint256(r1), uint256(r0));
        uint256 actualIn = IERC20(tokenIn).balanceOf(address(pair)) - rIn;
        uint256 out = DexMath.amountOut(actualIn, rIn, rOut, swapFeeBps);
        (uint256 out0, uint256 out1) = inIs0 ? (uint256(0), out) : (out, uint256(0));
        pair.swap(out0, out1, to, "");
    }

    // ------------------------------------------------------------- helpers
    function _digest(bytes32 structHash) private view returns (bytes32) {
        bytes32 domain = keccak256(
            abi.encode(DOMAIN_TYPEHASH, keccak256("AgentRobMint"), keccak256("1"), block.chainid, address(this))
        );
        return keccak256(abi.encodePacked("\x19\x01", domain, structHash));
    }

    function _requireSigner(bytes32 digest, bytes calldata sig, address who) private pure {
        (address got, ECDSA.RecoverError err,) = ECDSA.tryRecover(digest, sig);
        if (err != ECDSA.RecoverError.NoError || got != who) revert BadSignature();
    }
}
