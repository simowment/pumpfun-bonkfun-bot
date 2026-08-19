"""Finalized RPC fixture tests for exact landing reconciliation."""

from __future__ import annotations

import unittest

from solders.pubkey import Pubkey

from rugbot.execution.landing_reconciliation import (
    LandingReconciliationError,
    reconcile_finalized_landing,
    reconcile_finalized_transaction,
)
from rugbot.protocol.pump.create_decoder import WSOL_MINT_ID
from rugbot.protocol.pump.trade_decoder import BUY_V2_ACCOUNT_NAMES
from rugbot.protocol.pump.v2_builder import PUMP_PROGRAM_ID
from rugbot.protocol.solana.transfers import SYSTEM_PROGRAM_ID


class LandingReconciliationTests(unittest.IsolatedAsyncioTestCase):
    """Validate wallet deltas, fee attribution, and finalized RPC options."""

    def setUp(self) -> None:
        self.wallet = str(Pubkey.new_unique())
        self.mint = str(Pubkey.new_unique())
        self.base_ata = str(Pubkey.new_unique())
        self.quote_ata = str(Pubkey.new_unique())
        self.fee_accounts = tuple(str(Pubkey.new_unique()) for _ in range(3))
        self.jito_account = str(Pubkey.new_unique())
        self.signature = "5finalizedSignature"

    def test_finalized_fixture_reconciles_exact_wallet_and_fee_deltas(self) -> None:
        reconciled = reconcile_finalized_transaction(
            self._result(),
            signature=self.signature,
            wallet_pubkey=self.wallet,
            mint=self.mint,
            side="buy",
            jito_tip_accounts=(self.jito_account,),
            expected_jito_tip_lamports=1_000_000,
        )

        self.assertEqual(reconciled.landed_slot, 123)
        self.assertEqual(reconciled.token_delta_base_units, 777)
        self.assertEqual(reconciled.sol_delta_lamports, -3_044_280)
        self.assertEqual(reconciled.network_fee_lamports, 5_000)
        self.assertEqual(reconciled.jito_tip_lamports, 1_000_000)
        self.assertEqual(reconciled.ata_rent_lamports, 2_039_280)
        self.assertEqual(reconciled.protocol_fee_lamports, 350_000)

    def test_jito_tip_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(LandingReconciliationError, "Jito tip"):
            reconcile_finalized_transaction(
                self._result(),
                signature=self.signature,
                wallet_pubkey=self.wallet,
                mint=self.mint,
                side="buy",
                jito_tip_accounts=(self.jito_account,),
                expected_jito_tip_lamports=2_000_000,
            )

    async def test_rpc_fetch_requires_finalized_json_parsed_evidence(self) -> None:
        client = _FakeReconciliationClient(self._result())

        reconciled = await reconcile_finalized_landing(
            client,
            signature=self.signature,
            wallet_pubkey=self.wallet,
            mint=self.mint,
            side="buy",
            jito_tip_accounts=(self.jito_account,),
            expected_jito_tip_lamports=1_000_000,
        )

        self.assertEqual(reconciled.token_delta_base_units, 777)
        request = client.requests[0]
        self.assertEqual(request["method"], "getTransaction")
        self.assertEqual(request["params"][1]["commitment"], "finalized")
        self.assertEqual(request["params"][1]["encoding"], "jsonParsed")

    def _result(self) -> dict[str, object]:
        account_keys = [
            self.wallet,
            self.base_ata,
            self.quote_ata,
            *self.fee_accounts,
            self.jito_account,
            SYSTEM_PROGRAM_ID,
        ]
        fee_owners = [str(Pubkey.new_unique()) for _ in self.fee_accounts]
        pre_token_balances = [
            _token_balance(2, WSOL_MINT_ID, self.wallet, 100_000_000),
            *(
                _token_balance(index, WSOL_MINT_ID, owner, 1_000)
                for index, owner in zip(range(3, 6), fee_owners, strict=True)
            ),
        ]
        post_token_balances = [
            _token_balance(1, self.mint, self.wallet, 777),
            _token_balance(2, WSOL_MINT_ID, self.wallet, 75_000_000),
            *(
                _token_balance(index, WSOL_MINT_ID, owner, amount)
                for index, owner, amount in zip(
                    range(3, 6),
                    fee_owners,
                    (201_000, 51_000, 101_000),
                    strict=True,
                )
            ),
        ]
        pump_accounts = [self.wallet for _name in BUY_V2_ACCOUNT_NAMES]
        for role, account in zip(
            (
                "associated_quote_fee_recipient",
                "associated_quote_buyback_fee_recipient",
                "associated_creator_vault",
            ),
            self.fee_accounts,
            strict=True,
        ):
            pump_accounts[BUY_V2_ACCOUNT_NAMES.index(role)] = account
        return {
            "slot": 123,
            "blockTime": 1_700_000_000,
            "transaction": {
                "signatures": [self.signature],
                "message": {
                    "accountKeys": account_keys,
                    "instructions": [
                        {
                            "programId": PUMP_PROGRAM_ID,
                            "accounts": pump_accounts,
                            "data": "trade-data",
                        },
                        {
                            "program": "system",
                            "programId": SYSTEM_PROGRAM_ID,
                            "parsed": {
                                "type": "transfer",
                                "info": {
                                    "source": self.wallet,
                                    "destination": self.jito_account,
                                    "lamports": 1_000_000,
                                },
                            },
                        },
                    ],
                },
            },
            "meta": {
                "err": None,
                "fee": 5_000,
                "preBalances": [
                    1_000_000_000,
                    0,
                    2_039_280,
                    2_039_280,
                    2_039_280,
                    2_039_280,
                    0,
                    1,
                ],
                "postBalances": [
                    996_955_720,
                    2_039_280,
                    2_039_280,
                    2_039_280,
                    2_039_280,
                    2_039_280,
                    1_000_000,
                    1,
                ],
                "preTokenBalances": pre_token_balances,
                "postTokenBalances": post_token_balances,
                "innerInstructions": [],
            },
        }


def _token_balance(
    account_index: int,
    mint: str,
    owner: str,
    amount: int,
) -> dict[str, object]:
    return {
        "accountIndex": account_index,
        "mint": mint,
        "owner": owner,
        "uiTokenAmount": {"amount": str(amount), "decimals": 6},
    }


class _FakeReconciliationClient:
    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.requests: list[dict[str, object]] = []

    async def post_rpc(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        return {"jsonrpc": "2.0", "id": 1, "result": self.result}


if __name__ == "__main__":
    unittest.main()
