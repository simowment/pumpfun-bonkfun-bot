"""Bundle pattern analysis over confirmed entity mints (pure aggregation)."""

from rugbot.intelligence.bundle_analysis import (
    analyze_entity_bundles,
    entity_bundle_analysis_to_json,
)
from rugbot.intelligence.entity_mint_index import FinalizedEntityMint
from rugbot.intelligence.token_resolver import BundleBuy

CREATOR = "Creator1111111111111111111111111111111111111"
BUNDLER_A = "BundlerAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
BUNDLER_B = "BundlerBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
ONE_OFF = "OneOff11111111111111111111111111111111111111"


def _buy(wallet: str, sig: str, index: int, sol_lamports: int) -> BundleBuy:
    return BundleBuy(
        wallet=wallet,
        signature=sig,
        transaction_index=index,
        token_amount=1_000_000,
        max_sol_cost_lamports=sol_lamports,
    )


def _mint(mint: str, slot: int, creator_idx: int, buys: tuple[BundleBuy, ...]):
    return FinalizedEntityMint(
        mint=mint,
        creator=CREATOR,
        name="Test",
        symbol="TST",
        created_timestamp=0,
        creation_slot=slot,
        creation_signature=f"create-{mint}",
        creation_transaction_index=creator_idx,
        bonding_curve=f"curve-{mint}",
        relation="target_creator",
        bundle_buys=buys,
    )


def test_first_buyer_ordered_by_transaction_index():
    analysis = analyze_entity_bundles(
        (
            _mint(
                "MintA",
                100,
                0,
                (
                    _buy(BUNDLER_A, "sig-b", 5, 100_000_000),
                    _buy(ONE_OFF, "sig-a", 2, 50_000_000),
                ),
            ),
        ),
        entity_creator=CREATOR,
    )
    launch = analysis.launches[0]
    assert launch.bundle_size == 2
    assert launch.first_buyer_wallet == ONE_OFF
    assert launch.first_buy_transaction_index == 2
    assert launch.total_max_sol_lamports == 150_000_000
    assert analysis.bundled_launch_count == 1


def test_repeat_crew_requires_two_creation_slot_mints():
    analysis = analyze_entity_bundles(
        (
            _mint(
                "MintA",
                100,
                0,
                (_buy(BUNDLER_A, "sig-a1", 2, 100_000_000),),
            ),
            _mint(
                "MintB",
                200,
                1,
                (
                    _buy(BUNDLER_A, "sig-b1", 3, 100_000_000),
                    _buy(ONE_OFF, "sig-b2", 4, 100_000_000),
                ),
            ),
        ),
        entity_creator=CREATOR,
    )
    assert len(analysis.repeat_bundlers) == 1
    crew = analysis.repeat_bundlers[0]
    assert crew.bundler_wallet == BUNDLER_A
    assert crew.entity_creator == CREATOR
    assert crew.mints == ("MintA", "MintB")
    assert crew.buy_count == 2
    assert crew.first_buy_slot == 100 and crew.last_buy_slot == 200
    assert any("pump-create" in ev for ev in crew.evidence_ids)
    assert any("sig-a1" in ev for ev in crew.evidence_ids)


def test_unbundled_launch_has_none_pattern_flags():
    analysis = analyze_entity_bundles(
        (_mint("MintA", 100, 5, ()),),
        entity_creator=CREATOR,
    )
    launch = analysis.launches[0]
    assert launch.bundle_size == 0
    assert launch.first_buyer_wallet is None
    assert analysis.bundled_launch_count == 0
    assert analysis.repeat_bundlers == ()


def test_json_serialization_shape():
    payload = entity_bundle_analysis_to_json(
        analyze_entity_bundles(
            (_mint("MintA", 100, 0, (_buy(BUNDLER_A, "sig-a", 2, 100_000_000),)),),
            entity_creator=CREATOR,
        )
    )
    assert payload["bundled_launch_count"] == 1
    assert payload["repeat_bundler_count"] == 0
    launch = payload["launches"][0]
    assert launch["mint"] == "MintA"
    assert launch["first_buyer_wallet"] == BUNDLER_A
    assert launch["buys"][0]["max_sol_cost_lamports"] == 100_000_000
