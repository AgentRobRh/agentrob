// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Minimal Uniswap-V2 style interfaces. These point at a self-deployed minimal
///         V2 where no suitable DEX exists, or at the native V2 DEX otherwise (same ABI).

interface IDexFactory {
    function createPair(address tokenA, address tokenB) external returns (address pair);
    function getPair(address tokenA, address tokenB) external view returns (address pair);
}

interface IDexPair {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function getReserves() external view returns (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast);
    function mint(address to) external returns (uint256 liquidity);
    function swap(uint256 amount0Out, uint256 amount1Out, address to, bytes calldata data) external;
    function sync() external;
}

interface IWETH {
    function deposit() external payable;
    function withdraw(uint256 amount) external;
}

/// @dev Constant-product output with a configurable fee (in bps) to match the venue.
library DexMath {
    function amountOut(uint256 amountIn, uint256 reserveIn, uint256 reserveOut, uint256 feeBps)
        internal
        pure
        returns (uint256)
    {
        uint256 inWithFee = amountIn * (10_000 - feeBps);
        return (inWithFee * reserveOut) / (reserveIn * 10_000 + inWithFee);
    }
}
