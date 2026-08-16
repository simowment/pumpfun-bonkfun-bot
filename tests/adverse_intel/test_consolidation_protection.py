"""Regression guards for the point-in-time consolidation protection signal."""

import unittest

from rugbot.decision.consolidation_protection import (
    ConsolidationProtectionConfig,
    ConsolidationSignal,
    WalletTokenInventory,
    detect_consolidation_signal,
)
from rugbot.domain.amounts import Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.wallet_behavior import CanonicalTransferEvidence, WalletAssetKind


class ConsolidationProtectionTests(unittest.TestCase):
    """Check threshold detection and incomplete-history abstention."""

    def test_detects_consolidation_threshold(self) -> None:
        result = detect_consolidation_signal(
            transfers=(_transfer("wallet-a", "wallet-b", 60),),
            initial_inventories=(
                _inventory("wallet-a", 100),
                _inventory("wallet-b", 0),
            ),
            config=_config(history_complete=True),
        )

        self.assertIsInstance(result, ConsolidationSignal)
        if isinstance(result, ConsolidationSignal):
            self.assertEqual(result.destination_wallet, "wallet-b")
            self.assertEqual(result.consolidated_share_ppm, 600_000)

    def test_incomplete_history_abstains(self) -> None:
        result = detect_consolidation_signal(
            transfers=(_transfer("wallet-a", "wallet-b", 60),),
            initial_inventories=(
                _inventory("wallet-a", 100),
                _inventory("wallet-b", 0),
            ),
            config=_config(history_complete=False),
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.UNKNOWN_PROTOCOL_STATE)

    def test_malformed_inventory_abstains_without_attribute_error(self) -> None:
        result = detect_consolidation_signal(
            transfers=(),
            initial_inventories=("not-an-inventory",),  # type: ignore[arg-type]
            config=_config(history_complete=True),
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.MISSING_FEATURE)

    def test_inventory_at_transfer_boundary_is_stale(self) -> None:
        result = detect_consolidation_signal(
            transfers=(_transfer("wallet-a", "wallet-b", 60),),
            initial_inventories=(
                _inventory("wallet-a", 100, as_of_slot=10),
                _inventory("wallet-b", 0, as_of_slot=10),
            ),
            config=_config(history_complete=True),
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.STALE_STATE)

    def test_duplicate_transfer_identity_abstains(self) -> None:
        transfer = _transfer("wallet-a", "wallet-b", 60)
        result = detect_consolidation_signal(
            transfers=(transfer, transfer),
            initial_inventories=(
                _inventory("wallet-a", 100),
                _inventory("wallet-b", 0),
            ),
            config=_config(history_complete=True),
        )

        self.assertIsInstance(result, AbstainResult)
        if isinstance(result, AbstainResult):
            self.assertIs(result.reason, AbstainReason.UNSUPPORTED_PROTOCOL_STATE)


def _config(*, history_complete: bool) -> ConsolidationProtectionConfig:
    return ConsolidationProtectionConfig(
        as_of_slot=Slot(10),
        token_mint="mint",  # noqa: S106
        operator_wallets=("wallet-a",),
        operator_supply_base_units=TokenBaseUnits(100),
        threshold_ppm=500_000,
        history_complete=history_complete,
    )


def _inventory(
    wallet: str, balance: int, *, as_of_slot: int = 1
) -> WalletTokenInventory:
    return WalletTokenInventory(
        as_of_slot=Slot(as_of_slot),
        wallet=wallet,
        token_mint="mint",  # noqa: S106
        balance_base_units=TokenBaseUnits(balance),
        evidence_ids=(f"inventory:{wallet}",),
    )


def _transfer(source: str, destination: str, amount: int) -> CanonicalTransferEvidence:
    return CanonicalTransferEvidence(
        as_of_slot=Slot(10),
        slot=Slot(10),
        transaction_index=0,
        event_index=0,
        signature=b"signature",
        evidence_ids=("transfer:1",),
        source_wallet=source,
        destination_wallet=destination,
        asset_kind=WalletAssetKind.TOKEN,
        asset_id="mint",
        amount_base_units=amount,
    )


if __name__ == "__main__":
    unittest.main()
