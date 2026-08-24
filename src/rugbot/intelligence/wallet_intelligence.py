"""Bounded read-only wallet intelligence built from finalized RPC evidence."""

# Parsing hostile RPC JSON is intentionally branch-heavy and fail-closed.
# ruff: noqa: C901, PLR0911, PLR0912, PLR0913, S105, TRY003

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import base58
from sol_trade_sdk.solana.provider_pool import (
    AiohttpRpcTransport,
    RpcHttpTransport,
    RpcProviderPool,
)

from rugbot.decision.rugger_protection import (
    RuggerProtectionSnapshot,
    WalletHistoryBoundary,
    WalletRoleEvidence,
    build_rugger_protection_snapshot,
)
from rugbot.domain.account_roles import AddressRole
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.trades import TradeSide
from rugbot.ingest.pump.pump_create_observation import (
    decode_pump_create_v2_observation,
)
from rugbot.ingest.pump.pump_trade_observation import decode_pump_trade_observation
from rugbot.ingest.rpc_observer import observe_address
from rugbot.intelligence.gmgn_creator_history import (
    GmgnCreatorHistory,
    creator_history_to_json,
    fetch_gmgn_creator_history,
)
from rugbot.intelligence.token_resolver import resolve_token_or_wallet
from rugbot.intelligence.wallet_behavior import (
    CanonicalTransferEvidence,
    WalletAssetKind,
)
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from rugbot.domain.observations import RawChainObservation

logger = get_logger(__name__)

SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
SYSTEM_TRANSFER_TAG = b"\x02\x00\x00\x00"
SYSTEM_TRANSFER_DATA_LENGTH = 12
SYSTEM_TRANSFER_ACCOUNT_COUNT = 2
SOLANA_ADDRESS_BYTES = 32
MAX_HISTORY_TRANSACTIONS = 100
HISTORY_PAGE_SIZE = 20
MAX_HISTORY_PAGES = 100
MAX_LINKED_WALLETS = 20
MAX_WALLET_HOPS = 3
FRESH_WALLET_WINDOW_SLOTS = 10_000
MIN_REPEAT_BUNDLER_MINTS = 2
RPC_MINIMUM_INTERVAL_SECONDS = 0.125
SPL_TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SPL_TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SPL_TRANSFER_TAG = 3
SPL_TRANSFER_CHECKED_TAG = 12


@dataclass(frozen=True, slots=True)
class WalletLaunch:
    """One historical Pump create attributed to a wallet observation."""

    slot: int
    transaction_index: int
    signature: str
    mint: str
    name: str
    symbol: str
    creator: str
    position_is_zero_or_one: bool
    bonding_curve: str
    creation_submitter: str | None = None
    fee_payer: str | None = None
    first_buyer: str | None = None
    observed_wallet: str | None = None
    created_at: int | None = None


@dataclass(frozen=True, slots=True)
class WalletPumpTrade:
    """One finalized Pump trade executed by the observed wallet."""

    slot: int
    transaction_index: int
    outer_instruction_index: int
    signature: str
    mint: str
    side: TradeSide
    wallet: str
    created_at: int | None = None


@dataclass(frozen=True, slots=True)
class RepeatBundlerEntity:
    """Wallet bought in the creation slot across multiple mints by one creator."""

    bundler_wallet: str
    entity_creator: str
    mints: tuple[str, ...]
    buy_count: int
    first_buy_slot: int
    last_buy_slot: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WalletSwitchEvidence:
    """Point-in-time evidence for a linked wallet taking over launch activity."""

    as_of_slot: int
    linked_wallet: str
    transfer_source: str
    transfer_target: str
    launch_count: int
    early_launch_count: int
    first_transfer_slot: int
    last_transfer_slot: int
    first_launch_slot: int
    last_launch_slot: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WalletLink:
    """Observed direct native transfer between two wallet nodes."""

    source: str
    target: str
    transfer_count: int
    amount_lamports: int
    first_slot: int
    last_slot: int
    evidence_ids: tuple[str, ...]
    asset_kind: WalletAssetKind = WalletAssetKind.NATIVE
    asset_id: str = "SOL"
    amount_base_units: int | None = None


@dataclass(frozen=True, slots=True)
class TransferEvidenceRow:
    """One authoritative per-transfer record ready for tracker persistence.

    Carries exactly the fields ``SolTransfer`` construction needs, derived from
    canonical finalized transfer evidence. ``timestamp`` is present only when the
    underlying evidence exposes a block time; it is ``None`` otherwise.
    """

    source: str
    target: str
    amount_lamports: int
    slot: int
    signature: str
    event_index: int
    timestamp: int | None = None


@dataclass(frozen=True, slots=True)
class WalletNode:
    """Node displayed by the wallet bubble graph."""

    address: str
    is_target: bool
    scanned_transaction_count: int
    launch_count: int
    first_seen_slot: int | None
    last_seen_slot: int | None
    roles: tuple[str, ...]
    fresh_wallet_status: str = "unknown"


@dataclass(frozen=True, slots=True)
class WalletIntelligenceReport:
    """Bounded historical wallet report and graph payload."""

    as_of_slot: int
    target_wallet: str
    history_limit: int
    scanned_transaction_count: int
    successful_transaction_count: int
    first_seen_slot: int | None
    last_seen_slot: int | None
    launch_count: int
    direct_linked_wallet_count: int
    linked_creator_wallet_count: int
    wallet_switch_candidate: bool
    native_in_lamports: int
    native_out_lamports: int
    launches: tuple[WalletLaunch, ...]
    nodes: tuple[WalletNode, ...]
    edges: tuple[WalletLink, ...]
    warnings: tuple[str, ...]
    protection: RuggerProtectionSnapshot | None = None
    linked_launches: tuple[WalletLaunch, ...] = ()
    wallet_switches: tuple[WalletSwitchEvidence, ...] = ()
    linked_launch_count: int = 0
    early_launch_count: int = 0
    linked_early_launch_count: int = 0
    creator_history: GmgnCreatorHistory | None = None
    transfers: tuple[TransferEvidenceRow, ...] = ()
    trades: tuple[WalletPumpTrade, ...] = ()
    repeat_bundler_entities: tuple[RepeatBundlerEntity, ...] = ()


WalletIntelligenceResult = WalletIntelligenceReport | AbstainResult


async def scan_wallet_intelligence(
    wallet: str,
    *,
    endpoint: str,
    max_transactions: int = 50,
    max_history_pages: int = 10,
    max_linked_wallets: int = 8,
    max_hops: int = MAX_WALLET_HOPS,
    fresh_wallet_window_slots: int = FRESH_WALLET_WINDOW_SLOTS,
    as_of_slot: int | None = None,
    transport: RpcHttpTransport | None = None,
    fallback_endpoints: tuple[str, ...] = (),
) -> WalletIntelligenceResult:
    """Build a bounded wallet history and rugger-protection evidence graph.

    The scan uses only finalized ``getSignaturesForAddress`` and
    ``getTransaction`` evidence. Direct transfers, SPL transfers, and paths are
    observations, not proof of common ownership. Linked-wallet history is
    bounded by ``max_linked_wallets`` and ``max_hops``.
    """

    validation = _validate_request(
        wallet=wallet,
        endpoint=endpoint,
        max_transactions=max_transactions,
        max_history_pages=max_history_pages,
        max_linked_wallets=max_linked_wallets,
        max_hops=max_hops,
        fresh_wallet_window_slots=fresh_wallet_window_slots,
        as_of_slot=as_of_slot,
    )
    if validation is not None:
        return validation
    rpc_transport = (
        transport
        if transport is not None
        else (
            RpcProviderPool(
                (endpoint, *fallback_endpoints),
                minimum_interval_seconds=RPC_MINIMUM_INTERVAL_SECONDS,
            )
            if fallback_endpoints
            else AiohttpRpcTransport()
        )
    )
    uses_provider_pool = isinstance(rpc_transport, RpcProviderPool)
    observations = await observe_address(
        wallet,
        endpoint=endpoint,
        source_id="wallet-intelligence",
        observer_id="wallet-intelligence",
        max_signatures=min(max_transactions, HISTORY_PAGE_SIZE),
        max_transactions=max_transactions,
        max_pages=max_history_pages,
        start_slot=(
            0
            if max_transactions > HISTORY_PAGE_SIZE or as_of_slot is not None
            else None
        ),
        end_slot=as_of_slot,
        transport=rpc_transport,
        standard_history_only=uses_provider_pool,
    )
    if isinstance(observations, AbstainResult):
        return observations

    if not observations:
        return _abstain("wallet has no finalized transaction evidence", -1)

    cutoff = as_of_slot if as_of_slot is not None else _max_slot(observations)
    observations = _observations_at_or_before(observations, cutoff)
    if not observations:
        return _abstain(
            "wallet has no finalized transaction evidence at the requested cutoff",
            cutoff,
        )

    histories: dict[str, tuple[RawChainObservation, ...]] = {wallet: observations}
    history_limits = {wallet: max_transactions}
    warnings: list[str] = []
    frontier = set()
    seen_wallets = {wallet}
    target_transfers = _transfers_from_observations(
        observations,
        as_of_slot=cutoff,
    )
    if isinstance(target_transfers, AbstainResult):
        return target_transfers
    frontier.update(_counterparties(wallet, target_transfers))
    for _ in range(max_hops):
        remaining_wallets = max_linked_wallets - (len(seen_wallets) - 1)
        if remaining_wallets <= 0:
            break
        next_frontier = sorted(frontier - seen_wallets)[:remaining_wallets]
        if not next_frontier:
            break
        for peer in next_frontier:
            if peer in seen_wallets:
                continue
            peer_limit = min(max_transactions, 20)
            peer_observations = await observe_address(
                peer,
                endpoint=endpoint,
                source_id="wallet-intelligence-linked",
                observer_id="wallet-intelligence",
                max_signatures=min(peer_limit, HISTORY_PAGE_SIZE),
                max_transactions=peer_limit,
                max_pages=max_history_pages,
                start_slot=(
                    0
                    if peer_limit > HISTORY_PAGE_SIZE or as_of_slot is not None
                    else None
                ),
                end_slot=as_of_slot,
                transport=rpc_transport,
                standard_history_only=uses_provider_pool,
            )
            seen_wallets.add(peer)
            if isinstance(peer_observations, AbstainResult):
                warnings.append(f"linked wallet history unavailable: {peer}")
                continue
            peer_observations = _observations_at_or_before(
                peer_observations,
                cutoff,
            )
            if not peer_observations:
                warnings.append(f"linked wallet has no evidence at cutoff: {peer}")
                continue
            peer_transfers = _transfers_from_observations(
                peer_observations,
                as_of_slot=cutoff,
            )
            if isinstance(peer_transfers, AbstainResult):
                warnings.append(f"linked wallet history malformed: {peer}")
                continue
            histories[peer] = peer_observations
            history_limits[peer] = peer_limit
            frontier.update(_counterparties(peer, peer_transfers))
        if len(seen_wallets) > max_linked_wallets + 1:
            break

    return await build_wallet_intelligence_report_from_histories(
        wallet,
        histories=histories,
        history_limits=history_limits,
        endpoint=endpoint,
        max_hops=max_hops,
        fresh_wallet_window_slots=fresh_wallet_window_slots,
        warnings=tuple(warnings),
        as_of_slot=cutoff,
    )


async def build_wallet_intelligence_report_from_histories(
    wallet: str,
    *,
    histories: dict[str, tuple[RawChainObservation, ...]],
    history_limits: dict[str, int],
    endpoint: str,
    max_hops: int = MAX_WALLET_HOPS,
    fresh_wallet_window_slots: int = FRESH_WALLET_WINDOW_SLOTS,
    warnings: tuple[str, ...] = (),
    as_of_slot: int | None = None,
) -> WalletIntelligenceResult:
    """Analyze finalized wallet histories from live or durable observations."""

    wallet_observations = histories.get(wallet, ())
    if not wallet_observations:
        return _abstain("wallet has no cached finalized transaction evidence", -1)
    cutoff = as_of_slot if as_of_slot is not None else _max_slot(wallet_observations)
    all_transfers: dict[tuple[object, ...], CanonicalTransferEvidence] = {}
    launches_by_wallet: dict[str, tuple[WalletLaunch, ...]] = {}
    trades_by_wallet: dict[str, tuple[WalletPumpTrade, ...]] = {}
    report_warnings = list(warnings)
    for address, address_observations in histories.items():
        parsed_transfers = _transfers_from_observations(
            address_observations,
            as_of_slot=cutoff,
        )
        if isinstance(parsed_transfers, AbstainResult):
            if address == wallet:
                return parsed_transfers
            report_warnings.append(f"linked wallet evidence unavailable: {address}")
            continue
        for transfer in parsed_transfers:
            all_transfers[_transfer_key(transfer)] = transfer
        launches = _launches_from_observations(
            address_observations,
            observed_wallet=address,
            as_of_slot=cutoff,
        )
        if isinstance(launches, AbstainResult):
            if address == wallet:
                return launches
            report_warnings.append(
                f"linked wallet launch evidence unavailable: {address}"
            )
            continue
        launches_by_wallet[address] = launches
        trades = _trades_from_observations(
            address_observations,
            as_of_slot=cutoff,
        )
        if isinstance(trades, AbstainResult):
            if address == wallet:
                return trades
            report_warnings.append(
                f"linked wallet trade evidence unavailable: {address}"
            )
            continue
        trades_by_wallet[address] = trades

    typed_transfers = tuple(all_transfers.values())
    roles = _roles_from_launches(launches_by_wallet)
    histories_for_graph = tuple(
        _history_boundary(
            address=address,
            observations=address_observations,
            history_limit=history_limits[address],
        )
        for address, address_observations in histories.items()
    )
    snapshot_slot = cutoff
    protection = build_rugger_protection_snapshot(
        transfers=typed_transfers,
        roles=roles,
        histories=histories_for_graph,
        target_wallet=wallet,
        as_of_slot=snapshot_slot,
        max_hops=max_hops,
        fresh_wallet_window_slots=fresh_wallet_window_slots,
    )
    if isinstance(protection, AbstainResult):
        return protection

    report = _build_report(
        wallet=wallet,
        histories=histories,
        transfers=typed_transfers,
        launches_by_wallet=launches_by_wallet,
        trades_by_wallet=trades_by_wallet,
        roles=roles,
        protection=protection,
        history_limit=history_limits[wallet],
        warnings=tuple(report_warnings),
        as_of_slot=cutoff,
    )
    creator_history, repeat_bundler_entities = await asyncio.gather(
        fetch_gmgn_creator_history(wallet),
        _repeat_bundler_entities(report.trades, endpoint=endpoint),
    )
    if isinstance(creator_history, AbstainResult):
        return replace(
            report,
            repeat_bundler_entities=repeat_bundler_entities,
            warnings=(
                *report.warnings,
                f"creator-wide history unavailable: {creator_history.message}",
            ),
        )
    return replace(
        report,
        creator_history=creator_history,
        repeat_bundler_entities=repeat_bundler_entities,
    )


def report_to_json(report: WalletIntelligenceReport) -> dict[str, object]:
    """Convert a typed wallet report to a UI-friendly JSON object."""

    repeat_bundler_wallets = {
        entity.bundler_wallet for entity in report.repeat_bundler_entities
    }
    return {
        "status": "ok",
        "as_of_slot": report.as_of_slot,
        "target_wallet": report.target_wallet,
        "history_limit": report.history_limit,
        "stats": {
            "scanned_transaction_count": report.scanned_transaction_count,
            "successful_transaction_count": report.successful_transaction_count,
            "first_seen_slot": report.first_seen_slot,
            "last_seen_slot": report.last_seen_slot,
            "launch_count": report.launch_count,
            "early_launch_count": report.early_launch_count,
            "linked_launch_count": report.linked_launch_count,
            "linked_early_launch_count": report.linked_early_launch_count,
            "direct_linked_wallet_count": report.direct_linked_wallet_count,
            "linked_creator_wallet_count": report.linked_creator_wallet_count,
            "wallet_switch_candidate": report.wallet_switch_candidate,
            "wallet_switch_count": len(report.wallet_switches),
            "pump_buy_count": sum(
                trade.side is TradeSide.BUY for trade in report.trades
            ),
            "pump_sell_count": sum(
                trade.side is TradeSide.SELL for trade in report.trades
            ),
            "traded_mint_count": len({trade.mint for trade in report.trades}),
            "repeat_bundler_entity_count": len(report.repeat_bundler_entities),
            "native_in_lamports": report.native_in_lamports,
            "native_out_lamports": report.native_out_lamports,
            "spl_transfer_count": sum(
                edge.transfer_count
                for edge in report.edges
                if edge.asset_kind is WalletAssetKind.TOKEN
            ),
            "multi_hop_count": (
                len(report.protection.multi_hops) if report.protection else 0
            ),
            "fresh_wallet_count": (
                sum(
                    item.status.value == "proven"
                    for item in report.protection.freshness
                )
                if report.protection
                else 0
            ),
        },
        "rug_evidence": rug_evidence_summary(report),
        "creator_history": creator_history_to_json(report.creator_history),
        "launches": [_launch_to_json(launch) for launch in report.launches],
        "linked_launches": [
            _launch_to_json(launch) for launch in report.linked_launches
        ],
        "wallet_switches": [
            {
                "as_of_slot": switch.as_of_slot,
                "linked_wallet": switch.linked_wallet,
                "transfer_source": switch.transfer_source,
                "transfer_target": switch.transfer_target,
                "launch_count": switch.launch_count,
                "early_launch_count": switch.early_launch_count,
                "first_transfer_slot": switch.first_transfer_slot,
                "last_transfer_slot": switch.last_transfer_slot,
                "first_launch_slot": switch.first_launch_slot,
                "last_launch_slot": switch.last_launch_slot,
                "evidence_ids": list(switch.evidence_ids),
            }
            for switch in report.wallet_switches
        ],
        "pump_trades": [
            {
                "slot": trade.slot,
                "transaction_index": trade.transaction_index,
                "outer_instruction_index": trade.outer_instruction_index,
                "signature": trade.signature,
                "mint": trade.mint,
                "side": trade.side.value,
                "wallet": trade.wallet,
                "created_at": trade.created_at,
            }
            for trade in report.trades
        ],
        "repeat_bundler_entities": [
            {
                "bundler_wallet": entity.bundler_wallet,
                "entity_creator": entity.entity_creator,
                "mints": list(entity.mints),
                "mint_count": len(entity.mints),
                "buy_count": entity.buy_count,
                "first_buy_slot": entity.first_buy_slot,
                "last_buy_slot": entity.last_buy_slot,
                "evidence_ids": list(entity.evidence_ids),
                "finalized_entity_attribution": True,
            }
            for entity in report.repeat_bundler_entities
        ],
        "transfers": [
            {
                "source": row.source,
                "target": row.target,
                "amount_lamports": row.amount_lamports,
                "slot": row.slot,
                "signature": row.signature,
                "event_index": row.event_index,
                "timestamp": row.timestamp,
            }
            for row in report.transfers
        ],
        "graph": {
            "nodes": [
                {
                    "id": node.address,
                    "address": node.address,
                    "is_target": node.is_target,
                    "scanned_transaction_count": node.scanned_transaction_count,
                    "launch_count": node.launch_count,
                    "first_seen_slot": node.first_seen_slot,
                    "last_seen_slot": node.last_seen_slot,
                    "roles": [
                        *node.roles,
                        *(
                            ("REPEAT_BUNDLER",)
                            if node.address in repeat_bundler_wallets
                            and "REPEAT_BUNDLER" not in node.roles
                            else ()
                        ),
                    ],
                    "fresh_wallet_status": node.fresh_wallet_status,
                }
                for node in report.nodes
            ],
            "edges": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "kind": (
                        "direct_native_transfer"
                        if edge.asset_kind is WalletAssetKind.NATIVE
                        else "direct_spl_transfer"
                    ),
                    "asset_kind": edge.asset_kind.value,
                    "asset_id": edge.asset_id,
                    "transfer_count": edge.transfer_count,
                    "amount_lamports": edge.amount_lamports,
                    "amount_base_units": edge.amount_base_units,
                    "first_slot": edge.first_slot,
                    "last_slot": edge.last_slot,
                    "evidence_ids": list(edge.evidence_ids),
                }
                for edge in report.edges
            ],
        },
        "protection": _protection_to_json(report.protection),
        "warnings": list(report.warnings),
    }


def rug_evidence_summary(report: WalletIntelligenceReport) -> dict[str, object]:
    """Summarize observed rug-relevant evidence without inventing a score.

    The result describes observable behavior and keeps insufficient history
    separate from a clean bill of health.
    """

    fresh_wallet_count = (
        sum(item.status.value == "proven" for item in report.protection.freshness)
        if report.protection
        else 0
    )
    multi_hop_count = len(report.protection.multi_hops) if report.protection else 0
    early_launch_count = sum(
        launch.position_is_zero_or_one for launch in report.launches
    )
    indexed_created_count = (
        report.creator_history.total_created_count
        if report.creator_history is not None
        else None
    )

    return {
        "launch_count": report.launch_count,
        "linked_launch_count": report.linked_launch_count,
        "early_position_launch_count": early_launch_count,
        "direct_linked_wallet_count": report.direct_linked_wallet_count,
        "linked_creator_wallet_count": report.linked_creator_wallet_count,
        "wallet_switch_candidate": report.wallet_switch_candidate,
        "wallet_switch_count": len(report.wallet_switches),
        "fresh_wallet_proven_count": fresh_wallet_count,
        "multi_hop_transfer_count": multi_hop_count,
        "repeat_bundler_entity_count": len(report.repeat_bundler_entities),
        "repeat_bundler_mint_count": sum(
            len(entity.mints) for entity in report.repeat_bundler_entities
        ),
        "native_in_lamports": report.native_in_lamports,
        "native_out_lamports": report.native_out_lamports,
        "history_is_bounded": True,
        "indexed_created_count": indexed_created_count,
    }


def abstention_to_json(result: AbstainResult) -> dict[str, object]:
    """Convert a typed abstention to the wallet command response shape."""

    return {
        "status": "abstain",
        "reason": result.reason.value,
        "message": result.message,
        "as_of_slot": result.as_of_slot,
    }


def _launch_to_json(launch: WalletLaunch) -> dict[str, object]:
    return {
        "slot": launch.slot,
        "transaction_index": launch.transaction_index,
        "signature": launch.signature,
        "mint": launch.mint,
        "name": launch.name,
        "symbol": launch.symbol,
        "creator": launch.creator,
        "position_is_zero_or_one": launch.position_is_zero_or_one,
        "creation_submitter": launch.creation_submitter,
        "fee_payer": launch.fee_payer,
        "first_buyer": launch.first_buyer,
        "observed_wallet": launch.observed_wallet,
        "bonding_curve": launch.bonding_curve,
        "created_at": launch.created_at,
    }


def _build_report(
    *,
    wallet: str,
    histories: dict[str, tuple[RawChainObservation, ...]],
    transfers: tuple[CanonicalTransferEvidence, ...],
    launches_by_wallet: dict[str, tuple[WalletLaunch, ...]],
    trades_by_wallet: dict[str, tuple[WalletPumpTrade, ...]],
    roles: tuple[WalletRoleEvidence, ...],
    protection: RuggerProtectionSnapshot,
    history_limit: int,
    warnings: tuple[str, ...],
    as_of_slot: int,
) -> WalletIntelligenceReport:
    observations = histories[wallet]
    launches = launches_by_wallet.get(wallet, ())
    linked_launches = tuple(
        sorted(
            (
                launch
                for address, address_launches in launches_by_wallet.items()
                if address != wallet
                for launch in address_launches
            ),
            key=lambda launch: (launch.slot, launch.signature),
        )
    )
    links = _links_from_transfers(transfers)
    wallet_switches = _wallet_switches(
        target_wallet=wallet,
        launches_by_wallet=launches_by_wallet,
        transfers=transfers,
        as_of_slot=as_of_slot,
    )
    trades = tuple(
        sorted(
            {
                (trade.signature, trade.outer_instruction_index): trade
                for address_trades in trades_by_wallet.values()
                for trade in address_trades
            }.values(),
            key=lambda trade: (
                trade.slot,
                trade.transaction_index,
                trade.outer_instruction_index,
            ),
        )
    )
    all_nodes = {wallet}
    all_nodes.update(peer for link in links for peer in (link.source, link.target))
    all_nodes.update(launches_by_wallet)
    all_nodes.update(role.wallet for role in roles)
    all_nodes.update(trade.wallet for trade in trades)
    linked_addresses = {
        peer
        for link in links
        for peer in (
            (link.target,)
            if link.source == wallet
            else (link.source,)
            if link.target == wallet
            else ()
        )
    }
    freshness_by_wallet = {
        item.wallet: item.status.value for item in protection.freshness
    }
    nodes = tuple(
        _node_for_address(
            address=address,
            target_wallet=wallet,
            observations=histories.get(address, ()),
            links=links,
            launches=launches_by_wallet.get(address, ()),
            role_evidence=roles,
            freshness_status=freshness_by_wallet.get(address, "unknown"),
        )
        for address in sorted(all_nodes)
    )
    in_amount = sum(
        edge.amount_lamports
        for edge in links
        if edge.target == wallet and edge.asset_kind is WalletAssetKind.NATIVE
    )
    out_amount = sum(
        edge.amount_lamports
        for edge in links
        if edge.source == wallet and edge.asset_kind is WalletAssetKind.NATIVE
    )
    first_seen, last_seen = _slot_bounds(observations)
    return WalletIntelligenceReport(
        as_of_slot=as_of_slot,
        target_wallet=wallet,
        history_limit=history_limit,
        scanned_transaction_count=len(observations),
        successful_transaction_count=sum(
            1 for observation in observations if _transaction_succeeded(observation)
        ),
        first_seen_slot=first_seen,
        last_seen_slot=last_seen,
        launch_count=len(launches),
        direct_linked_wallet_count=len(
            {
                peer
                for link in links
                for peer in (
                    (link.target,)
                    if link.source == wallet
                    else (link.source,)
                    if link.target == wallet
                    else ()
                )
            }
        ),
        linked_creator_wallet_count=len(
            {
                role.wallet
                for role in roles
                if (
                    role.wallet != wallet
                    and role.wallet in linked_addresses
                    and role.role is AddressRole.CREATOR
                )
            }
        ),
        wallet_switch_candidate=bool(wallet_switches),
        native_in_lamports=in_amount,
        native_out_lamports=out_amount,
        launches=launches,
        nodes=nodes,
        edges=links,
        warnings=(
            f"history is bounded to the newest {history_limit} finalized transactions",
            *warnings,
        ),
        protection=protection,
        linked_launches=linked_launches,
        wallet_switches=wallet_switches,
        linked_launch_count=len(linked_launches),
        early_launch_count=sum(launch.position_is_zero_or_one for launch in launches),
        linked_early_launch_count=sum(
            launch.position_is_zero_or_one for launch in linked_launches
        ),
        transfers=_transfer_rows(transfers),
        trades=trades,
    )


def _transfer_rows(
    transfers: tuple[CanonicalTransferEvidence, ...],
) -> tuple[TransferEvidenceRow, ...]:
    """Derive authoritative per-transfer rows from canonical transfer evidence.

    Evidence without a usable event index is skipped and logged rather than
    assigned a fabricated index.
    """
    rows: list[TransferEvidenceRow] = []
    for transfer in transfers:
        if type(transfer.event_index) is not int or transfer.event_index < 0:
            logger.warning(
                "skipping transfer evidence without a usable event index: %s",
                transfer.evidence_ids,
            )
            continue
        rows.append(
            TransferEvidenceRow(
                source=transfer.source_wallet,
                target=transfer.destination_wallet,
                amount_lamports=transfer.amount_base_units,
                slot=int(transfer.slot),
                signature=base58.b58encode(transfer.signature).decode("ascii"),
                event_index=transfer.event_index,
            )
        )
    return tuple(rows)


def _wallet_switches(
    *,
    target_wallet: str,
    launches_by_wallet: dict[str, tuple[WalletLaunch, ...]],
    transfers: tuple[CanonicalTransferEvidence, ...],
    as_of_slot: int,
) -> tuple[WalletSwitchEvidence, ...]:
    """Link a creator's launch history to a prior direct transfer only."""

    result: list[WalletSwitchEvidence] = []
    for linked_wallet, launches in sorted(launches_by_wallet.items()):
        if linked_wallet == target_wallet:
            continue
        creator_launches = tuple(
            launch
            for launch in launches
            if launch.creator == linked_wallet and launch.slot <= as_of_slot
        )
        if not creator_launches:
            continue
        direct_transfers = tuple(
            transfer
            for transfer in transfers
            if target_wallet in {transfer.source_wallet, transfer.destination_wallet}
            and linked_wallet in {transfer.source_wallet, transfer.destination_wallet}
            and transfer.source_wallet != transfer.destination_wallet
            and transfer.slot <= as_of_slot
        )
        matched_launches = tuple(
            launch
            for launch in creator_launches
            if any(transfer.slot < launch.slot for transfer in direct_transfers)
        )
        if not matched_launches:
            continue
        matched_transfers = tuple(
            transfer
            for transfer in direct_transfers
            if any(transfer.slot < launch.slot for launch in matched_launches)
        )
        if not matched_transfers:
            continue
        evidence_ids = {
            evidence_id
            for transfer in matched_transfers
            for evidence_id in transfer.evidence_ids
        }
        evidence_ids.update(f"launch:{launch.signature}" for launch in matched_launches)
        first_transfer = min(matched_transfers, key=lambda item: item.slot)
        last_transfer = max(matched_transfers, key=lambda item: item.slot)
        result.append(
            WalletSwitchEvidence(
                as_of_slot=as_of_slot,
                linked_wallet=linked_wallet,
                transfer_source=first_transfer.source_wallet,
                transfer_target=first_transfer.destination_wallet,
                launch_count=len(matched_launches),
                early_launch_count=sum(
                    launch.position_is_zero_or_one for launch in matched_launches
                ),
                first_transfer_slot=first_transfer.slot,
                last_transfer_slot=last_transfer.slot,
                first_launch_slot=min(launch.slot for launch in matched_launches),
                last_launch_slot=max(launch.slot for launch in matched_launches),
                evidence_ids=tuple(sorted(evidence_ids)),
            )
        )
    return tuple(result)


def _node_for_address(
    *,
    address: str,
    target_wallet: str,
    observations: tuple[RawChainObservation, ...],
    links: tuple[WalletLink, ...],
    launches: tuple[WalletLaunch, ...],
    role_evidence: tuple[WalletRoleEvidence, ...],
    freshness_status: str,
) -> WalletNode:
    first_seen, last_seen = _slot_bounds(observations)
    is_direct = any(
        address in {edge.source, edge.target}
        and target_wallet in {edge.source, edge.target}
        for edge in links
    )
    if address == target_wallet:
        node_roles = ["target"]
    elif is_direct:
        node_roles = ["direct_counterparty"]
    else:
        node_roles = ["expanded_counterparty"]
    if any(edge.source == address and edge.target == target_wallet for edge in links):
        node_roles.append("funding_source")
    if any(edge.source == target_wallet and edge.target == address for edge in links):
        node_roles.append("funded_counterparty")
    node_roles.extend(
        sorted({role.role.value for role in role_evidence if role.wallet == address})
    )
    if (
        any(launch.creator == address for launch in launches)
        and "creator" not in node_roles
    ):
        node_roles.append("creator")
    return WalletNode(
        address=address,
        is_target=address == target_wallet,
        scanned_transaction_count=len(observations),
        launch_count=len(launches),
        first_seen_slot=first_seen,
        last_seen_slot=last_seen,
        roles=tuple(dict.fromkeys(node_roles)),
        fresh_wallet_status=freshness_status,
    )


def _links_from_transfers(
    transfers: tuple[CanonicalTransferEvidence, ...],
) -> tuple[WalletLink, ...]:
    grouped: dict[
        tuple[str, str, WalletAssetKind, str], list[CanonicalTransferEvidence]
    ] = {}
    for transfer in transfers:
        grouped.setdefault(
            (
                transfer.source_wallet,
                transfer.destination_wallet,
                transfer.asset_kind,
                transfer.asset_id,
            ),
            [],
        ).append(transfer)
    links = []
    for (source, target, asset_kind, asset_id), items in sorted(
        grouped.items(),
        key=lambda item: (
            item[0][0],
            item[0][1],
            item[0][2].value,
            item[0][3],
        ),
    ):
        amount = sum(item.amount_base_units for item in items)
        links.append(
            WalletLink(
                source=source,
                target=target,
                transfer_count=len(items),
                amount_lamports=amount if asset_kind is WalletAssetKind.NATIVE else 0,
                first_slot=min(item.slot for item in items),
                last_slot=max(item.slot for item in items),
                evidence_ids=tuple(
                    sorted(
                        {
                            evidence_id
                            for item in items
                            for evidence_id in item.evidence_ids
                        }
                    )
                ),
                asset_kind=asset_kind,
                asset_id=asset_id,
                amount_base_units=amount,
            )
        )
    return tuple(links)


def _counterparties(
    wallet: str,
    transfers: tuple[CanonicalTransferEvidence, ...],
) -> set[str]:
    return {
        transfer.destination_wallet
        if transfer.source_wallet == wallet
        else transfer.source_wallet
        for transfer in transfers
        if wallet in {transfer.source_wallet, transfer.destination_wallet}
    }


def _roles_from_launches(
    launches_by_wallet: dict[str, tuple[WalletLaunch, ...]],
) -> tuple[WalletRoleEvidence, ...]:
    roles: list[WalletRoleEvidence] = []
    for launches in launches_by_wallet.values():
        for launch in launches:
            evidence_id = f"launch:{launch.signature}"
            role_values = (
                (AddressRole.CREATOR, launch.creator),
                (AddressRole.CREATION_SUBMITTER, launch.creation_submitter),
                (AddressRole.FEE_PAYER, launch.fee_payer),
                (AddressRole.FIRST_BUYER, launch.first_buyer),
            )
            for role, address in role_values:
                if not address:
                    continue
                roles.append(
                    WalletRoleEvidence(
                        as_of_slot=launch.slot,
                        wallet=address,
                        role=role,
                        observed_slot=launch.slot,
                        evidence_ids=(evidence_id,),
                    )
                )
    return tuple(
        sorted(
            {
                (
                    item.as_of_slot,
                    item.wallet,
                    item.role,
                    item.observed_slot,
                    item.evidence_ids,
                ): item
                for item in roles
            }.values(),
            key=lambda item: (item.wallet, item.role.value, item.observed_slot),
        )
    )


def _history_boundary(
    *,
    address: str,
    observations: tuple[RawChainObservation, ...],
    history_limit: int,
) -> WalletHistoryBoundary:
    first_seen, last_seen = _slot_bounds(observations)
    return WalletHistoryBoundary(
        as_of_slot=_max_slot(observations),
        wallet=address,
        first_observed_slot=first_seen,
        last_observed_slot=last_seen,
        observed_transaction_count=len(observations),
        requested_transaction_limit=history_limit,
        # The observer omits failed finalized transactions from its returned
        # tuple, so a short tuple does not prove that the requested history
        # window was fully consumed.
        history_complete=False,
        evidence_ids=tuple(
            sorted(
                _evidence_id(observation)
                for observation in observations
                if observation.signature is not None
            )
        )
        or (f"history:{address}:{_max_slot(observations)}",),
    )


def _transfer_key(transfer: CanonicalTransferEvidence) -> tuple[object, ...]:
    return (
        transfer.signature,
        transfer.event_index,
        transfer.source_wallet,
        transfer.destination_wallet,
        transfer.asset_kind,
        transfer.asset_id,
        transfer.amount_base_units,
    )


def _protection_to_json(
    protection: RuggerProtectionSnapshot | None,
) -> dict[str, object] | None:
    if protection is None:
        return None
    return {
        "as_of_slot": protection.as_of_slot,
        "target_wallet": protection.target_wallet,
        "reason_codes": list(protection.reason_codes),
        "roles": [
            {
                "wallet": item.wallet,
                "role": item.role.value,
                "observed_slot": item.observed_slot,
                "evidence_ids": list(item.evidence_ids),
            }
            for item in protection.roles
        ],
        "transfer_ranges": [
            {
                "source": item.source_wallet,
                "target": item.destination_wallet,
                "asset_kind": item.asset_kind.value,
                "asset_id": item.asset_id,
                "first_slot": item.first_slot,
                "last_slot": item.last_slot,
                "transfer_count": item.transfer_count,
                "amount_base_units": item.amount_base_units,
                "evidence_ids": list(item.evidence_ids),
            }
            for item in protection.transfer_ranges
        ],
        "multi_hops": [
            {
                "path": list(item.path),
                "hop_count": item.hop_count,
                "first_slot": item.first_slot,
                "last_slot": item.last_slot,
                "evidence_ids": list(item.evidence_ids),
            }
            for item in protection.multi_hops
        ],
        "freshness": [
            {
                "wallet": item.wallet,
                "first_observed_slot": item.first_observed_slot,
                "age_slots": item.age_slots,
                "status": item.status.value,
                "evidence_ids": list(item.evidence_ids),
            }
            for item in protection.freshness
        ],
    }


def _transfers_from_observations(
    observations: tuple[RawChainObservation, ...],
    *,
    as_of_slot: int,
) -> tuple[CanonicalTransferEvidence, ...] | AbstainResult:
    evidence: dict[tuple[object, ...], CanonicalTransferEvidence] = {}
    for observation in observations:
        if observation.slot > as_of_slot:
            return _abstain(
                "transfer evidence is newer than the requested cutoff",
                as_of_slot,
            )
        parsed = _parse_transfer_evidence(observation)
        if isinstance(parsed, AbstainResult):
            return parsed
        for transfer in parsed:
            evidence[_transfer_key(transfer)] = transfer
    return tuple(
        sorted(
            evidence.values(),
            key=lambda item: (
                int(item.slot),
                item.transaction_index,
                item.event_index,
                item.signature,
                item.source_wallet,
                item.destination_wallet,
            ),
        )
    )


def _parse_transfer_evidence(
    observation: RawChainObservation,
) -> tuple[CanonicalTransferEvidence, ...] | AbstainResult:
    if (
        observation.commitment != "finalized"
        or observation.canonical_status != "canonical"
        or observation.source_update_kind != "transaction"
        or not isinstance(observation.raw_source_payload, bytes)
        or not isinstance(observation.signature, bytes)
        or type(observation.transaction_index) is not int
    ):
        return _abstain(
            "transfer decoder requires finalized transaction evidence", observation.slot
        )
    try:
        envelope = json.loads(observation.raw_source_payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _abstain("wallet transaction evidence is invalid JSON", observation.slot)
    if not isinstance(envelope, Mapping):
        return _abstain("wallet transaction envelope is malformed", observation.slot)
    result = envelope.get("result")
    if not isinstance(result, Mapping):
        return _abstain("wallet transaction result is missing", observation.slot)
    message = _message(result)
    if isinstance(message, AbstainResult):
        return message
    account_keys, outer_instructions = message
    transaction = result.get("transaction")
    meta = result.get("meta")
    if not isinstance(transaction, Mapping) or not isinstance(meta, Mapping):
        return _abstain("wallet transaction envelope is incomplete", observation.slot)
    if result.get("slot") != observation.slot:
        return _abstain(
            "wallet transaction slot does not match observation", observation.slot
        )
    if meta.get("err") is not None:
        return ()
    signatures = transaction.get("signatures")
    signature_text = base58.b58encode(observation.signature).decode("ascii")
    if (
        not isinstance(signatures, list)
        or not signatures
        or signatures[0] != signature_text
    ):
        return _abstain(
            "wallet transaction signature does not match observation", observation.slot
        )

    indexed_instructions = [
        (index, instruction) for index, instruction in enumerate(outer_instructions)
    ]
    inner = meta.get("innerInstructions", [])
    if inner is None:
        inner = ()
    if not isinstance(inner, list):
        return _abstain("wallet inner instructions are malformed", observation.slot)
    for group_index, group in enumerate(inner):
        if not isinstance(group, Mapping):
            return _abstain(
                "wallet inner instruction group is malformed", observation.slot
            )
        group_instructions = group.get("instructions")
        if not isinstance(group_instructions, list):
            return _abstain("wallet inner instructions are missing", observation.slot)
        for inner_index, instruction in enumerate(group_instructions):
            indexed_instructions.append(
                (1_000_000 + group_index * 10_000 + inner_index, instruction)
            )

    token_candidates = _token_balance_candidates(meta, account_keys)
    parsed: list[CanonicalTransferEvidence] = []
    for event_index, instruction in indexed_instructions:
        if not isinstance(instruction, Mapping):
            return _abstain("wallet instruction is malformed", observation.slot)
        native = _system_transfer(instruction, account_keys)
        if isinstance(native, AbstainResult):
            return native
        if native is not None and native[2] > 0:
            parsed.append(
                _canonical_transfer(
                    observation=observation,
                    event_index=event_index,
                    source=native[0],
                    target=native[1],
                    asset_kind=WalletAssetKind.NATIVE,
                    asset_id="SOL",
                    amount=native[2],
                    evidence_suffix="native-transfer",
                )
            )
            continue
        spl = _spl_transfer(
            instruction=instruction,
            account_keys=account_keys,
            candidates=token_candidates,
            amount_as_of_slot=observation.slot,
        )
        if isinstance(spl, AbstainResult):
            return spl
        if spl is not None:
            source, target, mint, amount = spl
            parsed.append(
                _canonical_transfer(
                    observation=observation,
                    event_index=event_index,
                    source=source,
                    target=target,
                    asset_kind=WalletAssetKind.TOKEN,
                    asset_id=mint,
                    amount=amount,
                    evidence_suffix="spl-transfer",
                )
            )
    return tuple(parsed)


def _canonical_transfer(
    *,
    observation: RawChainObservation,
    event_index: int,
    source: str,
    target: str,
    asset_kind: WalletAssetKind,
    asset_id: str,
    amount: int,
    evidence_suffix: str,
) -> CanonicalTransferEvidence:
    signature = observation.signature
    if signature is None or observation.transaction_index is None:
        raise ValueError("canonical transfer requires transaction identity")
    signature_text = base58.b58encode(signature).decode("ascii")
    return CanonicalTransferEvidence(
        as_of_slot=observation.slot,
        slot=observation.slot,
        transaction_index=observation.transaction_index,
        event_index=event_index,
        signature=signature,
        evidence_ids=(f"transaction:{signature_text}:{evidence_suffix}:{event_index}",),
        source_wallet=source,
        destination_wallet=target,
        asset_kind=asset_kind,
        asset_id=asset_id,
        amount_base_units=amount,
    )


def _token_balance_candidates(
    meta: Mapping[str, object],
    account_keys: tuple[str, ...],
) -> dict[tuple[int, str, str], tuple[str, int, int]] | AbstainResult:
    pre = meta.get("preTokenBalances", ())
    post = meta.get("postTokenBalances", ())
    if pre is None:
        pre = ()
    if post is None:
        post = ()
    if not isinstance(pre, list) or not isinstance(post, list):
        return _abstain("token balance evidence is malformed", -1)
    parsed: dict[tuple[int, str, str], list[object]] = {}
    for side, rows in ((0, pre), (1, post)):
        for row in rows:
            if not isinstance(row, Mapping):
                return _abstain("token balance row is malformed", -1)
            account_index = row.get("accountIndex")
            mint = row.get("mint")
            owner = row.get("owner")
            program_id = row.get("programId")
            amount_object = row.get("uiTokenAmount")
            if (
                type(account_index) is not int
                or not 0 <= account_index < len(account_keys)
                or not isinstance(mint, str)
                or not mint
                or not isinstance(owner, str)
                or not owner
                or program_id not in (SPL_TOKEN_PROGRAM_ID, SPL_TOKEN_2022_PROGRAM_ID)
                or not isinstance(amount_object, Mapping)
                or not isinstance(amount_object.get("amount"), str)
                or not amount_object["amount"].isdigit()
            ):
                return _abstain("token balance proof fields are incomplete", -1)
            key = (account_index, mint, program_id)
            values = parsed.setdefault(key, [owner, None, None])
            if values[0] != owner or values[side + 1] is not None:
                return _abstain("token balance evidence conflicts", -1)
            values[side + 1] = int(amount_object["amount"])
    return {
        key: (values[0], values[1] or 0, values[2] or 0)
        for key, values in parsed.items()
    }


def _spl_transfer(
    *,
    instruction: Mapping[str, object],
    account_keys: tuple[str, ...],
    candidates: dict[tuple[int, str, str], tuple[str, int, int]] | AbstainResult,
    amount_as_of_slot: int,
) -> tuple[str, str, str, int] | AbstainResult | None:
    program_index = instruction.get("programIdIndex")
    accounts = instruction.get("accounts")
    encoded_data = instruction.get("data")
    if type(program_index) is not int or not isinstance(accounts, list):
        return _abstain("token instruction identity is malformed", amount_as_of_slot)
    if not 0 <= program_index < len(account_keys):
        return _abstain(
            "token instruction program index is out of bounds", amount_as_of_slot
        )
    if account_keys[program_index] not in (
        SPL_TOKEN_PROGRAM_ID,
        SPL_TOKEN_2022_PROGRAM_ID,
    ):
        return None
    if not isinstance(encoded_data, str):
        return _abstain("token instruction data is malformed", amount_as_of_slot)
    try:
        data = bytes(base58.b58decode(encoded_data))
    except ValueError:
        return _abstain("token instruction data is not base58", amount_as_of_slot)
    if not data or data[0] not in (SPL_TRANSFER_TAG, SPL_TRANSFER_CHECKED_TAG):
        return None
    minimum_accounts = 4 if data[0] == SPL_TRANSFER_CHECKED_TAG else 3
    if len(data) != (10 if data[0] == SPL_TRANSFER_CHECKED_TAG else 9):
        return _abstain("token transfer layout is unsupported", amount_as_of_slot)
    if len(accounts) < minimum_accounts or any(
        type(index) is not int for index in accounts
    ):
        return _abstain(
            "token transfer account layout is unsupported", amount_as_of_slot
        )
    if any(index < 0 or index >= len(account_keys) for index in accounts):
        return _abstain(
            "token transfer account index is out of bounds", amount_as_of_slot
        )
    if isinstance(candidates, AbstainResult):
        return candidates
    source_index = accounts[0]
    target_index = accounts[2] if data[0] == SPL_TRANSFER_CHECKED_TAG else accounts[1]
    if source_index == target_index:
        return None
    amount = int.from_bytes(data[1:9], "little")
    if amount <= 0:
        return None
    mint_hint = (
        account_keys[accounts[1]] if data[0] == SPL_TRANSFER_CHECKED_TAG else None
    )
    matches: list[tuple[str, str, str, int]] = []
    for (index, mint, _program_id), (source_owner, pre, post) in candidates.items():
        if index != source_index:
            continue
        if mint_hint is not None and mint != mint_hint:
            continue
        destination_rows = [
            (other_mint, other_program, owner, other_pre, other_post)
            for (other_index, other_mint, other_program), (
                owner,
                other_pre,
                other_post,
            ) in candidates.items()
            if other_index == target_index
            and other_mint == mint
            and other_program == _program_id
        ]
        if len(destination_rows) != 1:
            continue
        other_mint, _other_program, target_owner, target_pre, target_post = (
            destination_rows[0]
        )
        if post - pre != -amount or target_post - target_pre != amount:
            continue
        if source_owner == target_owner:
            continue
        matches.append((source_owner, target_owner, other_mint, amount))
    if len(matches) > 1:
        return _abstain("token transfer proof is ambiguous", amount_as_of_slot)
    return matches[0] if matches else None


def _launches_from_observations(
    observations: tuple[RawChainObservation, ...],
    *,
    observed_wallet: str,
    as_of_slot: int,
) -> tuple[WalletLaunch, ...] | AbstainResult:
    launches: dict[tuple[str, int], WalletLaunch] = {}
    for observation in observations:
        if _is_failed_transaction(observation):
            continue
        decoded = decode_pump_create_v2_observation(observation)
        if isinstance(decoded, AbstainResult):
            return decoded
        if decoded is None:
            continue
        if observation.slot > as_of_slot:
            return _abstain(
                "launch evidence is newer than the requested cutoff",
                as_of_slot,
            )
        if (
            observation.signature is None
            or observation.transaction_index is None
            or type(decoded.transaction_index) is not int
            or decoded.transaction_index != observation.transaction_index
            or decoded.transaction_index < 0
            or decoded.as_of_slot != observation.slot
        ):
            return _abstain(
                "launch transaction position does not match finalized evidence",
                observation.slot,
            )
        signature = base58.b58encode(observation.signature).decode("ascii")
        launch = WalletLaunch(
            slot=decoded.as_of_slot,
            transaction_index=decoded.transaction_index,
            signature=signature,
            mint=decoded.mint_pubkey,
            name=decoded.name,
            symbol=decoded.symbol,
            creator=decoded.creator_pubkey,
            position_is_zero_or_one=decoded.transaction_index in {0, 1},
            bonding_curve=decoded.bonding_curve_pubkey,
            creation_submitter=decoded.user_pubkey,
            fee_payer=decoded.fee_payer_pubkey,
            first_buyer=decoded.first_buyer_pubkey,
            observed_wallet=observed_wallet,
            created_at=_block_time_from_observation(observation),
        )
        identity = (signature, decoded.outer_instruction_index)
        previous = launches.get(identity)
        if previous is not None and previous != launch:
            return _abstain("duplicate launch evidence conflicts", observation.slot)
        launches[identity] = launch
    return tuple(
        sorted(launches.values(), key=lambda launch: (launch.slot, launch.signature))
    )


def _trades_from_observations(
    observations: tuple[RawChainObservation, ...],
    *,
    as_of_slot: int,
) -> tuple[WalletPumpTrade, ...] | AbstainResult:
    """Decode finalized Pump trades present in the observed wallet history."""

    trades: dict[tuple[str, int], WalletPumpTrade] = {}
    for observation in observations:
        if _is_failed_transaction(observation):
            continue
        if observation.slot > as_of_slot:
            return _abstain(
                "Pump trade evidence is newer than the requested cutoff",
                as_of_slot,
            )
        decoded = decode_pump_trade_observation(observation)
        if isinstance(decoded, AbstainResult):
            return decoded
        for trade in decoded:
            account_pubkeys = trade.account_pubkeys
            if (
                account_pubkeys is None
                or observation.signature is None
                or trade.signature != observation.signature
                or trade.as_of_slot != observation.slot
                or trade.transaction_index != observation.transaction_index
                or trade.transaction_index is None
                or trade.transaction_index < 0
                or not 0 <= trade.mint_account_index < len(account_pubkeys)
                or not 0 <= trade.user_account_index < len(account_pubkeys)
            ):
                return _abstain(
                    "Pump trade identity does not match finalized evidence",
                    observation.slot,
                )
            wallet = account_pubkeys[trade.user_account_index]
            signature = base58.b58encode(observation.signature).decode("ascii")
            item = WalletPumpTrade(
                slot=int(trade.as_of_slot),
                transaction_index=trade.transaction_index,
                outer_instruction_index=trade.outer_instruction_index,
                signature=signature,
                mint=account_pubkeys[trade.mint_account_index],
                side=trade.side,
                wallet=wallet,
                created_at=_block_time_from_observation(observation),
            )
            identity = (signature, trade.outer_instruction_index)
            previous = trades.get(identity)
            if previous is not None and previous != item:
                return _abstain(
                    "duplicate Pump trade evidence conflicts",
                    observation.slot,
                )
            trades[identity] = item
    return tuple(
        sorted(
            trades.values(),
            key=lambda trade: (
                trade.slot,
                trade.transaction_index,
                trade.outer_instruction_index,
            ),
        )
    )


async def _repeat_bundler_entities(
    trades: tuple[WalletPumpTrade, ...],
    *,
    endpoint: str,
) -> tuple[RepeatBundlerEntity, ...]:
    """Group exact creation-slot buys by finalized on-chain creator."""

    buys = tuple(trade for trade in trades if trade.side is TradeSide.BUY)
    mints = tuple(sorted({trade.mint for trade in buys}))
    if len(mints) < MIN_REPEAT_BUNDLER_MINTS:
        return ()
    resolutions = await asyncio.gather(
        *(
            asyncio.to_thread(resolve_token_or_wallet, mint, rpc_url=endpoint)
            for mint in mints
        )
    )
    bundler_mints: dict[tuple[str, str], set[str]] = {}
    creation_signatures: dict[str, str] = {}
    creation_slots: dict[str, int] = {}
    for mint, resolution in zip(mints, resolutions, strict=True):
        matching_buys = tuple(trade for trade in buys if trade.mint == mint)
        if (
            not resolution.is_token
            or resolution.creation_slot is None
            or resolution.creation_signature is None
            or not any(
                trade.slot == resolution.creation_slot for trade in matching_buys
            )
        ):
            continue
        for buy in matching_buys:
            if buy.slot == resolution.creation_slot:
                bundler_mints.setdefault(
                    (buy.wallet, resolution.target_wallet), set()
                ).add(mint)
        creation_signatures[mint] = resolution.creation_signature
        creation_slots[mint] = resolution.creation_slot

    result: list[RepeatBundlerEntity] = []
    for (bundler_wallet, entity), attributed_mints in sorted(bundler_mints.items()):
        if len(attributed_mints) < MIN_REPEAT_BUNDLER_MINTS:
            continue
        matching = tuple(
            trade
            for trade in buys
            if trade.mint in attributed_mints
            and trade.wallet == bundler_wallet
            and trade.slot == creation_slots[trade.mint]
        )
        result.append(
            RepeatBundlerEntity(
                bundler_wallet=bundler_wallet,
                entity_creator=entity,
                mints=tuple(sorted(attributed_mints)),
                buy_count=len(matching),
                first_buy_slot=min(trade.slot for trade in matching),
                last_buy_slot=max(trade.slot for trade in matching),
                evidence_ids=tuple(
                    sorted(
                        f"transaction:{trade.signature}:pump-buy:"
                        f"{trade.outer_instruction_index}"
                        for trade in matching
                    )
                    + [
                        f"transaction:{creation_signatures[mint]}:pump-create"
                        for mint in sorted(attributed_mints)
                    ]
                ),
            )
        )
    return tuple(result)


def _block_time_from_observation(observation: RawChainObservation) -> int | None:
    """Return the finalized RPC block time when it is present and valid."""

    try:
        envelope = json.loads(observation.raw_source_payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(envelope, Mapping):
        return None
    result = envelope.get("result")
    if not isinstance(result, Mapping):
        return None
    block_time = result.get("blockTime")
    return block_time if type(block_time) is int and block_time >= 0 else None


def _parse_native_transfers(
    observation: RawChainObservation,
) -> tuple[tuple[str, str, int], ...] | AbstainResult:
    try:
        envelope = json.loads(observation.raw_source_payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _abstain("wallet transaction evidence is invalid JSON", observation.slot)
    if not isinstance(envelope, Mapping):
        return _abstain("wallet transaction envelope is malformed", observation.slot)
    result = envelope.get("result")
    if not isinstance(result, Mapping):
        return _abstain("wallet transaction result is missing", observation.slot)
    message = _message(result)
    if isinstance(message, AbstainResult):
        return message
    account_keys, instructions = message
    transfers: list[tuple[str, str, int]] = []
    for instruction in instructions:
        transfer = _system_transfer(instruction, account_keys)
        if isinstance(transfer, AbstainResult):
            return transfer
        if transfer is not None:
            transfers.append(transfer)
    return tuple(transfers)


def _message(
    result: Mapping[str, object],
) -> tuple[tuple[str, ...], tuple[Mapping[str, object], ...]] | AbstainResult:
    transaction = result.get("transaction")
    meta = result.get("meta")
    if not isinstance(transaction, Mapping) or not isinstance(meta, Mapping):
        return _abstain("wallet transaction message is incomplete", -1)
    message = transaction.get("message")
    if not isinstance(message, Mapping):
        return _abstain("wallet transaction message is malformed", -1)
    static = message.get("accountKeys")
    if not isinstance(static, list) or any(
        not isinstance(item, str) for item in static
    ):
        return _abstain("wallet transaction account keys are malformed", -1)
    loaded = meta.get("loadedAddresses", {})
    if not isinstance(loaded, Mapping):
        return _abstain("wallet transaction loaded addresses are malformed", -1)
    writable = loaded.get("writable", [])
    readonly = loaded.get("readonly", [])
    if not isinstance(writable, list) or not isinstance(readonly, list):
        return _abstain("wallet transaction loaded addresses are malformed", -1)
    keys = (*static, *writable, *readonly)
    instructions = message.get("instructions")
    if not isinstance(instructions, list):
        return _abstain("wallet transaction instructions are missing", -1)
    if any(not isinstance(instruction, Mapping) for instruction in instructions):
        return _abstain("wallet transaction instructions are malformed", -1)
    return keys, tuple(instructions)


def _system_transfer(
    instruction: Mapping[str, object],
    account_keys: tuple[str, ...],
) -> tuple[str, str, int] | AbstainResult | None:
    program_index = instruction.get("programIdIndex")
    accounts = instruction.get("accounts")
    encoded_data = instruction.get("data")
    if type(program_index) is not int or not isinstance(accounts, list):
        return _abstain("wallet instruction identity is malformed", -1)
    if not 0 <= program_index < len(account_keys):
        return _abstain("wallet instruction program index is out of bounds", -1)
    if account_keys[program_index] != SYSTEM_PROGRAM_ID:
        return None
    if not isinstance(encoded_data, str):
        return _abstain("system instruction data is malformed", -1)
    try:
        data = bytes(base58.b58decode(encoded_data))
    except ValueError:
        return _abstain("system instruction data is not base58", -1)
    if not data.startswith(SYSTEM_TRANSFER_TAG):
        return None
    if (
        len(data) != SYSTEM_TRANSFER_DATA_LENGTH
        or len(accounts) != SYSTEM_TRANSFER_ACCOUNT_COUNT
        or any(type(index) is not int for index in accounts)
    ):
        return _abstain("system transfer layout is unsupported", -1)
    source_index, target_index = accounts
    if not 0 <= source_index < len(account_keys) or not 0 <= target_index < len(
        account_keys
    ):
        return _abstain("system transfer account index is out of bounds", -1)
    return (
        account_keys[source_index],
        account_keys[target_index],
        int.from_bytes(data[4:], "little"),
    )


def _transaction_succeeded(observation: RawChainObservation) -> bool:
    try:
        envelope = json.loads(observation.raw_source_payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    result = envelope.get("result") if isinstance(envelope, Mapping) else None
    meta = result.get("meta") if isinstance(result, Mapping) else None
    return isinstance(meta, Mapping) and meta.get("err") is None


def _is_failed_transaction(observation: RawChainObservation) -> bool:
    """Recognize a valid finalized failed transaction without decoding it."""

    try:
        envelope = json.loads(observation.raw_source_payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(envelope, Mapping):
        return False
    result = envelope.get("result")
    if not isinstance(result, Mapping) or result.get("slot") != observation.slot:
        return False
    meta = result.get("meta")
    return isinstance(meta, Mapping) and meta.get("err") is not None


def _evidence_id(observation: RawChainObservation) -> str:
    signature = (
        base58.b58encode(observation.signature).decode("ascii")
        if observation.signature is not None
        else str(observation.raw_id)
    )
    return f"transaction:{signature}:native-transfer"


def _slot_bounds(
    observations: tuple[RawChainObservation, ...],
) -> tuple[int | None, int | None]:
    if not observations:
        return None, None
    slots = tuple(observation.slot for observation in observations)
    return min(slots), max(slots)


def _max_slot(observations: tuple[RawChainObservation, ...]) -> int:
    return max((observation.slot for observation in observations), default=-1)


def _observations_at_or_before(
    observations: tuple[RawChainObservation, ...],
    as_of_slot: int,
) -> tuple[RawChainObservation, ...]:
    """Keep only finalized evidence visible at one chain-slot cutoff."""

    return tuple(
        sorted(
            (
                observation
                for observation in observations
                if observation.slot <= as_of_slot
            ),
            key=lambda observation: (
                observation.slot,
                observation.transaction_index
                if observation.transaction_index is not None
                else -1,
                observation.receive_sequence,
            ),
        )
    )


def _validate_request(
    *,
    wallet: str,
    endpoint: str,
    max_transactions: int,
    max_history_pages: int,
    max_linked_wallets: int,
    max_hops: int,
    fresh_wallet_window_slots: int,
    as_of_slot: int | None,
) -> AbstainResult | None:
    if not isinstance(wallet, str) or not wallet:
        return _abstain("wallet is required", -1)
    try:
        decoded = base58.b58decode(wallet)
    except ValueError:
        return _abstain("wallet is not valid base58", -1)
    if (
        len(decoded) != SOLANA_ADDRESS_BYTES
        or base58.b58encode(decoded).decode("ascii") != wallet
    ):
        return _abstain("wallet is not a valid Solana address", -1)
    if not isinstance(endpoint, str) or not endpoint.startswith(
        ("http://", "https://")
    ):
        return _abstain("HTTP RPC endpoint is required", -1)
    if (
        type(max_transactions) is not int
        or not 1 <= max_transactions <= MAX_HISTORY_TRANSACTIONS
    ):
        return _abstain("max_transactions must be between 1 and 100", -1)
    if (
        type(max_history_pages) is not int
        or not 1 <= max_history_pages <= MAX_HISTORY_PAGES
    ):
        return _abstain("max_history_pages must be between 1 and 100", -1)
    if (
        type(max_linked_wallets) is not int
        or not 0 <= max_linked_wallets <= MAX_LINKED_WALLETS
    ):
        return _abstain("max_linked_wallets must be between 0 and 20", -1)
    if type(max_hops) is not int or not 1 <= max_hops <= MAX_WALLET_HOPS:
        return _abstain("max_hops must be between 1 and 3", -1)
    if type(fresh_wallet_window_slots) is not int or fresh_wallet_window_slots < 0:
        return _abstain(
            "fresh_wallet_window_slots must be a non-negative integer",
            -1,
        )
    if as_of_slot is not None and (type(as_of_slot) is not int or as_of_slot < 0):
        return _abstain("as_of_slot must be a non-negative integer", -1)
    return None


def _abstain(message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNKNOWN_PROTOCOL_STATE,
        message=message,
        as_of_slot=as_of_slot,
    )
