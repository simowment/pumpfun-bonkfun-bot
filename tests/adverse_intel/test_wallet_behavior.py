"""Focused tests for the canonical point-in-time wallet behavior ledger."""

import ast
import unittest
from dataclasses import replace
from pathlib import Path
from typing import cast

from rugbot.domain.amounts import Slot
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.graph.wallet_behavior import (
    CanonicalBuyEvidence,
    CanonicalSellEvidence,
    CanonicalTransferEvidence,
    WalletAssetKind,
    WalletBehaviorLedger,
    build_wallet_behavior_ledger,
)

MODULE_PATH = Path("src/rugbot/graph/wallet_behavior.py")
FORBIDDEN_IMPORT_PREFIXES = (
    "requests",
    "aiohttp",
    "httpx",
    "sqlite",
    "psycopg",
    "solana",
    "solders",
    "src.trading",
    "src.platforms",
)


class WalletBehaviorLedgerTests(unittest.TestCase):
    """Tests for strict typed wallet behavior reduction."""

    def test_reduces_flows_inventory_and_deterministic_wallet_order(self) -> None:
        """Transfers, trades, and inventory are summarized at one boundary."""

        result = build_wallet_behavior_ledger(
            transfers=(
                _transfer(
                    event_id=1,
                    slot=1,
                    source="funding-wallet",
                    destination="wallet-b",
                    kind=WalletAssetKind.NATIVE,
                    asset_id="SOL",
                    amount=500,
                ),
                _transfer(
                    event_id=2,
                    slot=4,
                    source="wallet-b",
                    destination="wallet-a",
                    kind=WalletAssetKind.TOKEN,
                    asset_id="token-z",
                    amount=40,
                ),
            ),
            buys=(
                _buy(event_id=3, slot=2, wallet="wallet-a", mint="token-z"),
                _buy(event_id=4, slot=3, wallet="wallet-b", mint="token-z"),
            ),
            sells=(
                _sell(
                    event_id=5,
                    slot=5,
                    wallet="wallet-a",
                    mint="token-z",
                    amount=40,
                    destination="profit-wallet",
                ),
            ),
            as_of_slot=Slot(5),
        )

        ledger = self.assert_ledger(result)
        self.assertEqual(ledger.as_of_slot, 5)
        self.assertEqual(
            [summary.wallet for summary in ledger.wallets],
            [
                "funding-wallet",
                "profit-wallet",
                "wallet-a",
                "wallet-b",
            ],
        )
        wallet_a = ledger.wallets[2]
        token_position = wallet_a.inventory[0]
        self.assertEqual(token_position.inflow_base_units, 140)
        self.assertEqual(token_position.outflow_base_units, 40)
        self.assertEqual(token_position.balance_base_units, 100)
        self.assertEqual(wallet_a.first_seen_slot, 2)
        self.assertEqual(wallet_a.last_seen_slot, 5)
        self.assertEqual(wallet_a.co_buy_counts[0].counterparty_wallet, "wallet-b")
        self.assertEqual(wallet_a.co_buy_counts[0].count, 1)
        self.assertEqual(ledger.transfer_count, 2)
        self.assertEqual(ledger.buy_count, 2)
        self.assertEqual(ledger.sell_count, 1)

    def test_wallet_churn_keeps_replacement_wallets_distinct(self) -> None:
        """A token handoff creates observable churn, not an inferred identity."""

        result = build_wallet_behavior_ledger(
            transfers=(
                _transfer(
                    event_id=10,
                    slot=3,
                    source="old-wallet",
                    destination="fresh-wallet",
                    kind=WalletAssetKind.TOKEN,
                    asset_id="token-a",
                    amount=70,
                ),
                _transfer(
                    event_id=11,
                    slot=4,
                    source="old-wallet",
                    destination="fresh-wallet",
                    kind=WalletAssetKind.NATIVE,
                    asset_id="SOL",
                    amount=25,
                ),
            ),
            buys=(_buy(event_id=12, slot=1, wallet="old-wallet", mint="token-a"),),
            sells=(
                _sell(
                    event_id=13,
                    slot=5,
                    wallet="fresh-wallet",
                    mint="token-a",
                    amount=20,
                    destination="collector",
                ),
            ),
            as_of_slot=Slot(5),
        )

        ledger = self.assert_ledger(result)
        self.assertEqual(
            [summary.wallet for summary in ledger.wallets],
            [
                "collector",
                "fresh-wallet",
                "old-wallet",
            ],
        )
        old_wallet = ledger.wallets[2]
        fresh_wallet = ledger.wallets[1]
        self.assertEqual(old_wallet.inventory[0].balance_base_units, 30)
        self.assertEqual(fresh_wallet.inventory[0].balance_base_units, 50)
        self.assertEqual(fresh_wallet.first_seen_slot, 3)
        self.assertEqual(fresh_wallet.last_seen_slot, 5)
        self.assertEqual(len(fresh_wallet.funding_relationships), 1)
        self.assertEqual(
            fresh_wallet.funding_relationships[0].source_wallet,
            "old-wallet",
        )

    def test_sell_destination_tracking_is_per_destination(self) -> None:
        """Repeated sells preserve destination-specific proceeds and counts."""

        result = build_wallet_behavior_ledger(
            transfers=(),
            buys=(
                _buy(event_id=20, slot=1, wallet="seller", mint="token-a", amount=100),
            ),
            sells=(
                _sell(
                    event_id=21,
                    slot=2,
                    wallet="seller",
                    mint="token-a",
                    amount=20,
                    quote=7,
                    destination="collector-a",
                ),
                _sell(
                    event_id=22,
                    slot=3,
                    wallet="seller",
                    mint="token-a",
                    amount=30,
                    quote=11,
                    destination="collector-a",
                ),
                _sell(
                    event_id=23,
                    slot=4,
                    wallet="seller",
                    mint="token-a",
                    amount=10,
                    quote=5,
                    destination="collector-b",
                ),
            ),
            as_of_slot=Slot(4),
        )

        seller = self.assert_ledger(result).wallets[2]
        self.assertEqual(
            [
                (item.destination_wallet, item.sell_count)
                for item in seller.sell_destinations
            ],
            [("collector-a", 2), ("collector-b", 1)],
        )
        self.assertEqual(seller.sell_destinations[0].quote_amount_base_units, 18)
        collector_a = self.assert_ledger(result).wallets[0]
        quote_flow = collector_a.asset_flows[0]
        self.assertEqual(quote_flow.inflow_base_units, 18)
        self.assertEqual(quote_flow.outflow_base_units, 0)

    def test_co_buy_and_co_sell_counts_are_repeated_per_token(self) -> None:
        """Pair counts measure repeated token-level coordination deterministically."""

        buys = (
            _buy(event_id=30, slot=1, wallet="wallet-a", mint="token-a"),
            _buy(event_id=31, slot=2, wallet="wallet-b", mint="token-a"),
            _buy(event_id=32, slot=3, wallet="wallet-a", mint="token-b"),
            _buy(event_id=33, slot=4, wallet="wallet-b", mint="token-b"),
            _buy(event_id=34, slot=5, wallet="wallet-a", mint="token-c"),
            _buy(event_id=35, slot=6, wallet="wallet-c", mint="token-c"),
        )
        sells = (
            _sell(event_id=36, slot=7, wallet="wallet-a", mint="token-a"),
            _sell(event_id=37, slot=8, wallet="wallet-b", mint="token-a"),
            _sell(event_id=38, slot=9, wallet="wallet-a", mint="token-b"),
            _sell(event_id=39, slot=10, wallet="wallet-b", mint="token-b"),
        )
        result = build_wallet_behavior_ledger(
            transfers=(), buys=buys, sells=sells, as_of_slot=Slot(10)
        )

        ledger = self.assert_ledger(result)
        wallet_a = next(item for item in ledger.wallets if item.wallet == "wallet-a")
        self.assertEqual(
            [(item.counterparty_wallet, item.count) for item in wallet_a.co_buy_counts],
            [("wallet-b", 2), ("wallet-c", 1)],
        )
        self.assertEqual(wallet_a.co_sell_counts[0].counterparty_wallet, "wallet-b")
        self.assertEqual(wallet_a.co_sell_counts[0].count, 2)

    def test_future_evidence_abstains_instead_of_filtering(self) -> None:
        """Future evidence cannot be silently removed from a point-in-time view."""

        result = build_wallet_behavior_ledger(
            transfers=(),
            buys=(_buy(event_id=40, slot=11, wallet="wallet-a", mint="token-a"),),
            sells=(),
            as_of_slot=Slot(10),
        )

        self.assert_abstains(result, AbstainReason.STALE_STATE, as_of_slot=10)

    def test_conflicting_event_identity_abstains(self) -> None:
        """One canonical chain identity cannot describe two different buys."""

        first = _buy(event_id=50, slot=1, wallet="wallet-a", mint="token-a")
        conflicting = replace(first, base_amount_base_units=2)
        result = build_wallet_behavior_ledger(
            transfers=(),
            buys=(first, conflicting),
            sells=(),
            as_of_slot=Slot(1),
        )

        self.assert_abstains(
            result,
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            as_of_slot=1,
        )

    def test_incomplete_inventory_and_destination_abstain(self) -> None:
        """Unknown token inventory or missing sell routing fails closed."""

        inventory_result = build_wallet_behavior_ledger(
            transfers=(),
            buys=(),
            sells=(
                _sell(
                    event_id=60,
                    slot=1,
                    wallet="wallet-a",
                    mint="token-a",
                ),
            ),
            as_of_slot=Slot(1),
        )
        self.assert_abstains(
            inventory_result,
            AbstainReason.MISSING_FEATURE,
            as_of_slot=1,
        )

        missing_destination = replace(
            _sell(event_id=61, slot=1, wallet="wallet-a", mint="token-a"),
            destination_wallet="",
        )
        missing_destination_result = build_wallet_behavior_ledger(
            transfers=(),
            buys=(_buy(event_id=62, slot=0, wallet="wallet-a", mint="token-a"),),
            sells=(missing_destination,),
            as_of_slot=Slot(1),
        )
        self.assert_abstains(
            missing_destination_result,
            AbstainReason.MISSING_FEATURE,
            as_of_slot=1,
        )

    def test_exact_duplicate_evidence_is_idempotent(self) -> None:
        """Repeated delivery of identical canonical evidence does not double count."""

        buy = _buy(event_id=70, slot=1, wallet="wallet-a", mint="token-a")
        result = build_wallet_behavior_ledger(
            transfers=(),
            buys=(buy, buy),
            sells=(),
            as_of_slot=Slot(1),
        )

        ledger = self.assert_ledger(result)
        self.assertEqual(ledger.source_evidence_count, 2)
        self.assertEqual(ledger.deduplicated_evidence_count, 1)
        self.assertEqual(ledger.buy_count, 1)

    def test_module_is_pure_and_integer_only(self) -> None:
        """The reducer has no transport, signer, database, or float dependency."""

        source = MODULE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(MODULE_PATH))
        imported_names = _imported_module_names(tree)
        self.assertEqual(
            [
                name
                for name in imported_names
                if name.startswith(FORBIDDEN_IMPORT_PREFIXES)
            ],
            [],
        )
        for token in (
            "float(",
            "PRIVATE_KEY",
            "send_transaction",
            "send_raw_transaction",
        ):
            with self.subTest(mint=token):
                self.assertNotIn(token, source)

    def assert_ledger(self, result: object) -> WalletBehaviorLedger:
        self.assertIsInstance(result, WalletBehaviorLedger)
        return cast("WalletBehaviorLedger", result)

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


def _buy(  # noqa: PLR0913
    *,
    event_id: int,
    slot: int,
    wallet: str,
    mint: str,
    amount: int = 100,
    quote: int = 10,
) -> CanonicalBuyEvidence:
    return CanonicalBuyEvidence(
        as_of_slot=Slot(slot),
        slot=Slot(slot),
        transaction_index=event_id,
        event_index=0,
        signature=f"signature-{event_id}".encode(),
        evidence_ids=(f"evidence-{event_id}",),
        wallet=wallet,
        token_mint=mint,
        base_amount_base_units=amount,
        quote_asset_kind=WalletAssetKind.NATIVE,
        quote_asset_id="SOL",
        quote_amount_base_units=quote,
    )


def _sell(  # noqa: PLR0913
    *,
    event_id: int,
    slot: int,
    wallet: str,
    mint: str,
    amount: int = 1,
    quote: int = 1,
    destination: str = "collector",
) -> CanonicalSellEvidence:
    return CanonicalSellEvidence(
        as_of_slot=Slot(slot),
        slot=Slot(slot),
        transaction_index=event_id,
        event_index=0,
        signature=f"signature-{event_id}".encode(),
        evidence_ids=(f"evidence-{event_id}",),
        wallet=wallet,
        token_mint=mint,
        base_amount_base_units=amount,
        quote_asset_kind=WalletAssetKind.NATIVE,
        quote_asset_id="SOL",
        quote_amount_base_units=quote,
        destination_wallet=destination,
    )


def _transfer(  # noqa: PLR0913
    *,
    event_id: int,
    slot: int,
    source: str,
    destination: str,
    kind: WalletAssetKind,
    asset_id: str,
    amount: int,
) -> CanonicalTransferEvidence:
    return CanonicalTransferEvidence(
        as_of_slot=Slot(slot),
        slot=Slot(slot),
        transaction_index=event_id,
        event_index=0,
        signature=f"signature-{event_id}".encode(),
        evidence_ids=(f"evidence-{event_id}",),
        source_wallet=source,
        destination_wallet=destination,
        asset_kind=kind,
        asset_id=asset_id,
        amount_base_units=amount,
    )


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.append(node.module)
    return names


if __name__ == "__main__":
    unittest.main()
