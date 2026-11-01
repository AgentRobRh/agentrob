// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Chainlink-compatible aggregator (AggregatorV3Interface subset).
interface IAggregatorV3 {
    function decimals() external view returns (uint8);
    function latestRoundData()
        external
        view
        returns (uint80 roundId, int256 answer, uint256 startedAt, uint256 updatedAt, uint80 answeredInRound);
}
