"""Pure point-in-time wallet behavior ledger construction.

This module consumes canonical, typed evidence only. It deliberately does not
decode chain payloads, resolve entities, or infer control from an observed
transfer. A funding relationship is reported as an observed native-asset
flow, not as proof that either wallet controls the other.
"""

from dataclasses import dataclass
from enum import Enum
from itertools import combinations
from typing import TypeAlias

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult


class WalletAssetKind(Enum):
    """Asset class used by a canonical wallet flow."""

    NATIVE = "native"
    TOKEN = "token"  # noqa: S105
    QUOTE = "quote"


@dataclass(frozen=True, slots=True)
class CanonicalTransferEvidence:
    """Canonical finalized transfer evidence between two wallets."""

    as_of_slot: Slot
    slot: Slot
    transaction_index: int
    event_index: int
    signature: bytes
    evidence_ids: tuple[str, ...]
    source_wallet: str
    destination_wallet: str
    asset_kind: WalletAssetKind
    asset_id: str
    amount_base_units: int


@dataclass(frozen=True, slots=True)
class CanonicalBuyEvidence:
    """Canonical finalized token buy evidence for one wallet."""

    as_of_slot: Slot
    slot: Slot
    transaction_index: int
    event_index: int
    signature: bytes
    evidence_ids: tuple[str, ...]
    wallet: str
    token_mint: str
    base_amount_base_units: int
    quote_asset_kind: WalletAssetKind
    quote_asset_id: str
    quote_amount_base_units: int


@dataclass(frozen=True, slots=True)
class CanonicalSellEvidence:
    """Canonical finalized token sell evidence and its quote destination."""

    as_of_slot: Slot
    slot: Slot
    transaction_index: int
    event_index: int
    signature: bytes
    evidence_ids: tuple[str, ...]
    wallet: str
    token_mint: str
    base_amount_base_units: int
    quote_asset_kind: WalletAssetKind
    quote_asset_id: str
    quote_amount_base_units: int
    destination_wallet: str


@dataclass(frozen=True, slots=True)
class WalletAssetFlow:
    """Observed inflow and outflow totals for one wallet and asset."""

    as_of_slot: Slot
    wallet: str
    asset_kind: WalletAssetKind
    asset_id: str
    inflow_base_units: int
    outflow_base_units: int


@dataclass(frozen=True, slots=True)
class WalletInventoryPosition:
    """Token inventory reconstructed at the requested slot boundary."""

    as_of_slot: Slot
    wallet: str
    token_mint: str
    inflow_base_units: int
    outflow_base_units: int
    balance_base_units: int


@dataclass(frozen=True, slots=True)
class WalletSellDestination:
    """Observed sell proceeds routed from one seller to one destination."""

    as_of_slot: Slot
    wallet: str
    destination_wallet: str
    sell_count: int
    token_amount_base_units: int
    quote_amount_base_units: int


@dataclass(frozen=True, slots=True)
class WalletFundingRelationship:
    """Observed native-asset transfer relationship, without control inference."""

    as_of_slot: Slot
    source_wallet: str
    destination_wallet: str
    transfer_count: int
    amount_base_units: int


@dataclass(frozen=True, slots=True)
class WalletCounterpartyCount:
    """Repeated co-buy or co-sell count for one wallet pair."""

    as_of_slot: Slot
    wallet: str
    counterparty_wallet: str
    count: int


@dataclass(frozen=True, slots=True)
class WalletBehaviorSummary:
    """Deterministic behavior summary for one wallet at one slot boundary."""

    as_of_slot: Slot
    wallet: str
    first_seen_slot: Slot
    last_seen_slot: Slot
    transfer_count: int
    buy_count: int
    sell_count: int
    asset_flows: tuple[WalletAssetFlow, ...]
    inventory: tuple[WalletInventoryPosition, ...]
    sell_destinations: tuple[WalletSellDestination, ...]
    funding_relationships: tuple[WalletFundingRelationship, ...]
    co_buy_counts: tuple[WalletCounterpartyCount, ...]
    co_sell_counts: tuple[WalletCounterpartyCount, ...]


@dataclass(frozen=True, slots=True)
class WalletBehaviorLedger:
    """Complete deterministic wallet behavior ledger at one slot boundary."""

    as_of_slot: Slot
    wallets: tuple[WalletBehaviorSummary, ...]
    transfer_count: int
    buy_count: int
    sell_count: int
    source_evidence_count: int
    deduplicated_evidence_count: int


WalletBehaviorLedgerResult: TypeAlias = WalletBehaviorLedger | AbstainResult
_Evidence: TypeAlias = (
    CanonicalTransferEvidence | CanonicalBuyEvidence | CanonicalSellEvidence
)
_EventKey: TypeAlias = tuple[int, int, int, bytes]


def build_wallet_behavior_ledger(  # noqa: PLR0911
    *,
    transfers: tuple[CanonicalTransferEvidence, ...],
    buys: tuple[CanonicalBuyEvidence, ...],
    sells: tuple[CanonicalSellEvidence, ...],
    as_of_slot: Slot,
) -> WalletBehaviorLedgerResult:
    """Reduce canonical transfer, buy, and sell evidence into a snapshot.

    Evidence is accepted only when its event slot and availability boundary
    are both at or before ``as_of_slot``. The reducer is idempotent for exact
    duplicate evidence and abstains on conflicting identities, missing
    provenance, ambiguous ordering, or negative reconstructed token inventory.
    """

    request_error = _validate_request(
        transfers=transfers,
        buys=buys,
        sells=sells,
        as_of_slot=as_of_slot,
    )
    if request_error is not None:
        return request_error

    source_evidence_count = len(transfers) + len(buys) + len(sells)
    evidence_by_key: dict[_EventKey, _Evidence] = {}
    evidence_by_id: dict[str, _Evidence] = {}
    for evidence in (*transfers, *buys, *sells):
        evidence_error = _validate_evidence(evidence, as_of_slot)
        if evidence_error is not None:
            return evidence_error
        event_key = _event_key(evidence)
        existing = evidence_by_key.get(event_key)
        if existing is not None and existing != evidence:
            return _conflict(
                "canonical event identity has conflicting evidence", as_of_slot
            )
        for evidence_id in evidence.evidence_ids:
            existing_by_id = evidence_by_id.get(evidence_id)
            if existing_by_id is not None and existing_by_id != evidence:
                return _conflict(
                    "evidence_id has conflicting canonical evidence", as_of_slot
                )
            evidence_by_id[evidence_id] = evidence
        evidence_by_key[event_key] = evidence

    if not evidence_by_key:
        return _missing("wallet behavior evidence is required", as_of_slot)

    state = _MutableLedgerState()
    ordered_evidence = tuple(sorted(evidence_by_key.values(), key=_event_sort_key))
    for evidence in ordered_evidence:
        apply_error = state.apply(evidence, as_of_slot)
        if apply_error is not None:
            return apply_error

    return state.snapshot(
        as_of_slot=as_of_slot,
        source_evidence_count=source_evidence_count,
        deduplicated_evidence_count=len(ordered_evidence),
    )


@dataclass(slots=True)
class _MutableLedgerState:
    wallets: set[str]
    first_seen: dict[str, int]
    last_seen: dict[str, int]
    transfer_counts: dict[str, int]
    buy_counts: dict[str, int]
    sell_counts: dict[str, int]
    flows: dict[str, dict[tuple[WalletAssetKind, str], list[int]]]
    inventory: dict[tuple[str, str], list[int]]
    sell_destinations: dict[tuple[str, str], list[int]]
    funding: dict[tuple[str, str], list[int]]
    buys_by_token: dict[str, set[str]]
    sells_by_token: dict[str, set[str]]

    def __init__(self) -> None:
        self.wallets = set()
        self.first_seen = {}
        self.last_seen = {}
        self.transfer_counts = {}
        self.buy_counts = {}
        self.sell_counts = {}
        self.flows = {}
        self.inventory = {}
        self.sell_destinations = {}
        self.funding = {}
        self.buys_by_token = {}
        self.sells_by_token = {}

    def apply(
        self,
        evidence: _Evidence,
        as_of_slot: Slot,
    ) -> AbstainResult | None:
        if isinstance(evidence, CanonicalTransferEvidence):
            return self._apply_transfer(evidence, as_of_slot)
        if isinstance(evidence, CanonicalBuyEvidence):
            self._apply_buy(evidence)
            return None
        if isinstance(evidence, CanonicalSellEvidence):
            return self._apply_sell(evidence, as_of_slot)
        return _unsupported("unknown wallet behavior evidence type", as_of_slot)

    def _apply_transfer(
        self,
        evidence: CanonicalTransferEvidence,
        as_of_slot: Slot,
    ) -> AbstainResult | None:
        self._touch(evidence.source_wallet, evidence.slot)
        self._touch(evidence.destination_wallet, evidence.slot)
        self._flow(
            evidence.source_wallet,
            evidence.asset_kind,
            evidence.asset_id,
            inflow=0,
            outflow=evidence.amount_base_units,
        )
        self._flow(
            evidence.destination_wallet,
            evidence.asset_kind,
            evidence.asset_id,
            inflow=evidence.amount_base_units,
            outflow=0,
        )
        if evidence.asset_kind is WalletAssetKind.TOKEN:
            source_inventory_ok = self._inventory_delta(
                wallet=evidence.source_wallet,
                token_mint=evidence.asset_id,
                inflow=0,
                outflow=evidence.amount_base_units,
            )
            if not source_inventory_ok:
                return _missing(
                    "transfer exceeds reconstructed token inventory", as_of_slot
                )
            self._inventory_delta(
                wallet=evidence.destination_wallet,
                token_mint=evidence.asset_id,
                inflow=evidence.amount_base_units,
                outflow=0,
            )
        if evidence.asset_kind is WalletAssetKind.NATIVE:
            relationship = self.funding.setdefault(
                (evidence.source_wallet, evidence.destination_wallet),
                [0, 0],
            )
            relationship[0] += 1
            relationship[1] += evidence.amount_base_units
        self._increment(self.transfer_counts, evidence.source_wallet)
        self._increment(self.transfer_counts, evidence.destination_wallet)
        return None

    def _apply_buy(self, evidence: CanonicalBuyEvidence) -> None:
        self._touch(evidence.wallet, evidence.slot)
        self._flow(
            evidence.wallet,
            WalletAssetKind.TOKEN,
            evidence.token_mint,
            inflow=evidence.base_amount_base_units,
            outflow=0,
        )
        self._flow(
            evidence.wallet,
            evidence.quote_asset_kind,
            evidence.quote_asset_id,
            inflow=0,
            outflow=evidence.quote_amount_base_units,
        )
        self._inventory_delta(
            wallet=evidence.wallet,
            token_mint=evidence.token_mint,
            inflow=evidence.base_amount_base_units,
            outflow=0,
        )
        self._increment(self.buy_counts, evidence.wallet)
        self.buys_by_token.setdefault(evidence.token_mint, set()).add(evidence.wallet)

    def _apply_sell(
        self,
        evidence: CanonicalSellEvidence,
        as_of_slot: Slot,
    ) -> AbstainResult | None:
        self._touch(evidence.wallet, evidence.slot)
        self._touch(evidence.destination_wallet, evidence.slot)
        self._flow(
            evidence.wallet,
            WalletAssetKind.TOKEN,
            evidence.token_mint,
            inflow=0,
            outflow=evidence.base_amount_base_units,
        )
        self._flow(
            evidence.destination_wallet,
            evidence.quote_asset_kind,
            evidence.quote_asset_id,
            inflow=evidence.quote_amount_base_units,
            outflow=0,
        )
        inventory_error = self._inventory_delta(
            wallet=evidence.wallet,
            token_mint=evidence.token_mint,
            inflow=0,
            outflow=evidence.base_amount_base_units,
        )
        if inventory_error is False:
            return _missing(
                "sell evidence exceeds reconstructed token inventory", as_of_slot
            )
        self._increment(self.sell_counts, evidence.wallet)
        self.sells_by_token.setdefault(evidence.token_mint, set()).add(evidence.wallet)
        destination = self.sell_destinations.setdefault(
            (evidence.wallet, evidence.destination_wallet),
            [0, 0, 0],
        )
        destination[0] += 1
        destination[1] += evidence.base_amount_base_units
        destination[2] += evidence.quote_amount_base_units
        return None

    def _touch(self, wallet: str, slot: Slot) -> None:
        self.wallets.add(wallet)
        slot_value = int(slot)
        if wallet not in self.first_seen:
            self.first_seen[wallet] = slot_value
        self.first_seen[wallet] = min(self.first_seen[wallet], slot_value)
        self.last_seen[wallet] = max(self.last_seen.get(wallet, slot_value), slot_value)

    def _flow(
        self,
        wallet: str,
        asset_kind: WalletAssetKind,
        asset_id: str,
        *,
        inflow: int,
        outflow: int,
    ) -> None:
        flow = self.flows.setdefault(wallet, {}).setdefault(
            (asset_kind, asset_id),
            [0, 0],
        )
        flow[0] += inflow
        flow[1] += outflow

    def _inventory_delta(
        self,
        *,
        wallet: str,
        token_mint: str,
        inflow: int,
        outflow: int,
    ) -> bool:
        position = self.inventory.setdefault((wallet, token_mint), [0, 0, 0])
        position[0] += inflow
        position[1] += outflow
        position[2] += inflow - outflow
        if position[2] < 0:
            position[2] += outflow - inflow
            position[0] -= inflow
            position[1] -= outflow
            return False
        return True

    @staticmethod
    def _increment(counts: dict[str, int], wallet: str) -> None:
        counts[wallet] = counts.get(wallet, 0) + 1

    def snapshot(
        self,
        *,
        as_of_slot: Slot,
        source_evidence_count: int,
        deduplicated_evidence_count: int,
    ) -> WalletBehaviorLedger:
        co_buy_pairs = _pair_counts(self.buys_by_token)
        co_sell_pairs = _pair_counts(self.sells_by_token)
        summaries = tuple(
            self._summary(
                wallet=wallet,
                as_of_slot=as_of_slot,
                co_buy_pairs=co_buy_pairs,
                co_sell_pairs=co_sell_pairs,
            )
            for wallet in sorted(self.wallets)
        )
        return WalletBehaviorLedger(
            as_of_slot=as_of_slot,
            wallets=summaries,
            transfer_count=sum(self.transfer_counts.values()) // 2,
            buy_count=sum(self.buy_counts.values()),
            sell_count=sum(self.sell_counts.values()),
            source_evidence_count=source_evidence_count,
            deduplicated_evidence_count=deduplicated_evidence_count,
        )

    def _summary(
        self,
        *,
        wallet: str,
        as_of_slot: Slot,
        co_buy_pairs: dict[tuple[str, str], int],
        co_sell_pairs: dict[tuple[str, str], int],
    ) -> WalletBehaviorSummary:
        flows = tuple(
            WalletAssetFlow(
                as_of_slot=as_of_slot,
                wallet=wallet,
                asset_kind=asset_kind,
                asset_id=asset_id,
                inflow_base_units=values[0],
                outflow_base_units=values[1],
            )
            for (asset_kind, asset_id), values in sorted(
                self.flows.get(wallet, {}).items(),
                key=lambda item: (item[0][0].value, item[0][1]),
            )
        )
        inventory = tuple(
            WalletInventoryPosition(
                as_of_slot=as_of_slot,
                wallet=wallet,
                token_mint=token_mint,
                inflow_base_units=values[0],
                outflow_base_units=values[1],
                balance_base_units=values[2],
            )
            for (inventory_wallet, token_mint), values in sorted(
                self.inventory.items(), key=lambda item: (item[0][0], item[0][1])
            )
            if inventory_wallet == wallet
        )
        sell_destinations = tuple(
            WalletSellDestination(
                as_of_slot=as_of_slot,
                wallet=wallet,
                destination_wallet=destination_wallet,
                sell_count=values[0],
                token_amount_base_units=values[1],
                quote_amount_base_units=values[2],
            )
            for (seller, destination_wallet), values in sorted(
                self.sell_destinations.items(), key=lambda item: item[0]
            )
            if seller == wallet
        )
        funding_relationships = tuple(
            WalletFundingRelationship(
                as_of_slot=as_of_slot,
                source_wallet=source_wallet,
                destination_wallet=destination_wallet,
                transfer_count=values[0],
                amount_base_units=values[1],
            )
            for (source_wallet, destination_wallet), values in sorted(
                self.funding.items()
            )
            if wallet in {source_wallet, destination_wallet}
        )
        return WalletBehaviorSummary(
            as_of_slot=as_of_slot,
            wallet=wallet,
            first_seen_slot=Slot(self.first_seen[wallet]),
            last_seen_slot=Slot(self.last_seen[wallet]),
            transfer_count=self.transfer_counts.get(wallet, 0),
            buy_count=self.buy_counts.get(wallet, 0),
            sell_count=self.sell_counts.get(wallet, 0),
            asset_flows=flows,
            inventory=inventory,
            sell_destinations=sell_destinations,
            funding_relationships=funding_relationships,
            co_buy_counts=_counterparty_summaries(
                wallet=wallet,
                counts=co_buy_pairs,
                as_of_slot=as_of_slot,
            ),
            co_sell_counts=_counterparty_summaries(
                wallet=wallet,
                counts=co_sell_pairs,
                as_of_slot=as_of_slot,
            ),
        )


def _pair_counts(wallets_by_token: dict[str, set[str]]) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    for wallets in wallets_by_token.values():
        for pair in combinations(sorted(wallets), 2):
            counts[pair] = counts.get(pair, 0) + 1
    return counts


def _counterparty_summaries(
    *,
    wallet: str,
    counts: dict[tuple[str, str], int],
    as_of_slot: Slot,
) -> tuple[WalletCounterpartyCount, ...]:
    rows: list[WalletCounterpartyCount] = []
    for (first, second), count in sorted(counts.items()):
        if wallet == first:
            counterparty = second
        elif wallet == second:
            counterparty = first
        else:
            continue
        rows.append(
            WalletCounterpartyCount(
                as_of_slot=as_of_slot,
                wallet=wallet,
                counterparty_wallet=counterparty,
                count=count,
            )
        )
    return tuple(rows)


def _validate_request(
    *,
    transfers: tuple[CanonicalTransferEvidence, ...],
    buys: tuple[CanonicalBuyEvidence, ...],
    sells: tuple[CanonicalSellEvidence, ...],
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _non_negative_int(as_of_slot):
        return _unsupported("as_of_slot must be a non-negative integer", as_of_slot)
    if not all(isinstance(items, tuple) for items in (transfers, buys, sells)):
        return _missing("wallet behavior evidence must be typed tuples", as_of_slot)
    return None


def _validate_evidence(  # noqa: PLR0911
    evidence: object,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not isinstance(
        evidence,
        (CanonicalTransferEvidence, CanonicalBuyEvidence, CanonicalSellEvidence),
    ):
        return _missing("wallet behavior evidence type is incomplete", as_of_slot)
    if not _non_negative_int(evidence.as_of_slot) or not _non_negative_int(
        evidence.slot
    ):
        return _unsupported("evidence slots must be non-negative integers", as_of_slot)
    if evidence.slot > evidence.as_of_slot or evidence.as_of_slot > as_of_slot:
        return _stale("evidence is not bounded by as_of_slot", as_of_slot)
    if not _non_negative_int(evidence.transaction_index) or not _non_negative_int(
        evidence.event_index
    ):
        return _missing("canonical event ordering evidence is required", as_of_slot)
    if not isinstance(evidence.signature, bytes) or not evidence.signature:
        return _missing("canonical event signature is required", as_of_slot)
    if not _valid_evidence_ids(evidence.evidence_ids):
        return _missing("canonical evidence_ids are required", as_of_slot)
    if isinstance(evidence, CanonicalTransferEvidence):
        return _validate_transfer(evidence, as_of_slot)
    if isinstance(evidence, CanonicalBuyEvidence):
        return _validate_buy(evidence, as_of_slot)
    return _validate_sell(evidence, as_of_slot)


def _validate_transfer(
    evidence: CanonicalTransferEvidence,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _wallet_pair(evidence.source_wallet, evidence.destination_wallet):
        return _missing("transfer wallets are required and must differ", as_of_slot)
    if not isinstance(evidence.asset_kind, WalletAssetKind):
        return _unsupported("transfer asset kind is invalid", as_of_slot)
    if not _non_empty_str(evidence.asset_id):
        return _missing("transfer asset_id is required", as_of_slot)
    if not _positive_int(evidence.amount_base_units):
        return _missing(
            "transfer amount must be positive integer base units", as_of_slot
        )
    return None


def _validate_buy(
    evidence: CanonicalBuyEvidence,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _non_empty_str(evidence.wallet):
        return _missing("buy wallet is required", as_of_slot)
    if not _non_empty_str(evidence.token_mint):
        return _missing("buy token_mint is required", as_of_slot)
    if not _positive_int(evidence.base_amount_base_units) or not _positive_int(
        evidence.quote_amount_base_units
    ):
        return _missing("buy amounts must be positive integer base units", as_of_slot)
    return _validate_quote(
        quote_asset_kind=evidence.quote_asset_kind,
        quote_asset_id=evidence.quote_asset_id,
        as_of_slot=as_of_slot,
    )


def _validate_sell(
    evidence: CanonicalSellEvidence,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if not _non_empty_str(evidence.wallet):
        return _missing("sell wallet is required", as_of_slot)
    if not _non_empty_str(evidence.token_mint):
        return _missing("sell token_mint is required", as_of_slot)
    if not _positive_int(evidence.base_amount_base_units) or not _positive_int(
        evidence.quote_amount_base_units
    ):
        return _missing("sell amounts must be positive integer base units", as_of_slot)
    if not _non_empty_str(evidence.destination_wallet):
        return _missing("sell destination_wallet is required", as_of_slot)
    return _validate_quote(
        quote_asset_kind=evidence.quote_asset_kind,
        quote_asset_id=evidence.quote_asset_id,
        as_of_slot=as_of_slot,
    )


def _validate_quote(
    *,
    quote_asset_kind: WalletAssetKind,
    quote_asset_id: str,
    as_of_slot: Slot,
) -> AbstainResult | None:
    if quote_asset_kind not in (WalletAssetKind.NATIVE, WalletAssetKind.QUOTE):
        return _unsupported("quote asset kind must be native or quote", as_of_slot)
    if not _non_empty_str(quote_asset_id):
        return _missing("quote asset_id is required", as_of_slot)
    return None


def _event_key(evidence: _Evidence) -> _EventKey:
    return (
        int(evidence.slot),
        evidence.transaction_index,
        evidence.event_index,
        evidence.signature,
    )


def _event_sort_key(evidence: _Evidence) -> tuple[int, int, int, bytes, str]:
    return (*_event_key(evidence), type(evidence).__name__)


def _valid_evidence_ids(evidence_ids: object) -> bool:
    return (
        isinstance(evidence_ids, tuple)
        and bool(evidence_ids)
        and len(set(evidence_ids)) == len(evidence_ids)
        and all(_non_empty_str(evidence_id) for evidence_id in evidence_ids)
    )


def _wallet_pair(source_wallet: object, destination_wallet: object) -> bool:
    return (
        _non_empty_str(source_wallet)
        and _non_empty_str(destination_wallet)
        and source_wallet != destination_wallet
    )


def _non_empty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


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


def _conflict(message: str, as_of_slot: Slot) -> AbstainResult:
    return AbstainResult(
        reason=AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
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
    if type(as_of_slot) is int:
        return as_of_slot
    return -1


__all__ = [
    "CanonicalBuyEvidence",
    "CanonicalSellEvidence",
    "CanonicalTransferEvidence",
    "WalletAssetFlow",
    "WalletAssetKind",
    "WalletBehaviorLedger",
    "WalletBehaviorLedgerResult",
    "WalletBehaviorSummary",
    "WalletCounterpartyCount",
    "WalletFundingRelationship",
    "WalletInventoryPosition",
    "WalletSellDestination",
    "build_wallet_behavior_ledger",
]
