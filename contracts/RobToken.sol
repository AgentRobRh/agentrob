// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IDexFactory, IDexPair, IWETH, DexMath} from "./interfaces/IDex.sol";

interface IRobMintTreasury {
    function treasury() external view returns (address);
}

/// @dev The pair forbids swapping out TO token0/token1, so the dividend swap cannot send WETH
///      directly to the token contract. This trivial sink receives the WETH from the swap and
///      forwards it to the token on demand. It holds nothing else and has no other powers.
contract DividendSink {
    address public immutable token;
    IERC20 public immutable weth;

    constructor(address weth_) {
        token = msg.sender;
        weth = IERC20(weth_);
    }

    function flush() external returns (uint256 amount) {
        require(msg.sender == token, "only token");
        amount = weth.balanceOf(address(this));
        if (amount > 0) weth.transfer(token, amount);
    }
}

/// @title ROB token
/// @notice Fixed-cap ERC-20 minted only by the mint contract, up to the hard cap. Before trading is
///         enabled only the mint contract can move tokens, which keeps the escrow/launch phase
///         non-transferable. After launch any wallet and any plain contract can receive. Pool-like
///         receivers are refused: other pairs that hold this token, and any pool the mint contract
///         flags. The liquidity pair is a self-deployed Uniswap V2 pair.
///
///         Trade tax: BUY 3% is left with the mint contract (swept to ETH for the treasury). SELL 3%
///         splits 1% to the mint contract (treasury) and 2% to THIS contract, which accumulates and,
///         via a permissionless capped `sweepDividends()`, swaps to ETH and distributes it to every
///         $ROB holder pro-rata (magnified-dividend-per-share accounting). Holders pull their ETH
///         with `withdrawDividend()`. Transfers are untaxed. No swap ever runs inside a user transfer.
contract RobToken is ERC20 {
    using DexMath for uint256;

    uint256 public constant MAX_SUPPLY = 444_000_000_000 ether; // 444B

    // Trade tax in basis points. Buy 3% -> treasury. Sell 3% = 1% treasury + 2% holder dividend.
    uint256 public constant BUY_TAX_BPS = 300;
    uint256 public constant SELL_TREASURY_BPS = 100;
    uint256 public constant SELL_DIVIDEND_BPS = 200;

    uint256 private constant SWAP_FEE_BPS = 25;     // RobPair constant-product fee (0.25%)
    uint256 private constant MAX_SWEEP_BPS = 10;    // at most 0.1% of the pool token reserve per sweep
    uint256 private constant MAGNITUDE = 2 ** 128;  // dividend-per-share fixed-point scale

    uint256 private constant PROBE_GAS = 20_000;
    address public constant DEAD = 0x000000000000000000000000000000000000dEaD;

    /// @dev The other side of the official pair (WETH on this deployment).
    address public immutable quote;
    address public immutable pair;
    address public immutable sink; // receives dividend-swap WETH (pair can't pay out to the token)

    address public deployer;
    address public minter;
    bool public tradingEnabled;
    bool public blocksDisabled;
    mapping(address => bool) public blockedPool;

    // --- dividends (ETH, magnified-dividend-per-share) ---
    uint256 public magnifiedDividendPerShare;
    mapping(address => int256) public magnifiedDividendCorrections;
    mapping(address => uint256) public withdrawnDividends;
    mapping(address => bool) public excludedFromDividends;
    mapping(address => uint256) private _divBal; // eligible balance last accounted per holder
    uint256 public dividendEligibleSupply;       // sum of non-excluded balances
    uint256 public totalDividendsDistributed;    // lifetime ETH distributed
    uint256 public lastSweepBlock;
    bool private _locked;

    event TradingEnabled(uint256 at);
    event PoolBlocked(address indexed pool, bool blocked);
    event BlocksDisabled();
    event DividendsDistributed(uint256 ethAmount);
    event DividendWithdrawn(address indexed account, uint256 ethAmount);
    event DividendSwept(uint256 tokensIn, uint256 ethOut);
    event ExcludedFromDividends(address indexed account, bool excluded);

    error NotDeployer();
    error NotMinter();
    error AlreadyInitialized();
    error ZeroAddress();
    error TransfersClosed();
    error PoolRejected();
    error SupplyCap();
    error Reentrancy();
    error SweptThisBlock();
    error NothingToSweep();
    error NotTradingYet();

    constructor(address quote_, address factory_)
        ERC20("AgentRob", "ROB")
    {
        if (quote_ == address(0) || factory_ == address(0)) revert ZeroAddress();
        quote = quote_;
        pair = IDexFactory(factory_).createPair(address(this), quote_);
        sink = address(new DividendSink(quote_));
        deployer = msg.sender;
        // pools / sinks never earn dividends (set before any mint, so balances are 0)
        excludedFromDividends[pair] = true;
        excludedFromDividends[DEAD] = true;
        excludedFromDividends[address(this)] = true;
    }

    /// @notice One-time wiring of the mint contract, after which the deployer is cleared. Excludes
    ///         the mint contract and its treasury from dividends (both are tax sinks, not holders).
    function initialize(address minter_) external {
        if (msg.sender != deployer) revert NotDeployer();
        if (minter != address(0)) revert AlreadyInitialized();
        if (minter_ == address(0)) revert ZeroAddress();
        minter = minter_;
        deployer = address(0);
        _setExcluded(minter_, true);
        address t = IRobMintTreasury(minter_).treasury();
        if (t != address(0)) _setExcluded(t, true);
    }

    modifier onlyMinter() {
        if (msg.sender != minter) revert NotMinter();
        _;
    }

    modifier nonReentrant() {
        if (_locked) revert Reentrancy();
        _locked = true;
        _;
        _locked = false;
    }

    function mint(address to, uint256 amount) external onlyMinter {
        if (totalSupply() + amount > MAX_SUPPLY) revert SupplyCap();
        _mint(to, amount);
    }

    function enableTrading() external onlyMinter {
        if (tradingEnabled) return;
        tradingEnabled = true;
        emit TradingEnabled(block.timestamp);
    }

    /// @notice Moves pool inventory to the dead address for the floor mechanism.
    ///         The mint contract syncs the pair immediately afterwards.
    function burnFromPair(uint256 amount) external onlyMinter {
        super._update(pair, DEAD, amount); // pair & DEAD are both excluded: no dividend sync needed
    }

    /// @notice Burns tokens from `holder`, reducing total supply. Used only by the mint contract to
    ///         un-mint a refunded pre-open stake. Goes through super._update so it is not stopped by
    ///         the pre-trading transfer lock; we re-sync the holder's dividend balance afterwards.
    function burnFromHolder(address holder, uint256 amount) external onlyMinter {
        super._update(holder, address(0), amount);
        _syncDiv(holder);
    }

    function setPoolBlocked(address pool, bool blocked) external onlyMinter {
        blockedPool[pool] = blocked;
        emit PoolBlocked(pool, blocked);
    }

    function disableBlocks() external onlyMinter {
        blocksDisabled = true;
        emit BlocksDisabled();
    }

    /// @notice Include/exclude an address from dividends. Minter-gated (or deployer pre-init).
    function excludeFromDividends(address a, bool ex) external {
        if (!(msg.sender == minter || (minter == address(0) && msg.sender == deployer))) revert NotMinter();
        _setExcluded(a, ex);
    }

    // ------------------------------------------------------------- pool guard
    function rejects(address to) public view returns (bool) {
        if (to == pair) return false;
        if (blockedPool[to] && !blocksDisabled) return true;
        return _isOtherPair(to);
    }

    function _isOtherPair(address a) private view returns (bool) {
        uint256 size = a.code.length;
        if (size == 0) return false;
        if (size == 23) {
            bytes memory code = a.code;
            if (code[0] == 0xef && code[1] == 0x01 && code[2] == 0x00) return false;
        }
        address t0 = _probe(a, 0x0dfe1681); // token0()
        if (t0 == address(this)) return _probe(a, 0xd21220a7) != address(0); // token1()
        if (t0 == address(0)) return false;
        return _probe(a, 0xd21220a7) == address(this);
    }

    function _probe(address a, bytes4 selector) private view returns (address out) {
        (bool ok, bytes memory data) = a.staticcall{gas: PROBE_GAS}(abi.encodeWithSelector(selector));
        if (!ok || data.length != 32) return address(0);
        uint256 word = abi.decode(data, (uint256));
        if (word >> 160 != 0) return address(0);
        out = address(uint160(word));
    }

    // ------------------------------------------------------------- transfers + tax
    function _update(address from, address to, uint256 value) internal override {
        if (from == address(0)) {
            super._update(from, to, value); // mint
            _syncDiv(to);
            return;
        }
        address m = minter;
        if (!tradingEnabled) {
            if (from != m) revert TransfersClosed();
            super._update(from, to, value);
            _syncDiv(to);
            return;
        }
        if (rejects(to)) revert PoolRejected();
        // mint contract and this token (sweep) move untaxed
        if (from == m || to == m || from == address(this) || to == address(this)) {
            super._update(from, to, value);
            _syncDiv(from);
            _syncDiv(to);
            return;
        }
        if (from == pair) {
            // Buy: 3% to the mint contract (treasury path).
            uint256 tax = (value * BUY_TAX_BPS) / 10_000;
            super._update(from, m, tax);
            super._update(from, to, value - tax);
            _syncDiv(to);
        } else if (to == pair) {
            // Sell: 1% to the mint contract (treasury), 2% to this contract (holder dividend).
            uint256 toTreasury = (value * SELL_TREASURY_BPS) / 10_000;
            uint256 toDividend = (value * SELL_DIVIDEND_BPS) / 10_000;
            super._update(from, m, toTreasury);
            super._update(from, address(this), toDividend);
            super._update(from, to, value - toTreasury - toDividend);
            _syncDiv(from);
        } else {
            super._update(from, to, value);
            _syncDiv(from);
            _syncDiv(to);
        }
    }

    // ------------------------------------------------------------- dividend accounting
    /// @dev Reconcile a holder's dividend-eligible balance against their real balance. Excluded
    ///      accounts contribute nothing. Corrections preserve already-accrued dividends across
    ///      balance changes (standard magnified-dividend pattern).
    function _syncDiv(address account) private {
        if (account == address(0) || excludedFromDividends[account]) return;
        uint256 bal = balanceOf(account);
        uint256 prev = _divBal[account];
        if (bal == prev) return;
        if (bal > prev) {
            uint256 d = bal - prev;
            dividendEligibleSupply += d;
            magnifiedDividendCorrections[account] -= int256(magnifiedDividendPerShare * d);
        } else {
            uint256 d = prev - bal;
            dividendEligibleSupply -= d;
            magnifiedDividendCorrections[account] += int256(magnifiedDividendPerShare * d);
        }
        _divBal[account] = bal;
    }

    function _setExcluded(address a, bool ex) private {
        if (excludedFromDividends[a] == ex) return;
        if (ex) {
            uint256 b = _divBal[a];
            if (b > 0) {
                dividendEligibleSupply -= b;
                magnifiedDividendCorrections[a] += int256(magnifiedDividendPerShare * b);
                _divBal[a] = 0;
            }
            excludedFromDividends[a] = true;
        } else {
            excludedFromDividends[a] = false;
            uint256 bal = balanceOf(a);
            if (bal > 0) {
                dividendEligibleSupply += bal;
                magnifiedDividendCorrections[a] -= int256(magnifiedDividendPerShare * bal);
                _divBal[a] = bal;
            }
        }
        emit ExcludedFromDividends(a, ex);
    }

    function _distributeDividends(uint256 ethAmount) private {
        if (ethAmount == 0 || dividendEligibleSupply == 0) return;
        magnifiedDividendPerShare += (ethAmount * MAGNITUDE) / dividendEligibleSupply;
        totalDividendsDistributed += ethAmount;
        emit DividendsDistributed(ethAmount);
    }

    /// @notice Lifetime ETH credited to `account` (whether or not withdrawn).
    function accumulativeDividendOf(address account) public view returns (uint256) {
        int256 total = int256(magnifiedDividendPerShare * _divBal[account]) + magnifiedDividendCorrections[account];
        if (total < 0) return 0;
        return uint256(total) / MAGNITUDE;
    }

    /// @notice ETH `account` can withdraw right now.
    function withdrawableDividendOf(address account) public view returns (uint256) {
        return accumulativeDividendOf(account) - withdrawnDividends[account];
    }

    function dividendOf(address account) external view returns (uint256) {
        return withdrawableDividendOf(account);
    }

    /// @notice Pull your accrued ETH dividends. Permissionless, non-custodial, reentrancy-guarded.
    function withdrawDividend() external nonReentrant returns (uint256) {
        uint256 amount = withdrawableDividendOf(msg.sender);
        if (amount == 0) return 0;
        withdrawnDividends[msg.sender] += amount;
        emit DividendWithdrawn(msg.sender, amount);
        (bool ok,) = payable(msg.sender).call{value: amount}("");
        if (!ok) {
            withdrawnDividends[msg.sender] -= amount; // revert the credit so it can be retried
            revert("ETH transfer failed");
        }
        return amount;
    }

    // ------------------------------------------------------------- dividend sweep (token -> ETH)
    /// @notice Swap the accumulated 2% dividend tokens held by this contract into ETH and distribute
    ///         it to holders. Permissionless, once per block, capped at 0.1% of the pool token
    ///         reserve per call so it can never dump the pool. No swap runs inside a user transfer.
    function sweepDividends() external nonReentrant {
        if (!tradingEnabled) revert NotTradingYet();
        if (lastSweepBlock == block.number) revert SweptThisBlock();

        (uint112 r0, uint112 r1,) = IDexPair(pair).getReserves();
        bool tokenIs0 = IDexPair(pair).token0() == address(this);
        uint256 rTok = tokenIs0 ? uint256(r0) : uint256(r1);
        uint256 rQuote = tokenIs0 ? uint256(r1) : uint256(r0);
        if (rTok == 0 || rQuote == 0) revert NotTradingYet();

        uint256 bal = balanceOf(address(this));
        uint256 cap = (rTok * MAX_SWEEP_BPS) / 10_000;
        uint256 amount = bal > cap ? cap : bal;
        if (amount == 0) revert NothingToSweep();
        lastSweepBlock = block.number;

        // move the dividend tokens into the pair (untaxed: from == this) and swap for WETH to the
        // sink (the pair can't pay out to the token itself), then pull the WETH in and unwrap to ETH
        super._update(address(this), pair, amount);
        uint256 out = DexMath.amountOut(amount, rTok, rQuote, SWAP_FEE_BPS);
        (uint256 out0, uint256 out1) = tokenIs0 ? (uint256(0), out) : (out, uint256(0));
        IDexPair(pair).swap(out0, out1, sink, "");
        uint256 wethOut = DividendSink(sink).flush(); // WETH -> this contract

        if (wethOut > 0) {
            IWETH(quote).withdraw(wethOut); // WETH -> native ETH (lands in receive())
            _distributeDividends(wethOut);
        }
        emit DividendSwept(amount, wethOut);
    }

    receive() external payable {} // accepts ETH from WETH.withdraw during a sweep
}
