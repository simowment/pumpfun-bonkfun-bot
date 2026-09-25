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
    default_exit_rules,
    summarize_rules,
    trades_from_swap_api,
)
from rugbot.ingest.pump.create_event_decoder import decode_pump_create_event_logs
from rugbot.ingest.pump.pump_create_observation import (
    decode_pump_create_v2_observation,
)
from rugbot.ingest.pump.pump_stream import PumpPortalLaunchStream
from rugbot.ingest.rpc_observer import observe_finalized_transaction
from rugbot.integrations.pumpfun_api import get_client
from rugbot.intelligence.token_resolver import resolve_token_or_wallet
from rugbot.tracker.funding_chain import resolve_relay_terminal

pytestmark = pytest.mark.skipif(
    os.environ.get("RUGBOT_LIVE_TESTS") != "1" or not os.environ.get("SOLANA_RPC_HTTP"),
    reason="live mainnet tests need RUGBOT_LIVE_TESTS=1 and SOLANA_RPC_HTTP",
)

ZKASH_MINT = "3FCahiaD51BY8rNDK1BarxmYrWdE8KqKGeWjMQNwpump"
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
    resolution = resolve_relay_terminal(ZKASH_HUB_RECIPIENT, received_sol=2.0)
    assert resolution.terminal == ZKASH_CREATOR
    assert len(resolution.relays) >= 2


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
    summaries = summarize_rules([replay], default_exit_rules())
    assert summaries[0].net_ev_sol >= summaries[-1].net_ev_sol


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
