"""Pure point-in-time wallet evidence for rugger protection."""

# The validators are intentionally branch-heavy and fail closed.
# ruff: noqa: C901, PLR0911, PLR0913, PLR2004

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from itertools import pairwise

from rugbot.domain.account_roles import AddressRole
from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.intelligence.wallet_behavior import (
    CanonicalTransferEvidence,
    WalletAssetKind,
)


class FreshWalletStatus(Enum):
    """Evidence status for a wallet's first-observed slot."""

    PROVEN = "proven"
    NOT_FRESH = "not_fresh"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WalletRoleEvidence:
    """Direct launch or transfer evidence for one observed wallet role."""

    as_of_slot: Slot
    wallet: str
    role: AddressRole
    observed_slot: Slot
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WalletHistoryBoundary:
    """Bounded history metadata used for conservative freshness checks."""

    as_of_slot: Slot
    wallet: str
    first_observed_slot: Slot | None
    last_observed_slot: Slot | None
    observed_transaction_count: int
    requested_transaction_limit: int
    history_complete: bool
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WalletTransferRange:
    """Observed directed transfer range for one asset and wallet pair."""

    as_of_slot: Slot
    source_wallet: str
    destination_wallet: str
    asset_kind: WalletAssetKind
    asset_id: str
    first_slot: Slot
    last_slot: Slot
    transfer_count: int
    amount_base_units: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WalletHop:
    """Observed directed multi-hop transfer path, not an ownership claim."""

    as_of_slot: Slot
    path: tuple[str, ...]
    hop_count: int
    first_slot: Slot
    last_slot: Slot
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WalletFreshnessEvidence:
    """Point-in-time freshness result with an explicit unknown state."""

    as_of_slot: Slot
    wallet: str
    first_observed_slot: Slot | None
    age_slots: int | None
    status: FreshWalletStatus
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuggerProtectionSnapshot:
    """Immutable rugger-protection evidence snapshot at one slot boundary."""

    as_of_slot: Slot
    target_wallet: str
    roles: tuple[WalletRoleEvidence, ...]
    transfer_ranges: tuple[WalletTransferRange, ...]
    multi_hops: tuple[WalletHop, ...]
    freshness: tuple[WalletFreshnessEvidence, ...]
    reason_codes: tuple[str, ...]


RuggerProtectionResult = RuggerProtectionSnapshot | AbstainResult


def build_rugger_protection_snapshot(
    *,
    transfers: tuple[CanonicalTransferEvidence, ...],
    roles: tuple[WalletRoleEvidence, ...],
    histories: tuple[WalletHistoryBoundary, ...],
    target_wallet: str,
    as_of_slot: Slot,
    max_hops: int = 3,
    fresh_wallet_window_slots: int = 10_000,
) -> RuggerProtectionResult:
    """Build conservative transfer, role, hop, and freshness evidence.

    A transfer path describes observed asset movement only. It never upgrades
    proximity into same-controller identity. Freshness is ``UNKNOWN`` unless
    the supplied history explicitly proves its boundary is complete.
    """

    validation_error = _validate_request(
        transfers=transfers,
        roles=roles,
        histories=histories,
        target_wallet=target_wallet,
        as_of_slot=as_of_slot,
        max_hops=max_hops,
        fresh_wallet_window_slots=fresh_wallet_window_slots,
    )
    if validation_error is not None:
        return validation_error

    transfer_ranges = _transfer_ranges(transfers, as_of_slot)
    multi_hops = _multi_hops(
        transfers=transfers,
        target_wallet=target_wallet,
        as_of_slot=as_of_slot,
        max_hops=max_hops,
    )
    freshness = _freshness(
        histories=histories,
        as_of_slot=as_of_slot,
        fresh_wallet_window_slots=fresh_wallet_window_slots,
    )
    reason_codes = _reason_codes(
        roles=roles,
        transfer_ranges=transfer_ranges,
        multi_hops=multi_hops,
        freshness=freshness,
    )
    return RuggerProtectionSnapshot(
        as_of_slot=as_of_slot,
        target_wallet=target_wallet,
        roles=tuple(sorted(roles, key=_role_sort_key)),
        transfer_ranges=transfer_ranges,
        multi_hops=multi_hops,
        freshness=tuple(sorted(freshness, key=lambda item: item.wallet)),
        reason_codes=reason_codes,
    )


def _validate_request(
    *,
    transfers: tuple[CanonicalTransferEvidence, ...],
    roles: tuple[WalletRoleEvidence, ...],
    histories: tuple[WalletHistoryBoundary, ...],
    target_wallet: str,
    as_of_slot: Slot,
    max_hops: int,
    fresh_wallet_window_slots: int,
) -> AbstainResult | None:
    if not _non_negative_int(as_of_slot):
        return _unsupported("as_of_slot must be a non-negative integer", as_of_slot)
    if not _non_empty_str(target_wallet):
        return _missing("target_wallet is required", as_of_slot)
    if not all(isinstance(items, tuple) for items in (transfers, roles, histories)):
        return _missing("rugger evidence collections must be typed tuples", as_of_slot)
    if type(max_hops) is not int or not 1 <= max_hops <= 8:
        return _unsupported("max_hops must be between 1 and 8", as_of_slot)
    if type(fresh_wallet_window_slots) is not int or fresh_wallet_window_slots < 0:
        return _unsupported(
            "fresh_wallet_window_slots must be a non-negative integer", as_of_slot
        )
    for transfer in transfers:
        error = _validate_transfer(transfer, as_of_slot)
        if error is not None:
            return error
    for role in roles:
        error = _validate_role(role, as_of_slot)
        if error is not None:
            return error
    for history in histories:
        error = _validate_history(history, as_of_slot)
        if error is not None:
            return error
    return None


def _validate_transfer(
    transfer: object,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not isinstance(transfer, CanonicalTransferEvidence):
        return _missing("transfer evidence type is incomplete", as_of_slot)
    if (
        not _non_negative_int(transfer.as_of_slot)
        or not _non_negative_int(transfer.slot)
        or transfer.as_of_slot > as_of_slot
        or transfer.slot > transfer.as_of_slot
    ):
        return _stale("transfer evidence is newer than as_of_slot", as_of_slot)
    if not _wallet_pair(transfer.source_wallet, transfer.destination_wallet):
        return _missing("transfer wallets are required and must differ", as_of_slot)
    if not isinstance(transfer.asset_kind, WalletAssetKind):
        return _unsupported("transfer asset kind is invalid", as_of_slot)
    if not _non_empty_str(transfer.asset_id):
        return _missing("transfer asset_id is required", as_of_slot)
    if type(transfer.amount_base_units) is not int or transfer.amount_base_units <= 0:
        return _missing(
            "transfer amount must be positive integer base units", as_of_slot
        )
    if not isinstance(transfer.signature, bytes) or not transfer.signature:
        return _missing("transfer signature is required", as_of_slot)
    if not _valid_evidence_ids(transfer.evidence_ids):
        return _missing("transfer evidence_ids are required", as_of_slot)
    return None


def _validate_role(role: object, as_of_slot: Slot) -> AbstainResult | None:
    if not isinstance(role, WalletRoleEvidence):
        return _missing("wallet role evidence type is incomplete", as_of_slot)
    if (
        not _non_negative_int(role.as_of_slot)
        or not _non_negative_int(role.observed_slot)
        or role.as_of_slot > as_of_slot
        or role.observed_slot > role.as_of_slot
    ):
        return _stale("wallet role evidence is newer than as_of_slot", as_of_slot)
    if not _non_empty_str(role.wallet):
        return _missing("wallet role address is required", as_of_slot)
    if not isinstance(role.role, AddressRole):
        return _unsupported("wallet role is invalid", as_of_slot)
    if not _valid_evidence_ids(role.evidence_ids):
        return _missing("wallet role evidence_ids are required", as_of_slot)
    return None


def _validate_history(
    history: object,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not isinstance(history, WalletHistoryBoundary):
        return _missing("wallet history boundary type is incomplete", as_of_slot)
    if (
        not _non_negative_int(history.as_of_slot)
        or history.as_of_slot > as_of_slot
        or type(history.observed_transaction_count) is not int
        or history.observed_transaction_count < 0
        or type(history.requested_transaction_limit) is not int
        or history.requested_transaction_limit <= 0
        or type(history.history_complete) is not bool
    ):
        return _unsupported("wallet history boundary is malformed", as_of_slot)
    if history.observed_transaction_count > history.requested_transaction_limit:
        return _unsupported("wallet history exceeds its requested limit", as_of_slot)
    if history.first_observed_slot is None:
        if history.last_observed_slot is not None:
            return _unsupported("wallet history slot bounds are malformed", as_of_slot)
    elif (
        not _non_negative_int(history.first_observed_slot)
        or not _non_negative_int(history.last_observed_slot)
        or history.first_observed_slot > history.last_observed_slot
        or history.last_observed_slot > history.as_of_slot
    ):
        return _stale("wallet history slot bounds are malformed", as_of_slot)
    if not _valid_evidence_ids(history.evidence_ids):
        return _missing("wallet history evidence_ids are required", as_of_slot)
    return None


def _transfer_ranges(
    transfers: tuple[CanonicalTransferEvidence, ...],
    as_of_slot: Slot,
) -> tuple[WalletTransferRange, ...]:
    grouped: dict[tuple[str, str, WalletAssetKind, str], list[object]] = {}
    for transfer in transfers:
        key = (
            transfer.source_wallet,
            transfer.destination_wallet,
            transfer.asset_kind,
            transfer.asset_id,
        )
        grouped.setdefault(key, []).append(transfer)
    ranges = []
    for (source, target, asset_kind, asset_id), items in sorted(
        grouped.items(), key=lambda item: _range_sort_key(item[0])
    ):
        typed_items = tuple(items)
        ranges.append(
            WalletTransferRange(
                as_of_slot=as_of_slot,
                source_wallet=source,
                destination_wallet=target,
                asset_kind=asset_kind,
                asset_id=asset_id,
                first_slot=Slot(min(item.slot for item in typed_items)),
                last_slot=Slot(max(item.slot for item in typed_items)),
                transfer_count=len(typed_items),
                amount_base_units=sum(item.amount_base_units for item in typed_items),
                evidence_ids=tuple(
                    sorted(
                        {
                            evidence_id
                            for item in typed_items
                            for evidence_id in item.evidence_ids
                        }
                    )
                ),
            )
        )
    return tuple(ranges)


def _multi_hops(
    *,
    transfers: tuple[CanonicalTransferEvidence, ...],
    target_wallet: str,
    as_of_slot: Slot,
    max_hops: int,
) -> tuple[WalletHop, ...]:
    adjacency: dict[str, list[CanonicalTransferEvidence]] = {}
    for transfer in transfers:
        adjacency.setdefault(transfer.source_wallet, []).append(transfer)
    for items in adjacency.values():
        items.sort(
            key=lambda item: (item.destination_wallet, item.slot, item.evidence_ids)
        )

    discovered: dict[tuple[str, ...], WalletHop] = {}
    queue: list[tuple[str, ...]] = [(target_wallet,)]
    while queue:
        path = queue.pop(0)
        if len(path) - 1 >= max_hops:
            continue
        for transfer in adjacency.get(path[-1], ()):
            if transfer.destination_wallet in path:
                continue
            next_path = (*path, transfer.destination_wallet)
            if len(next_path) >= 3:
                path_transfers = _path_transfers(path=next_path, transfers=transfers)
                discovered[next_path] = WalletHop(
                    as_of_slot=as_of_slot,
                    path=next_path,
                    hop_count=len(next_path) - 1,
                    first_slot=Slot(min(item.slot for item in path_transfers)),
                    last_slot=Slot(max(item.slot for item in path_transfers)),
                    evidence_ids=tuple(
                        sorted(
                            {
                                evidence_id
                                for item in path_transfers
                                for evidence_id in item.evidence_ids
                            }
                        )
                    ),
                )
            queue.append(next_path)
    return tuple(
        sorted(discovered.values(), key=lambda item: (item.hop_count, item.path))
    )


def _path_transfers(
    *,
    path: tuple[str, ...],
    transfers: tuple[CanonicalTransferEvidence, ...],
) -> tuple[CanonicalTransferEvidence, ...]:
    selected: list[CanonicalTransferEvidence] = []
    for source, target in pairwise(path):
        candidates = tuple(
            transfer
            for transfer in transfers
            if transfer.source_wallet == source
            and transfer.destination_wallet == target
        )
        if candidates:
            selected.append(
                max(candidates, key=lambda item: (item.slot, item.evidence_ids))
            )
    return tuple(selected)


def _freshness(
    *,
    histories: tuple[WalletHistoryBoundary, ...],
    as_of_slot: Slot,
    fresh_wallet_window_slots: int,
) -> tuple[WalletFreshnessEvidence, ...]:
    rows = []
    for history in histories:
        if history.first_observed_slot is None or not history.history_complete:
            status = FreshWalletStatus.UNKNOWN
            age_slots = None
        else:
            age_slots = as_of_slot - history.first_observed_slot
            status = (
                FreshWalletStatus.PROVEN
                if age_slots <= fresh_wallet_window_slots
                else FreshWalletStatus.NOT_FRESH
            )
        rows.append(
            WalletFreshnessEvidence(
                as_of_slot=as_of_slot,
                wallet=history.wallet,
                first_observed_slot=history.first_observed_slot,
                age_slots=age_slots,
                status=status,
                evidence_ids=history.evidence_ids,
            )
        )
    return tuple(rows)


def _reason_codes(
    *,
    roles: tuple[WalletRoleEvidence, ...],
    transfer_ranges: tuple[WalletTransferRange, ...],
    multi_hops: tuple[WalletHop, ...],
    freshness: tuple[WalletFreshnessEvidence, ...],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if roles:
        reasons.append("direct_wallet_roles_observed")
    if transfer_ranges:
        reasons.append("direct_transfer_ranges_observed")
    if multi_hops:
        reasons.append("multi_hop_transfer_paths_observed")
    if any(item.status is FreshWalletStatus.PROVEN for item in freshness):
        reasons.append("fresh_wallets_proven")
    if any(item.status is FreshWalletStatus.UNKNOWN for item in freshness):
        reasons.append("freshness_abstained_for_incomplete_history")
    if not reasons:
        reasons.append("no_rugger_protection_evidence")
    return tuple(reasons)


def _role_sort_key(role: WalletRoleEvidence) -> tuple[str, str, int, tuple[str, ...]]:
    return role.wallet, role.role.value, int(role.observed_slot), role.evidence_ids


def _range_sort_key(
    key: tuple[str, str, WalletAssetKind, str],
) -> tuple[str, str, str, str]:
    source, target, asset_kind, asset_id = key
    return source, target, asset_kind.value, asset_id


def _valid_evidence_ids(value: object) -> bool:
    return (
        isinstance(value, tuple)
        and bool(value)
        and len(set(value)) == len(value)
        and all(_non_empty_str(item) for item in value)
    )


def _wallet_pair(source: object, target: object) -> bool:
    return _non_empty_str(source) and _non_empty_str(target) and source != target


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0


def _missing(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.MISSING_FEATURE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _stale(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.STALE_STATE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _unsupported(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
        message=message,
        as_of_slot=_abstain_slot(as_of_slot),
    )


def _abstain_slot(as_of_slot: object) -> int:
    return as_of_slot if type(as_of_slot) is int else -1


__all__ = [
    "FreshWalletStatus",
    "RuggerProtectionResult",
    "RuggerProtectionSnapshot",
    "WalletFreshnessEvidence",
    "WalletHistoryBoundary",
    "WalletHop",
    "WalletRoleEvidence",
    "WalletTransferRange",
    "build_rugger_protection_snapshot",
]
