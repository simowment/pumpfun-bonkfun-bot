"""Finalized wallet-delta reconciliation for landed sniper transactions."""

# Reconciliation rejects malformed external evidence with domain-specific messages.
# ruff: noqa: TRY003

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from solders.pubkey import Pubkey

from rugbot.protocol.pump.create_decoder import WSOL_MINT_ID
from rugbot.protocol.pump.trade_decoder import (
    BUY_V2_ACCOUNT_NAMES,
    SELL_V2_ACCOUNT_NAMES,
)
from rugbot.protocol.pump.v2_builder import PUMP_PROGRAM_ID
from rugbot.protocol.solana.transfers import parse_sol_transfers

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.client import SolanaClient


@dataclass(frozen=True, slots=True)
class LandingReconciliation:
    """Exact finalized deltas and attributed execution costs."""

    signature: str
    landed_slot: int
    token_delta_base_units: int
    sol_delta_lamports: int
    network_fee_lamports: int
    jito_tip_lamports: int
    ata_rent_lamports: int
    protocol_fee_lamports: int


class LandingReconciliationError(ValueError):
    """Raised when finalized evidence is missing, malformed, or inconsistent."""

    @classmethod
    def invalid(cls, message: str) -> LandingReconciliationError:
        """Build an error for invalid reconciliation evidence."""

        return cls(message)

    @classmethod
    def malformed(cls, field_name: str) -> LandingReconciliationError:
        """Build an error for a malformed finalized field."""

        return cls(f"finalized transaction {field_name} is malformed")


async def reconcile_finalized_landing(  # noqa: PLR0913
    client: SolanaClient,
    *,
    signature: str,
    wallet_pubkey: str,
    mint: str,
    side: str,
    jito_tip_accounts: Sequence[str],
    expected_jito_tip_lamports: int,
) -> LandingReconciliation:
    """Fetch and reconcile one finalized transaction from RPC evidence."""

    _validate_pubkey(wallet_pubkey, "wallet_pubkey")
    _validate_pubkey(mint, "mint")
    _validate_non_empty_text(signature, "signature")
    _validate_side(side)
    _validate_pubkey_sequence(jito_tip_accounts, "jito_tip_accounts")
    _validate_non_negative_int(
        expected_jito_tip_lamports,
        "expected_jito_tip_lamports",
    )
    response = await client.post_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTransaction",
            "params": [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": "finalized",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        }
    )
    if not isinstance(response, dict) or response.get("error") is not None:
        raise LandingReconciliationError.malformed("RPC response")
    return reconcile_finalized_transaction(
        response.get("result"),
        signature=signature,
        wallet_pubkey=wallet_pubkey,
        mint=mint,
        side=side,
        jito_tip_accounts=jito_tip_accounts,
        expected_jito_tip_lamports=expected_jito_tip_lamports,
    )


def reconcile_finalized_transaction(  # noqa: PLR0913
    result: object,
    *,
    signature: str,
    wallet_pubkey: str,
    mint: str,
    side: str,
    jito_tip_accounts: Sequence[str],
    expected_jito_tip_lamports: int,
) -> LandingReconciliation:
    """Derive exact deltas from one validated finalized RPC result."""

    if not isinstance(result, dict):
        raise LandingReconciliationError.malformed("result")
    slot = _non_negative_int(result.get("slot"), "slot")
    transaction = _mapping(result.get("transaction"), "transaction")
    signatures = transaction.get("signatures")
    if not isinstance(signatures, list) or not signatures or signatures[0] != signature:
        raise LandingReconciliationError.invalid(
            "finalized transaction signature does not match submitted bytes"
        )
    message = _mapping(transaction.get("message"), "message")
    account_keys = _account_keys(message.get("accountKeys"))
    protocol_fee_token_accounts = _protocol_fee_accounts(message, side)
    meta = _mapping(result.get("meta"), "meta")
    if meta.get("err") is not None:
        raise LandingReconciliationError.invalid(
            "finalized transaction contains an execution error"
        )
    pre_balances = _integer_list(meta.get("preBalances"), "preBalances")
    post_balances = _integer_list(meta.get("postBalances"), "postBalances")
    if len(pre_balances) != len(account_keys) or len(post_balances) != len(
        account_keys
    ):
        raise LandingReconciliationError.invalid(
            "finalized native balances do not match account keys"
        )
    wallet_index = _unique_account_index(account_keys, wallet_pubkey)
    network_fee = _non_negative_int(meta.get("fee"), "fee")
    token_balances = _token_balance_deltas(meta, account_keys)
    wallet_token_delta = sum(
        delta
        for account_index, balance_mint, owner, delta in token_balances
        if balance_mint == mint and owner == wallet_pubkey
    )
    ata_indexes = {
        account_index
        for account_index, balance_mint, owner, _delta in token_balances
        if owner == wallet_pubkey and balance_mint in (mint, WSOL_MINT_ID)
    }
    ata_rent = sum(
        post_balances[index]
        for index in ata_indexes
        if pre_balances[index] == 0 and post_balances[index] > 0
    )
    protocol_accounts = set(protocol_fee_token_accounts)
    protocol_fee = sum(
        max(0, delta)
        for account_index, balance_mint, _owner, delta in token_balances
        if account_keys[account_index] in protocol_accounts
        and balance_mint == WSOL_MINT_ID
    )
    transfers = parse_sol_transfers(result)
    jito_accounts = set(jito_tip_accounts)
    jito_tip = sum(
        transfer.lamports
        for transfer in transfers
        if transfer.sender == wallet_pubkey and transfer.recipient in jito_accounts
    )
    if jito_tip != expected_jito_tip_lamports:
        raise LandingReconciliationError.invalid(
            "finalized Jito tip does not match the signed transaction policy"
        )
    return LandingReconciliation(
        signature=signature,
        landed_slot=slot,
        token_delta_base_units=wallet_token_delta,
        sol_delta_lamports=post_balances[wallet_index] - pre_balances[wallet_index],
        network_fee_lamports=network_fee,
        jito_tip_lamports=jito_tip,
        ata_rent_lamports=ata_rent,
        protocol_fee_lamports=protocol_fee,
    )


def _token_balance_deltas(
    meta: Mapping[str, object],
    account_keys: tuple[str, ...],
) -> tuple[tuple[int, str, str, int], ...]:
    pre = _token_balance_rows(meta.get("preTokenBalances"), account_keys)
    post = _token_balance_rows(meta.get("postTokenBalances"), account_keys)
    identities = set(pre) | set(post)
    return tuple(
        (
            account_index,
            mint,
            owner,
            post.get(identity, 0) - pre.get(identity, 0),
        )
        for identity in sorted(identities)
        for account_index, mint, owner in (identity,)
    )


def _token_balance_rows(
    value: object,
    account_keys: tuple[str, ...],
) -> dict[tuple[int, str, str], int]:
    if value is None:
        return {}
    if not isinstance(value, list):
        raise LandingReconciliationError.malformed("token balances")
    parsed: dict[tuple[int, str, str], int] = {}
    for row_value in value:
        row = _mapping(row_value, "token balance row")
        account_index = _non_negative_int(row.get("accountIndex"), "accountIndex")
        if account_index >= len(account_keys):
            raise LandingReconciliationError.invalid(
                "finalized token balance account index is out of bounds"
            )
        mint = _text(row.get("mint"), "token balance mint")
        owner = _text(row.get("owner"), "token balance owner")
        ui_amount = _mapping(row.get("uiTokenAmount"), "uiTokenAmount")
        amount_text = _text(ui_amount.get("amount"), "token amount")
        if not amount_text.isdigit():
            raise LandingReconciliationError.malformed("token amount")
        identity = (account_index, mint, owner)
        if identity in parsed:
            raise LandingReconciliationError.invalid(
                "finalized token balance identity is duplicated"
            )
        parsed[identity] = int(amount_text)
    return parsed


def _account_keys(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise LandingReconciliationError.malformed("accountKeys")
    parsed: list[str] = []
    for item in value:
        if type(item) is str:
            parsed.append(_text(item, "account key"))
            continue
        item_mapping = _mapping(item, "account key")
        parsed.append(_text(item_mapping.get("pubkey"), "account key pubkey"))
    if len(set(parsed)) != len(parsed):
        raise LandingReconciliationError.invalid(
            "finalized transaction contains duplicate account keys"
        )
    return tuple(parsed)


def _protocol_fee_accounts(
    message: Mapping[str, object],
    side: str,
) -> tuple[str, str, str]:
    _validate_side(side)
    instructions = message.get("instructions")
    if not isinstance(instructions, list):
        raise LandingReconciliationError.malformed("message instructions")
    pump_instructions = [
        _mapping(instruction, "Pump instruction")
        for instruction in instructions
        if isinstance(instruction, dict)
        and instruction.get("programId") == PUMP_PROGRAM_ID
    ]
    if len(pump_instructions) != 1:
        raise LandingReconciliationError.invalid(
            "finalized transaction must contain exactly one Pump trade instruction"
        )
    accounts = pump_instructions[0].get("accounts")
    account_names = BUY_V2_ACCOUNT_NAMES if side == "buy" else SELL_V2_ACCOUNT_NAMES
    if not isinstance(accounts, list) or len(accounts) != len(account_names):
        raise LandingReconciliationError.malformed("Pump instruction accounts")
    by_role = dict(zip(account_names, accounts, strict=True))
    fee_accounts = (
        by_role["associated_quote_fee_recipient"],
        by_role["associated_quote_buyback_fee_recipient"],
        by_role["associated_creator_vault"],
    )
    if any(type(account) is not str for account in fee_accounts):
        raise LandingReconciliationError.malformed("Pump fee account")
    _validate_pubkey_sequence(fee_accounts, "Pump fee accounts")
    return fee_accounts


def _integer_list(value: object, field_name: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise LandingReconciliationError.malformed(field_name)
    return tuple(_non_negative_int(item, field_name) for item in value)


def _unique_account_index(account_keys: tuple[str, ...], pubkey: str) -> int:
    try:
        return account_keys.index(pubkey)
    except ValueError as error:
        raise LandingReconciliationError.invalid(
            "execution wallet is absent from finalized account keys"
        ) from error


def _mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        raise LandingReconciliationError.malformed(field_name)
    return value


def _text(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise LandingReconciliationError.malformed(field_name)
    return value


def _non_negative_int(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise LandingReconciliationError.malformed(field_name)
    return value


def _validate_pubkey(value: object, field_name: str) -> None:
    _validate_non_empty_text(value, field_name)
    try:
        Pubkey.from_string(value)
    except ValueError as error:
        raise LandingReconciliationError.invalid(
            f"{field_name} must be a valid Solana public key"
        ) from error


def _validate_pubkey_sequence(value: object, field_name: str) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise LandingReconciliationError.invalid(f"{field_name} must be a sequence")
    if any(type(item) is not str for item in value):
        raise LandingReconciliationError.invalid(
            f"{field_name} must contain public key strings"
        )
    for item in value:
        _validate_pubkey(item, field_name)


def _validate_side(value: object) -> None:
    if value not in ("buy", "sell"):
        raise LandingReconciliationError.invalid("side must be buy or sell")


def _validate_non_empty_text(value: object, field_name: str) -> None:
    if type(value) is not str or not value:
        raise LandingReconciliationError.invalid(f"{field_name} must be non-empty text")


def _validate_non_negative_int(value: object, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise LandingReconciliationError.invalid(
            f"{field_name} must be a non-negative integer"
        )


__all__ = [
    "LandingReconciliation",
    "LandingReconciliationError",
    "reconcile_finalized_landing",
    "reconcile_finalized_transaction",
]
