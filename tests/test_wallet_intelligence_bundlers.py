"""Regression coverage for finalized repeat-bundler classification."""

import asyncio

from rugbot.domain.trades import TradeSide
from rugbot.intelligence.wallet_intelligence import (
    WalletPumpTrade,
    _repeat_bundler_entities,
)


def test_repeat_bundler_requires_two_finalized_buys_for_one_entity(
    monkeypatch,
) -> None:
    """Keep sells and unrelated creator entities out of bundler evidence."""

    entity = "7kWSBHb3cFHXLB3iyHxDEbCAG6jhpEgeGg6fFD55Z7w7"
    mint_a = "CcgzfZoTBdiJ5pRax7X9hXQ6kiYWcDgwPBP6tPn8pump"
    mint_b = "FbNVNE3QjCrrQdAZFubSvz7p6FmwysFAf537naafpump"
    unrelated = "Ch2gk7UVAYCqBonJVLuw1jrnwhP9KEYa7249SsXCpump"

    class Resolution:
        is_token = True
        creation_signature = "creation-signature"

        def __init__(self, mint: str) -> None:
            self.target_wallet = entity if mint in {mint_a, mint_b} else "other-entity"
            self.creation_slot = {mint_a: 10, mint_b: 20}.get(mint, 25)

    monkeypatch.setattr(
        "rugbot.intelligence.wallet_intelligence.resolve_token_or_wallet",
        lambda mint, rpc_url: Resolution(mint),
    )
    trades = (
        _trade(mint_a, TradeSide.BUY, 10, "sig-a"),
        _trade(mint_b, TradeSide.BUY, 20, "sig-b"),
        _trade(unrelated, TradeSide.SELL, 30, "sig-c"),
    )

    result = asyncio.run(_repeat_bundler_entities(trades, endpoint="https://rpc"))

    assert len(result) == 1
    assert result[0].entity_creator == entity
    assert result[0].mints == (mint_a, mint_b)
    assert result[0].buy_count == 2
    assert result[0].first_buy_slot == 10
    assert result[0].last_buy_slot == 20


def _trade(
    mint: str,
    side: TradeSide,
    slot: int,
    signature: str,
) -> WalletPumpTrade:
    return WalletPumpTrade(
        slot=slot,
        transaction_index=0,
        outer_instruction_index=3,
        signature=signature,
        mint=mint,
        side=side,
        wallet="43WTM7ddYoHG44cf1rdr3RXLDJUxHh2vNerDgLgTe5uN",
    )
