"""Fail-closed front-run-sell signal from point-in-time SPL transfers."""

# The boundary deliberately keeps malformed-evidence branches explicit.
# ruff: noqa: C901, PLR0911, PLR0912

from __future__ import annotations

from dataclasses import dataclass

from rugbot.domain.amounts import (
    PROBABILITY_PPM_DENOMINATOR,
    Slot,
    TokenBaseUnits,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.intelligence.wallet_behavior import (
    CanonicalTransferEvidence,
    WalletAssetKind,
)


@dataclass(frozen=True, slots=True)
class WalletTokenInventory:
    """Finalized inventory snapshot at the start of a transfer window."""

    as_of_slot: Slot
    wallet: str
    token_mint: str
    balance_base_units: TokenBaseUnits
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConsolidationProtectionConfig:
    """Thresholds and identity boundary for one operator token."""

    as_of_slot: Slot
    token_mint: str
    operator_wallets: tuple[str, ...]
    operator_supply_base_units: TokenBaseUnits
    threshold_ppm: int
    history_complete: bool


@dataclass(frozen=True, slots=True)
class ConsolidationSignal:
    """First finalized transfer after which one wallet holds the threshold."""

    as_of_slot: Slot
    slot: Slot
    transaction_index: int
    signature: bytes
    token_mint: str
    destination_wallet: str
    consolidated_base_units: TokenBaseUnits
    consolidated_share_ppm: int
    evidence_ids: tuple[str, ...]


ConsolidationResult = ConsolidationSignal | None | AbstainResult


def validate_consolidation_signal(
    signal: object,
    *,
    market_id: str,
    as_of_slot: int,
) -> AbstainResult | None:
    """Validate one consolidation event before it can trigger a paper exit."""

    if not isinstance(signal, ConsolidationSignal):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "consolidation signal is malformed",
            as_of_slot,
        )
    if (
        not isinstance(market_id, str)
        or not market_id
        or type(as_of_slot) is not int
        or as_of_slot < 0
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "consolidation exit identity is malformed",
            as_of_slot if type(as_of_slot) is int else -1,
        )
    if signal.token_mint != market_id:
        return _abstain(
            AbstainReason.DECODER_MISMATCH,
            "consolidation signal does not match the market",
            as_of_slot,
        )
    if (
        type(signal.as_of_slot) is not int
        or type(signal.slot) is not int
        or signal.as_of_slot < 0
        or signal.slot < 0
        or signal.slot > signal.as_of_slot
        or signal.as_of_slot > as_of_slot
        or not isinstance(signal.signature, bytes)
        or not signal.signature
        or not _valid_evidence_ids(signal.evidence_ids)
        or type(signal.consolidated_base_units) is not int
        or signal.consolidated_base_units <= 0
        or type(signal.consolidated_share_ppm) is not int
        or not 0 < signal.consolidated_share_ppm <= PROBABILITY_PPM_DENOMINATOR
    ):
        return _abstain(
            AbstainReason.STALE_STATE,
            "consolidation signal timing or provenance is invalid",
            as_of_slot,
        )
    return None


def detect_consolidation_signal(
    *,
    transfers: tuple[CanonicalTransferEvidence, ...],
    initial_inventories: tuple[WalletTokenInventory, ...],
    config: ConsolidationProtectionConfig,
) -> ConsolidationResult:
    """Find a supply-consolidation trigger without inferring ownership.

    All wallets touched by the supplied transfer window must have an explicit
    starting inventory.  Token provenance is treated conservatively: a
    transfer from a wallet with insufficient attributed inventory abstains
    rather than assuming the missing tokens belong to the operator.
    """

    validation = _validate_inputs(transfers, initial_inventories, config)
    if validation is not None:
        return validation

    inventory = {
        item.wallet: int(item.balance_base_units) for item in initial_inventories
    }
    inventory_evidence_ids = tuple(
        evidence_id for item in initial_inventories for evidence_id in item.evidence_ids
    )
    operator_wallets = set(config.operator_wallets)
    attributed = {
        item.wallet: int(item.balance_base_units)
        if item.wallet in operator_wallets
        else 0
        for item in initial_inventories
    }
    ordered = tuple(
        sorted(
            transfers,
            key=lambda item: (int(item.slot), item.transaction_index, item.event_index),
        )
    )
    seen_wallets = set(inventory)
    for transfer in ordered:
        if (
            transfer.source_wallet not in seen_wallets
            or transfer.destination_wallet not in seen_wallets
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "initial inventory is missing for a transfer wallet",
                config.as_of_slot,
            )
        amount = int(transfer.amount_base_units)
        if amount > inventory[transfer.source_wallet]:
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "token transfer exceeds proven source inventory",
                config.as_of_slot,
            )
        inventory[transfer.source_wallet] -= amount
        inventory[transfer.destination_wallet] += amount

        source_attributed = attributed[transfer.source_wallet]
        if amount > source_attributed:
            if transfer.source_wallet in operator_wallets:
                return _abstain(
                    AbstainReason.UNKNOWN_PROTOCOL_STATE,
                    "operator token provenance is ambiguous",
                    config.as_of_slot,
                )
            moved_attributed = 0
        else:
            moved_attributed = amount
        attributed[transfer.source_wallet] -= moved_attributed
        attributed[transfer.destination_wallet] += moved_attributed

        for destination in sorted(attributed):
            consolidated = attributed[destination]
            if consolidated * PROBABILITY_PPM_DENOMINATOR < (
                int(config.operator_supply_base_units) * config.threshold_ppm
            ):
                continue
            share = (
                consolidated
                * PROBABILITY_PPM_DENOMINATOR
                // int(config.operator_supply_base_units)
            )
            return ConsolidationSignal(
                as_of_slot=config.as_of_slot,
                slot=transfer.slot,
                transaction_index=transfer.transaction_index,
                signature=transfer.signature,
                token_mint=config.token_mint,
                destination_wallet=destination,
                consolidated_base_units=TokenBaseUnits(consolidated),
                consolidated_share_ppm=share,
                evidence_ids=tuple(
                    dict.fromkeys((*inventory_evidence_ids, *transfer.evidence_ids))
                ),
            )
    return None


def _validate_inputs(
    transfers: tuple[CanonicalTransferEvidence, ...],
    inventories: tuple[WalletTokenInventory, ...],
    config: ConsolidationProtectionConfig,
) -> AbstainResult | None:
    cutoff = (
        config.as_of_slot if isinstance(config, ConsolidationProtectionConfig) else -1
    )
    if not isinstance(config, ConsolidationProtectionConfig):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "consolidation config is malformed",
            -1,
        )
    if type(config.as_of_slot) is not int or config.as_of_slot < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "consolidation cutoff is malformed",
            cutoff,
        )
    if (
        type(config.operator_wallets) is not tuple
        or not isinstance(config.token_mint, str)
        or not config.token_mint
        or not config.operator_wallets
        or any(
            not isinstance(item, str) or not item for item in config.operator_wallets
        )
        or len(set(config.operator_wallets)) != len(config.operator_wallets)
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "consolidation identity is incomplete",
            cutoff,
        )
    if (
        type(config.operator_supply_base_units) is not int
        or config.operator_supply_base_units <= 0
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE, "operator supply proof is required", cutoff
        )
    if (
        type(config.threshold_ppm) is not int
        or not 0 < config.threshold_ppm <= PROBABILITY_PPM_DENOMINATOR
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "consolidation threshold is malformed",
            cutoff,
        )
    if config.history_complete is not True:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "complete transfer history is required",
            cutoff,
        )
    if type(transfers) is not tuple or type(inventories) is not tuple:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "consolidation evidence must be tuples",
            cutoff,
        )
    if not inventories:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "initial token inventories are required",
            cutoff,
        )

    inventory_wallets: set[str] = set()
    inventory_evidence_ids: set[str] = set()
    for item in inventories:
        if (
            not isinstance(item, WalletTokenInventory)
            or type(item.as_of_slot) is not int
            or item.as_of_slot < 0
            or item.as_of_slot > cutoff
            or not isinstance(item.wallet, str)
            or not item.wallet
            or item.wallet in inventory_wallets
            or item.token_mint != config.token_mint
            or type(item.balance_base_units) is not int
            or item.balance_base_units < 0
            or not _valid_evidence_ids(item.evidence_ids)
            or inventory_evidence_ids.intersection(item.evidence_ids)
        ):
            return _abstain(
                AbstainReason.MISSING_FEATURE,
                "initial inventory evidence is incomplete or ambiguous",
                cutoff,
            )
        inventory_wallets.add(item.wallet)
        inventory_evidence_ids.update(item.evidence_ids)

    if not set(config.operator_wallets) <= inventory_wallets:
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "operator inventory proof is incomplete",
            cutoff,
        )

    operator_inventory = sum(
        int(item.balance_base_units)
        for item in inventories
        if item.wallet in config.operator_wallets
    )
    if operator_inventory > int(config.operator_supply_base_units):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "initial operator inventory exceeds proven operator supply",
            cutoff,
        )

    transfer_keys: set[tuple[int, int, int, bytes]] = set()
    transfer_evidence_ids: set[str] = set()
    for transfer in transfers:
        if (
            not isinstance(transfer, CanonicalTransferEvidence)
            or transfer.asset_kind is not WalletAssetKind.TOKEN
            or transfer.asset_id != config.token_mint
            or type(transfer.as_of_slot) is not int
            or transfer.as_of_slot < 0
            or type(transfer.slot) is not int
            or transfer.slot < 0
            or transfer.as_of_slot > cutoff
            or transfer.slot > transfer.as_of_slot
            or type(transfer.transaction_index) is not int
            or transfer.transaction_index < 0
            or type(transfer.event_index) is not int
            or transfer.event_index < 0
            or not isinstance(transfer.source_wallet, str)
            or not transfer.source_wallet
            or not isinstance(transfer.destination_wallet, str)
            or not transfer.destination_wallet
            or transfer.source_wallet == transfer.destination_wallet
            or type(transfer.amount_base_units) is not int
            or transfer.amount_base_units <= 0
            or not isinstance(transfer.signature, bytes)
            or not transfer.signature
            or not _valid_evidence_ids(transfer.evidence_ids)
        ):
            return _abstain(
                AbstainReason.STALE_STATE,
                "consolidation transfer evidence is invalid",
                cutoff,
            )
        key = (
            int(transfer.slot),
            transfer.transaction_index,
            transfer.event_index,
            transfer.signature,
        )
        if key in transfer_keys or transfer_evidence_ids.intersection(
            transfer.evidence_ids
        ):
            return _abstain(
                AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
                "duplicate transfer identity is ambiguous",
                cutoff,
            )
        transfer_keys.add(key)
        transfer_evidence_ids.update(transfer.evidence_ids)

    if transfers:
        first_transfer_slot = min(int(transfer.slot) for transfer in transfers)
        if any(int(item.as_of_slot) >= first_transfer_slot for item in inventories):
            return _abstain(
                AbstainReason.STALE_STATE,
                "initial inventory snapshot is not before transfer history",
                cutoff,
            )
    return None


def _valid_evidence_ids(evidence_ids: object) -> bool:
    return (
        isinstance(evidence_ids, tuple)
        and bool(evidence_ids)
        and all(isinstance(value, str) and bool(value) for value in evidence_ids)
        and len(set(evidence_ids)) == len(evidence_ids)
    )


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


__all__ = [
    "ConsolidationProtectionConfig",
    "ConsolidationResult",
    "ConsolidationSignal",
    "WalletTokenInventory",
    "detect_consolidation_signal",
    "validate_consolidation_signal",
]
