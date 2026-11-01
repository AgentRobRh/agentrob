// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script} from "forge-std/Script.sol";
import {RobToken} from "../src/RobToken.sol";
import {RobMint} from "../src/RobMint.sol";

/// @notice Deploy order: factory + WETH must exist, then RobToken (creates the pair),
///         then RobMint, then RobToken.initialize(mint). Treasury seeds and unpauses.
contract Deploy is Script {
    function run() external {
        address weth = vm.envAddress("WETH");
        address factory = vm.envAddress("DEX_FACTORY");
        address gateSigner = vm.envAddress("GATE_SIGNER");
        // QUOTA_SIGNER must be set explicitly and SHOULD differ from GATE_SIGNER: the mint key and
        // the quota key are separate so a leak of one does not grant the other's power. No fallback.
        address quotaSigner = vm.envAddress("QUOTA_SIGNER");
        address treasury = vm.envAddress("TREASURY");
        uint256 swapFeeBps = vm.envOr("SWAP_FEE_BPS", uint256(25));
        uint256 freeShares = vm.envOr("FREE_SHARES", uint256(1_000));
        uint256 maxPaidPerWallet = vm.envOr("MAX_PAID_PER_WALLET", uint256(25));
        uint256 openAt = vm.envOr("OPEN_AT", uint256(10_000));
        uint256 floorLineNum = vm.envOr("FLOOR_LINE_NUM", uint256(50_000_000));
        uint256 floorLineDenom = vm.envOr("FLOOR_LINE_DENOM", uint256(27_727_060));
        uint256 poolTokenFloor = vm.envOr("POOL_TOKEN_FLOOR", uint256(11_666_667 ether));
        // Read the ETH/USD feed + L2 sequencer feed from the chain's published oracle list.
        address ethUsdFeed = vm.envAddress("ETH_USD_FEED");
        address sequencerFeed = vm.envOr("SEQUENCER_FEED", address(0));
        uint256 feedHeartbeat = vm.envOr("FEED_HEARTBEAT", uint256(1 hours));
        uint256 feedMin = vm.envOr("FEED_MIN", uint256(1e8));
        uint256 feedMax = vm.envOr("FEED_MAX", uint256(1_000_000e8));
        uint256 sequencerGrace = vm.envOr("SEQUENCER_GRACE", uint256(1 hours));

        vm.startBroadcast();

        RobToken token = new RobToken(weth, factory);
        RobMint mint = new RobMint(
            RobMint.Config({
                token: address(token),
                weth: weth,
                gateSigner: gateSigner,
                quotaSigner: quotaSigner,
                treasury: treasury,
                swapFeeBps: swapFeeBps,
                freeShares: freeShares,
                maxPaidPerWallet: maxPaidPerWallet,
                openAt: openAt,
                floorLineNum: floorLineNum,
                floorLineDenom: floorLineDenom,
                poolTokenFloor: poolTokenFloor,
                ethUsdFeed: ethUsdFeed,
                sequencerFeed: sequencerFeed,
                feedHeartbeat: feedHeartbeat,
                feedMin: feedMin,
                feedMax: feedMax,
                sequencerGrace: sequencerGrace
            })
        );
        token.initialize(address(mint));

        vm.stopBroadcast();
    }
}
