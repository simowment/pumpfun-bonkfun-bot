"""Live integration tests for the core tracking path against mainnet.

Run with ``RUGBOT_LIVE_TESTS=1`` and ``SOLANA_RPC_HTTP`` set. They exercise real
RPC, pump.fun swap-api and PumpPortal, so they are skipped otherwise. The
reference entity is the ZKASH operator: hub 6RfZnj... funds creator burners
through relay hops.
"""

import asyncio
import json
import os

import pytest

from rugbot.backtest.launch_replay import (
    LaunchReplay,
    ReplayCosts,
    summarize_rules,
    take_profit_rules,
    trades_from_swap_api,
)
from rugbot.discover.fleet import create_facts
from rugbot.discover.screen import ScreenFilters, screen_launches
from rugbot.execution.auto_router import AutoRouter, RouteVenue
from rugbot.ingest.pump.create_event_decoder import decode_pump_create_event_logs
from rugbot.ingest.pump.pump_create_observation import (
    decode_pump_create_v2_observation,
)
from rugbot.ingest.pump.pump_stream import PumpPortalLaunchStream
from rugbot.ingest.rpc_observer import observe_finalized_transaction
from rugbot.integrations.pumpfun_api import get_client
from rugbot.intelligence.token_resolver import resolve_token_or_wallet
from rugbot.tracker.funding_chain import descend_to_creators

pytestmark = pytest.mark.skipif(
    os.environ.get("RUGBOT_LIVE_TESTS") != "1" or not os.environ.get("SOLANA_RPC_HTTP"),
    reason="live mainnet tests need RUGBOT_LIVE_TESTS=1 and SOLANA_RPC_HTTP",
)

ZKASH_MINT = "3FCahiaD51BY8rNDK1BarxmYrWdE8KqKGeWjMQNwpump"
# The mint address saw a failed pump transaction before its create landed.
FAILED_FIRST_TX_MINT = "894gDxuQ4w9jbJL9uUJeWjkya31gca6i5rRd2a8Upump"
FAILED_FIRST_TX_CREATOR = "CoaX7BJWN8sdxQdHtbrdbRpnUhn4MxFadzWdo8CpS5jR"
ZKASH_CREATOR = "3AXdfyrkKuYAPU3uPabuBWBgKA5dppbDihXezwt6t7VA"
ZKASH_CREATE_SLOT = 450274836
ZKASH_HUB_RECIPIENT = "FTYT7yWGGjnEAXT6w2y1CcbLNYgcQzdMQ9CZ5konpMkc"
LIVE_CREATE_SAMPLE = 15
FINALITY_WAIT_S = 20
MIN_DECODED_SHARE = 0.6
# Launch market cap in SOL: 30 SOL virtual over 1.073B tokens.
LAUNCH_MC_FLOOR_SOL = 27.0


def test_resolver_finds_true_creation_and_bundle() -> None:
    resolved = resolve_token_or_wallet(
        ZKASH_MINT, rpc_url=os.environ["SOLANA_RPC_HTTP"], skip_metadata=True
    )
    assert resolved.target_wallet == ZKASH_CREATOR
    assert resolved.creation_slot == ZKASH_CREATE_SLOT
    assert len(resolved.bundle_buys) >= 2


def test_trade_history_starts_at_creation() -> None:
    trades = trades_from_swap_api(get_client().fetch_all_trades(ZKASH_MINT))
    assert trades[0].slot == ZKASH_CREATE_SLOT
    assert trades[0].wallet == ZKASH_CREATOR
    assert len(trades) > 1000


def test_relay_hops_resolve_to_creator() -> None:
    descent = descend_to_creators(ZKASH_HUB_RECIPIENT)
    assert descent.creators.get(ZKASH_CREATOR, 0) >= 2


def test_replay_ranks_exit_rules_on_real_trades() -> None:
    trades = trades_from_swap_api(get_client().fetch_all_trades(ZKASH_MINT))
    replay = LaunchReplay(
        ZKASH_MINT,
        create_slot=trades[0].slot,
        creator=ZKASH_CREATOR,
        trades=trades,
        costs=ReplayCosts(entry_delay_slots=2),
    )
    assert replay.profile.entry_mc_sol > LAUNCH_MC_FLOOR_SOL
    assert trades[0].price_usd is not None
    rules = take_profit_rules([replay.profile.ath_multiple])
    summaries = summarize_rules([replay], rules)
    assert summaries[0].net_ev_sol >= summaries[-1].net_ev_sol
    # The ATH-derived take-profit is in the grid, so the best TP can sit there.
    assert any(
        rule.take_profit_pct == int((replay.profile.ath_multiple - 1) * 100)
        for rule in rules
    )
    assert not any(rule.exit_on_dev_sell for rule in rules)


def test_live_creates_decode() -> None:
    async def sample() -> tuple[int, int]:
        stream = PumpPortalLaunchStream(
            websocket_endpoint="wss://pumpportal.fun/api/data", api_key=None
        )
        notes = [
            await stream.next_global_notification() for _ in range(LIVE_CREATE_SAMPLE)
        ]
        await stream.close()
        await asyncio.sleep(FINALITY_WAIT_S)
        decoded = 0
        for note in notes:
            observation = await observe_finalized_transaction(
                note.signature,
                expected_slot=None,
                endpoint=os.environ["SOLANA_RPC_HTTP"],
                source_id="live-test",
                observer_id="live-test",
                receive_sequence=1,
                transport=None,
            )
            if hasattr(observation, "reason"):
                continue
            launch = decode_pump_create_v2_observation(observation)
            if launch is not None and not hasattr(launch, "reason"):
                decoded += 1
        return decoded, len(notes)

    decoded, total = asyncio.run(sample())
    assert decoded / total >= MIN_DECODED_SHARE


def test_create_event_decodes_on_reference_launch() -> None:
    client = get_client()
    trade = client.fetch_all_trades(ZKASH_MINT)[0]
    observation = asyncio.run(
        observe_finalized_transaction(
            trade["tx"],
            expected_slot=None,
            endpoint=os.environ["SOLANA_RPC_HTTP"],
            source_id="live-test",
            observer_id="live-test",
            receive_sequence=1,
            transport=None,
        )
    )
    assert not hasattr(observation, "reason")
    logs = json.loads(observation.raw_source_payload)["result"]["meta"]["logMessages"]
    event = decode_pump_create_event_logs(logs, as_of_slot=observation.slot)
    assert event is not None and not hasattr(event, "reason")
    assert event.mint_pubkey == ZKASH_MINT


GRADUATED_MINT = "279mMFSUjS2kg4S3yQwwv3zZBqCtZ1Quvmg8FUHYpump"


def test_router_sends_graduated_coins_to_pumpswap() -> None:
    async def check() -> None:
        router = AutoRouter(endpoint=os.environ["SOLANA_RPC_HTTP"])
        assert await router.detect_venue(GRADUATED_MINT) is RouteVenue.PUMPSWAP_AMM
        pool = await router.get_pumpswap_pool_info(GRADUATED_MINT)
        assert pool is not None
        base, quote = await router.get_pool_reserves(pool[1])
        assert base > 0
        assert quote > 0

    asyncio.run(check())


def test_screen_lists_sol_curve_launches_in_the_age_window() -> None:
    result = screen_launches(
        ScreenFilters(
            min_age_min=5,
            max_age_min=15,
            min_volume_usd=None,
            max_volume_usd=None,
            max_mc_usd=None,
            max_dev_launches=None,
            max_creation_mc_usd=15_000,
        )
    )
    assert result.listed_in_window > 0
    assert result.coins
    assert all(5 <= coin.age_min <= 16 for coin in result.coins)
    assert all(coin.creation_mc_usd <= 15_000 for coin in result.coins)


def test_create_facts_skip_failed_transactions_before_the_create() -> None:
    creator, _ = create_facts(FAILED_FIRST_TX_MINT)
    assert creator == FAILED_FIRST_TX_CREATOR
