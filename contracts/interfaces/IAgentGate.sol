// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Where the Agent-ID registrar is native to the deployment chain, the gate
///         reads it on-chain. Where the Agent-ID lives on a different chain and cannot
///         be read here, the gate is an off-chain signer that attests eligibility with
///         an EIP-712 voucher. The mint contract supports both paths behind one config.
interface IAgentRegistrar {
    function ownerOf(uint256 tokenId) external view returns (address);
    function handleOf(address owner) external view returns (string memory);
}
