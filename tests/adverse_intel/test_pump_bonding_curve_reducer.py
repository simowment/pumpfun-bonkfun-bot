"""Pump bonding-curve market-state reducer tests."""

import ast
import base64
import json
import struct
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

import base58

from rugbot.domain.amounts import QuoteBaseUnits, Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import FeeConfig
from rugbot.domain.market_state import PumpBondingCurveAccountSnapshot
from rugbot.domain.observations import RawChainObservation
from rugbot.domain.quotes import ExecutableQuote, QuotePath
from rugbot.protocol.pump.bonding_curve_account import (
    PINNED_PUMP_IDL_SHA256,
    PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
    PUMP_PROGRAM_ID,
    bonding_curve_snapshot_to_pool_reserves,
)
from rugbot.protocol.pump.market_state import (
    PUMP_BONDING_CURVE_REDUCER_VERSION,
    PumpBondingCurveAccountMetadata,
    PumpBondingCurveMarketState,
    metadata_key_for_event,
    reduce_pump_bonding_curve_market_state,
)
from rugbot.protocol.pump.quote_engine import (
    PoolReserves,
    executable_buy_quote,
)
from rugbot.protocol.pump.version_registry import PumpProtocolVersionSnapshot
from rugbot.replay.account_state_timeline import (
    FinalizedAccountStateReplayEvent,
    FinalizedAccountStateTimeline,
    build_finalized_account_state_timeline,
)

REDUCER_MODULE = Path("src/rugbot/protocol/pump/market_state.py")
FIXTURE_PATH = Path(
    "fixtures/account_states/pump_bonding_curve/finalized_current_layout_ffzxakv.json"
)
SOURCE_ARTIFACT_VERSION = "pump-bonding-curve-account-fixture-v1"
BASE_MINT = "fixture-base-mint"
QUOTE_MINT = "So11111111111111111111111111111111111111112"
BOOT_ID = UUID("00000000-0000-0000-0000-000000000001")
FORBIDDEN_IMPORT_PREFIXES = (
    "rugbot.ingest",
    "rugbot.storage",
    "rugbot.execution",
    "src.core",
    "src.trading",
    "src.platforms",
    "solana",
    "solders",
    "dotenv",
    "requests",
    "aiohttp",
    "httpx",
)


class PumpBondingCurveReducerTests(unittest.TestCase):
    """Tests for reducing finalized account states into Pump snapshots."""

    def test_reduces_finalized_timeline_to_snapshot_and_quote_reserves(self) -> None:
        """A finalized account update decodes into quote-ready market state."""

        timeline = _timeline(
            _account_observation(raw_id_suffix=1, slot=_fixture_slot()),
            as_of_slot=_fixture_slot(),
        )
        metadata = _metadata(slot=Slot(_fixture_slot()))

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={metadata_key_for_event(timeline.events[0]): metadata},
        )

        self.assertIsInstance(result, PumpBondingCurveMarketState)
        state = cast("PumpBondingCurveMarketState", result)
        self.assertEqual(state.as_of_slot, _fixture_slot())
        self.assertEqual(state.reducer_version, PUMP_BONDING_CURVE_REDUCER_VERSION)
        self.assertEqual(state.source_event_count, 1)
        self.assertEqual(state.decoded_event_count, 1)
        self.assertEqual(state.decoded_events, state.latest_events)

        event = state.latest_events[0]
        snapshot = event.snapshot
        self.assertIsInstance(snapshot, PumpBondingCurveAccountSnapshot)
        self.assertEqual(event.as_of_slot, _fixture_slot())
        self.assertEqual(event.source_slot, _fixture_slot())
        self.assertEqual(event.source_raw_ids, timeline.events[0].source_raw_ids)
        self.assertEqual(snapshot.account_pubkey, _fixture()["account"]["pubkey"])
        self.assertEqual(snapshot.owner_program_id, PUMP_PROGRAM_ID)
        self.assertEqual(snapshot.raw_account_data_sha256, _fixture_data_sha256())

        reserves = bonding_curve_snapshot_to_pool_reserves(snapshot)
        self.assertIsInstance(reserves, PoolReserves)
        quote = executable_buy_quote(
            path=QuotePath.PUMP_BONDING_CURVE,
            reserves=cast("PoolReserves", reserves),
            quote_input_amount=QuoteBaseUnits(10_000),
            fee_config=_fee_config(snapshot.as_of_slot),
        )

        self.assertIsInstance(quote, ExecutableQuote)

    def test_latest_event_per_account_uses_replay_order(self) -> None:
        """Latest account state is selected after deterministic replay ordering."""

        first_slot = _fixture_slot()
        second_slot = first_slot + 1
        second_raw_data = _raw_data_with_real_sol_reserves(222_222_222)
        timeline = _timeline(
            _account_observation(
                raw_id_suffix=2,
                slot=first_slot,
                overrides=_ObservationOverrides(account_write_version=1),
            ),
            _account_observation(
                raw_id_suffix=3,
                slot=second_slot,
                overrides=_ObservationOverrides(
                    account_write_version=2,
                    raw_account_data=second_raw_data,
                ),
            ),
            as_of_slot=second_slot,
        )

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={
                (_fixture_account_bytes(), first_slot): _metadata(
                    slot=Slot(first_slot)
                ),
                (_fixture_account_bytes(), second_slot): _metadata(
                    slot=Slot(second_slot)
                ),
            },
        )

        self.assertIsInstance(result, PumpBondingCurveMarketState)
        state = cast("PumpBondingCurveMarketState", result)
        self.assertEqual(len(state.decoded_events), 2)
        self.assertEqual(len(state.latest_events), 1)
        latest = state.latest_events[0]
        self.assertEqual(latest.source_slot, second_slot)
        self.assertEqual(int(latest.snapshot.real_sol_reserves), 222_222_222)

    def test_same_slot_multiple_accounts_have_deterministic_latest_order(self) -> None:
        """Latest snapshots are sorted by raw account pubkey bytes."""

        other_account = bytes([9]) * 32
        slot = _fixture_slot()
        timeline = _timeline(
            _account_observation(
                raw_id_suffix=4,
                slot=slot,
                overrides=_ObservationOverrides(
                    account_pubkey=other_account,
                    raw_account_data=_raw_data_with_real_sol_reserves(333_333_333),
                ),
            ),
            _account_observation(raw_id_suffix=5, slot=slot),
            as_of_slot=slot,
        )

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={
                (_fixture_account_bytes(), slot): _metadata(slot=Slot(slot)),
                (other_account, slot): _metadata(
                    slot=Slot(slot),
                    overrides=_MetadataOverrides(account_pubkey=other_account),
                ),
            },
        )

        self.assertIsInstance(result, PumpBondingCurveMarketState)
        state = cast("PumpBondingCurveMarketState", result)
        self.assertEqual(
            [event.account_pubkey for event in state.latest_events],
            sorted([_fixture_account_bytes(), other_account]),
        )

    def test_missing_metadata_abstains(self) -> None:
        """Point-in-time protocol and mint metadata is required."""

        timeline = _timeline(
            _account_observation(raw_id_suffix=6, slot=_fixture_slot()),
            as_of_slot=_fixture_slot(),
        )

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={},
        )

        self.assert_abstains(
            result,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            as_of_slot=_fixture_slot(),
        )

    def test_metadata_slot_mismatch_abstains(self) -> None:
        """Metadata must be valid for the exact account-state source slot."""

        slot = _fixture_slot()
        timeline = _timeline(
            _account_observation(raw_id_suffix=7, slot=slot),
            as_of_slot=slot,
        )
        metadata = _metadata(slot=Slot(slot + 1))

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={(_fixture_account_bytes(), slot): metadata},
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=slot)

    def test_protocol_snapshot_slot_mismatch_abstains(self) -> None:
        """Protocol snapshots must be point-in-time aligned to event slots."""

        slot = _fixture_slot()
        timeline = _timeline(
            _account_observation(raw_id_suffix=8, slot=slot),
            as_of_slot=slot,
        )
        metadata = _metadata(
            slot=Slot(slot),
            overrides=_MetadataOverrides(
                protocol_snapshot=_protocol_snapshot(as_of_slot=Slot(slot + 1))
            ),
        )

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={(_fixture_account_bytes(), slot): metadata},
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=slot)

    def test_missing_protocol_snapshot_abstains(self) -> None:
        """Missing point-in-time protocol evidence returns abstention."""

        slot = _fixture_slot()
        timeline = _timeline(
            _account_observation(raw_id_suffix=13, slot=slot),
            as_of_slot=slot,
        )
        metadata = _metadata(
            slot=Slot(slot),
            overrides=_MetadataOverrides(protocol_snapshot=None),
        )

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={(_fixture_account_bytes(), slot): metadata},
        )

        self.assert_abstains(
            result,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            as_of_slot=slot,
        )

    def test_event_as_of_mismatch_abstains(self) -> None:
        """Reducer rejects timelines whose events use a different boundary."""

        slot = _fixture_slot()
        event = _account_state_event(
            as_of_slot=Slot(slot + 1),
            slot=Slot(slot),
            raw_id_suffix=9,
        )
        timeline = FinalizedAccountStateTimeline(
            as_of_slot=Slot(slot),
            events=(event,),
            source_observation_count=1,
            deduped_observation_count=1,
            timeline_version="timeline-v1",
        )

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={
                (_fixture_account_bytes(), slot): _metadata(slot=Slot(slot))
            },
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=slot)

    def test_malformed_event_account_pubkey_abstains(self) -> None:
        """Reducer revalidates timeline event account evidence at its boundary."""

        slot = _fixture_slot()
        event = _account_state_event(
            as_of_slot=Slot(slot),
            slot=Slot(slot),
            raw_id_suffix=14,
            overrides=_EventOverrides(account_pubkey=b"short"),
        )
        timeline = _manual_timeline(event=event, as_of_slot=Slot(slot))

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={},
        )

        self.assert_abstains(
            result,
            AbstainReason.MISSING_FEATURE,
            as_of_slot=slot,
        )

    def test_malformed_event_owner_abstains(self) -> None:
        """Reducer revalidates timeline event owner evidence at its boundary."""

        slot = _fixture_slot()
        event = _account_state_event(
            as_of_slot=Slot(slot),
            slot=Slot(slot),
            raw_id_suffix=15,
            overrides=_EventOverrides(owner_program_id=b"short"),
        )
        timeline = _manual_timeline(event=event, as_of_slot=Slot(slot))

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={},
        )

        self.assert_abstains(
            result,
            AbstainReason.MISSING_FEATURE,
            as_of_slot=slot,
        )

    def test_negative_event_write_version_abstains(self) -> None:
        """Reducer revalidates write-version evidence at its boundary."""

        slot = _fixture_slot()
        event = _account_state_event(
            as_of_slot=Slot(slot),
            slot=Slot(slot),
            raw_id_suffix=16,
            overrides=_EventOverrides(account_write_version=-1),
        )
        timeline = _manual_timeline(event=event, as_of_slot=Slot(slot))

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={},
        )

        self.assert_abstains(
            result,
            AbstainReason.MISSING_FEATURE,
            as_of_slot=slot,
        )

    def test_empty_event_source_ids_abstain(self) -> None:
        """Reducer requires source raw observation provenance."""

        slot = _fixture_slot()
        event = _account_state_event(
            as_of_slot=Slot(slot),
            slot=Slot(slot),
            raw_id_suffix=17,
            overrides=_EventOverrides(source_raw_ids=()),
        )
        timeline = _manual_timeline(event=event, as_of_slot=Slot(slot))

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={},
        )

        self.assert_abstains(
            result,
            AbstainReason.MISSING_FEATURE,
            as_of_slot=slot,
        )

    def test_raw_hash_mismatch_abstains(self) -> None:
        """Reducer rejects contradictory raw account byte evidence."""

        slot = _fixture_slot()
        event = _account_state_event(
            as_of_slot=Slot(slot),
            slot=Slot(slot),
            raw_id_suffix=18,
            overrides=_EventOverrides(raw_account_data_sha256="bad-hash"),
        )
        timeline = _manual_timeline(event=event, as_of_slot=Slot(slot))

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={},
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=slot,
        )

    def test_owner_mismatch_abstains_through_pinned_decoder(self) -> None:
        """The reducer does not bypass Pump owner validation."""

        slot = _fixture_slot()
        timeline = _timeline(
            _account_observation(
                raw_id_suffix=10,
                slot=slot,
                overrides=_ObservationOverrides(owner_program_id=bytes([88]) * 32),
            ),
            as_of_slot=slot,
        )

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={
                (_fixture_account_bytes(), slot): _metadata(slot=Slot(slot))
            },
        )

        self.assert_abstains(
            result,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            as_of_slot=slot,
        )

    def test_missing_source_artifact_abstains(self) -> None:
        """Raw account data must remain tied to a source artifact."""

        slot = _fixture_slot()
        timeline = _timeline(
            _account_observation(raw_id_suffix=11, slot=slot),
            as_of_slot=slot,
        )
        metadata = _metadata(
            slot=Slot(slot),
            overrides=_MetadataOverrides(source_artifact_version=""),
        )

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={(_fixture_account_bytes(), slot): metadata},
        )

        self.assert_abstains(
            result,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            as_of_slot=slot,
        )

    def test_idl_hash_mismatch_abstains_through_decoder(self) -> None:
        """Reducer propagates pinned decoder IDL mismatch abstentions."""

        slot = _fixture_slot()
        timeline = _timeline(
            _account_observation(raw_id_suffix=19, slot=slot),
            as_of_slot=slot,
        )
        metadata = _metadata(
            slot=Slot(slot),
            overrides=_MetadataOverrides(idl_hash="bad-idl"),
        )

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={(_fixture_account_bytes(), slot): metadata},
        )

        self.assert_abstains(
            result,
            AbstainReason.DECODER_MISMATCH,
            as_of_slot=slot,
        )

    def test_layout_artifact_mismatch_abstains_through_decoder(self) -> None:
        """Reducer propagates unsupported layout artifact abstentions."""

        slot = _fixture_slot()
        timeline = _timeline(
            _account_observation(raw_id_suffix=20, slot=slot),
            as_of_slot=slot,
        )
        metadata = _metadata(
            slot=Slot(slot),
            overrides=_MetadataOverrides(layout_artifact_version=""),
        )

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={(_fixture_account_bytes(), slot): metadata},
        )

        self.assert_abstains(
            result,
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            as_of_slot=slot,
        )

    def test_missing_decimals_or_mints_abstain_through_decoder(self) -> None:
        """Reducer propagates missing market metadata abstentions."""

        slot = _fixture_slot()
        cases = (
            _MetadataOverrides(base_decimals=None),
            _MetadataOverrides(quote_decimals=None),
            _MetadataOverrides(base_mint=None),
            _MetadataOverrides(quote_mint=None),
        )

        for index, overrides in enumerate(cases, start=21):
            with self.subTest(index=index):
                timeline = _timeline(
                    _account_observation(raw_id_suffix=index, slot=slot),
                    as_of_slot=slot,
                )
                metadata = _metadata(slot=Slot(slot), overrides=overrides)

                result = reduce_pump_bonding_curve_market_state(
                    timeline=timeline,
                    metadata_by_event={(_fixture_account_bytes(), slot): metadata},
                )

                self.assert_abstains(
                    result,
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    as_of_slot=slot,
                )

    def test_missing_reducer_version_abstains(self) -> None:
        """Reducer outputs require explicit version provenance."""

        timeline = _timeline(
            _account_observation(raw_id_suffix=12, slot=_fixture_slot()),
            as_of_slot=_fixture_slot(),
        )

        result = reduce_pump_bonding_curve_market_state(
            timeline=timeline,
            metadata_by_event={},
            reducer_version="",
        )

        self.assert_abstains(
            result,
            AbstainReason.DECODER_MISMATCH,
            as_of_slot=_fixture_slot(),
        )

    def test_reducer_stays_pure_and_integer_only(self) -> None:
        """Market-state reduction must not grow adapters, signers, or floats."""

        source = REDUCER_MODULE.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(REDUCER_MODULE))
        violations = [
            imported_name
            for imported_name in _imported_module_names(tree)
            if imported_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
        ]

        self.assertEqual(violations, [])
        for token in _forbidden_source_tokens():
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def assert_abstains(
        self,
        result: object,
        reason: AbstainReason,
        *,
        as_of_slot: int,
    ) -> None:
        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertEqual(result.reason, reason)
            self.assertEqual(result.as_of_slot, as_of_slot)


def _timeline(
    *observations: RawChainObservation,
    as_of_slot: int,
) -> FinalizedAccountStateTimeline:
    timeline = build_finalized_account_state_timeline(
        observations=observations,
        as_of_slot=Slot(as_of_slot),
    )
    if isinstance(timeline, FinalizedAccountStateTimeline):
        return timeline
    raise TypeError(timeline)


def _manual_timeline(
    *,
    event: FinalizedAccountStateReplayEvent,
    as_of_slot: Slot,
) -> FinalizedAccountStateTimeline:
    return FinalizedAccountStateTimeline(
        as_of_slot=as_of_slot,
        events=(event,),
        source_observation_count=1,
        deduped_observation_count=1,
        timeline_version="timeline-v1",
    )


@dataclass(frozen=True, slots=True)
class _ObservationOverrides:
    account_pubkey: bytes | None = None
    owner_program_id: bytes | None = None
    account_write_version: int = 0
    raw_account_data: bytes | None = None


@dataclass(frozen=True, slots=True)
class _EventOverrides:
    account_pubkey: bytes | None = None
    owner_program_id: bytes | None = None
    account_write_version: int = 0
    raw_account_data: bytes | None = None
    raw_account_data_sha256: str | None = None
    source_raw_ids: tuple[UUID, ...] | None = None


@dataclass(frozen=True, slots=True)
class _MetadataOverrides:
    account_pubkey: bytes | None = None
    protocol_snapshot: PumpProtocolVersionSnapshot | None | object = "__default__"
    base_decimals: int | object | None = 6
    quote_decimals: int | object | None = 9
    base_mint: str | object | None = BASE_MINT
    quote_mint: str | object | None = QUOTE_MINT
    source_artifact_version: str = SOURCE_ARTIFACT_VERSION
    idl_hash: str = PINNED_PUMP_IDL_SHA256
    layout_artifact_version: str = PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION


def _account_observation(
    *,
    raw_id_suffix: int,
    slot: int,
    overrides: _ObservationOverrides | None = None,
) -> RawChainObservation:
    values = overrides or _ObservationOverrides()
    return RawChainObservation(
        raw_id=UUID(f"00000000-0000-0000-0000-{raw_id_suffix:012d}"),
        source_id="geyser-main",
        observer_id="observer-1",
        boot_id=BOOT_ID,
        receive_sequence=raw_id_suffix,
        slot=slot,
        parent_slot=slot - 1 if slot > 0 else None,
        blockhash=None,
        signature=None,
        transaction_index=None,
        outer_instruction_index=None,
        inner_instruction_group_index=None,
        inner_instruction_index=None,
        stack_height=None,
        event_ordinal=None,
        commitment="finalized",
        canonical_status="canonical",
        received_wall_ns=10,
        received_monotonic_ns=20,
        program_id=None,
        account_pubkey=values.account_pubkey or _fixture_account_bytes(),
        account_owner_program_id=values.owner_program_id or _pump_program_bytes(),
        raw_transaction=None,
        raw_transaction_format=None,
        raw_account_data=values.raw_account_data or _fixture_raw_data(),
        account_write_version=values.account_write_version,
        source_update_kind="account",
        raw_source_status=None,
        raw_source_payload=b"raw-source-payload",
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
    )


def _account_state_event(
    *,
    as_of_slot: Slot,
    slot: Slot,
    raw_id_suffix: int,
    overrides: _EventOverrides | None = None,
) -> FinalizedAccountStateReplayEvent:
    values = overrides or _EventOverrides()
    raw_account_data = values.raw_account_data or _fixture_raw_data()
    raw_account_data_sha256 = values.raw_account_data_sha256 or _fixture_data_sha256()
    source_raw_ids = values.source_raw_ids
    if source_raw_ids is None:
        source_raw_ids = (UUID(f"00000000-0000-0000-0000-{raw_id_suffix:012d}"),)

    return FinalizedAccountStateReplayEvent(
        as_of_slot=as_of_slot,
        slot=slot,
        account_pubkey=values.account_pubkey or _fixture_account_bytes(),
        owner_program_id=values.owner_program_id or _pump_program_bytes(),
        account_write_version=values.account_write_version,
        raw_account_data=raw_account_data,
        raw_account_data_sha256=raw_account_data_sha256,
        decoder_name=None,
        decoder_version=None,
        idl_hash=None,
        source_raw_ids=source_raw_ids,
    )


def _metadata(
    *,
    slot: Slot,
    overrides: _MetadataOverrides | None = None,
) -> PumpBondingCurveAccountMetadata:
    values = overrides or _MetadataOverrides()
    selected_account = values.account_pubkey or _fixture_account_bytes()
    selected_protocol_snapshot = (
        _protocol_snapshot(as_of_slot=slot)
        if values.protocol_snapshot == "__default__"
        else values.protocol_snapshot
    )
    return PumpBondingCurveAccountMetadata(
        account_pubkey=selected_account,
        slot=slot,
        protocol_snapshot=selected_protocol_snapshot,
        base_decimals=values.base_decimals,
        quote_decimals=values.quote_decimals,
        base_mint=values.base_mint,
        quote_mint=values.quote_mint,
        source_artifact_version=values.source_artifact_version,
        idl_hash=values.idl_hash,
        layout_artifact_version=values.layout_artifact_version,
    )


def _protocol_snapshot(*, as_of_slot: Slot) -> PumpProtocolVersionSnapshot:
    return PumpProtocolVersionSnapshot(
        as_of_slot=as_of_slot,
        program_id=PUMP_PROGRAM_ID,
        idl_hash=PINNED_PUMP_IDL_SHA256,
        global_config_hash="fixture-global-config-hash",
        program_config_version="pump-global-v1",
        fee_config=_fee_config(as_of_slot),
        program_config_source_artifact_version="program-config-artifact-v1",
        fee_source_artifact_version="fee-artifact-v1",
        registry_version="pump-version-registry-v1",
    )


def _fee_config(as_of_slot: Slot) -> FeeConfig:
    return FeeConfig(
        version="pump-fees-v1",
        protocol_fee_bps=100,
        creator_fee_bps=25,
        is_known=True,
        program_config_version="pump-global-v1",
        valid_from_slot=Slot(max(0, int(as_of_slot) - 1)),
        valid_to_slot=None,
        source_artifact_version="fee-artifact-v1",
    )


def _fixture_raw_data() -> bytes:
    return base64.b64decode(_fixture()["account"]["data_base64"], validate=True)


def _raw_data_with_real_sol_reserves(real_sol_reserves: int) -> bytes:
    data = bytearray(_fixture_raw_data())
    struct.pack_into("<Q", data, 32, real_sol_reserves)
    return bytes(data)


def _fixture_data_sha256() -> str:
    return cast("str", _fixture()["account"]["data_sha256"])


def _fixture_slot() -> int:
    return cast("int", _fixture()["rpc"]["context_slot"])


def _fixture_account_bytes() -> bytes:
    return _pubkey_bytes(cast("str", _fixture()["account"]["pubkey"]))


def _pump_program_bytes() -> bytes:
    return _pubkey_bytes(PUMP_PROGRAM_ID)


def _pubkey_bytes(pubkey: str) -> bytes:
    return bytes(base58.b58decode(pubkey))


def _fixture() -> dict[str, object]:
    return cast(
        "dict[str, object]",
        json.loads(FIXTURE_PATH.read_text(encoding="utf-8")),
    )


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


def _forbidden_source_tokens() -> tuple[str, ...]:
    return (
        "Key" + "pair",
        "Wal" + "let",
        "PRIVATE" + "_KEY",
        "send" + "_transaction",
        "send" + "_raw_transaction",
        "float(",
    )


if __name__ == "__main__":
    unittest.main()
