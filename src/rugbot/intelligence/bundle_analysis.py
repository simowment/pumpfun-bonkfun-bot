"""Creation-slot bundle pattern analysis across an entity's confirmed launches.

Every confirmed entity mint already carries its creation-slot buy evidence
(extracted from finalized RPC during mint confirmation). This module turns
that evidence into the operator patterns that matter: who bought first, how
many wallets bought inside the creation slot, whether the buys look
automated (identical sizes, indices adjacent to the create), and which
wallets repeat across multiple launches of the same creator — the proven
bundle-crew fingerprint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rugbot.intelligence.wallet_intelligence import (
    MIN_REPEAT_BUNDLER_MINTS,
    RepeatBundlerEntity,
)

if TYPE_CHECKING:
    from rugbot.intelligence.entity_mint_index import FinalizedEntityMint
    from rugbot.intelligence.token_resolver import BundleBuy
    from rugbot.tracker.models import BundleParticipationRecord


@dataclass(frozen=True, slots=True)
class LaunchBundleEvidence:
    """Creation-slot buy pattern for one confirmed entity launch."""

    mint: str
    symbol: str
    creation_slot: int
    creator_transaction_index: int | None
    bundle_size: int
    first_buyer_wallet: str | None
    first_buy_transaction_index: int | None
    total_max_sol_lamports: int
    buys: tuple[BundleBuy, ...]


@dataclass(frozen=True, slots=True)
class EntityBundleAnalysis:
    """Per-launch bundle evidence plus the cross-launch repeat crew."""

    launches: tuple[LaunchBundleEvidence, ...]
    repeat_bundlers: tuple[RepeatBundlerEntity, ...]
    bundled_launch_count: int


def _launch_evidence(mint: FinalizedEntityMint) -> LaunchBundleEvidence:
    buys = tuple(
        sorted(
            mint.bundle_buys,
            key=lambda buy: (
                buy.transaction_index if buy.transaction_index is not None else -1,
                buy.wallet,
            ),
        )
    )
    first = buys[0] if buys else None
    return LaunchBundleEvidence(
        mint=mint.mint,
        symbol=mint.symbol,
        creation_slot=mint.creation_slot,
        creator_transaction_index=mint.creation_transaction_index,
        bundle_size=len(buys),
        first_buyer_wallet=first.wallet if first else None,
        first_buy_transaction_index=(first.transaction_index if first else None),
        total_max_sol_lamports=sum(buy.max_sol_cost_lamports for buy in buys),
        buys=buys,
    )


def analyze_entity_bundles(
    mints: tuple[FinalizedEntityMint, ...],
    *,
    entity_creator: str,
) -> EntityBundleAnalysis:
    """Analyze creation-slot buys per launch and detect the repeat crew."""

    launches = tuple(_launch_evidence(mint) for mint in mints)
    crew_mints: dict[str, set[str]] = {}
    crew_buys: dict[str, list[BundleBuy]] = {}
    for mint in mints:
        for buy in mint.bundle_buys:
            crew_mints.setdefault(buy.wallet, set()).add(mint.mint)
            crew_buys.setdefault(buy.wallet, []).append(buy)

    repeat_bundlers: list[RepeatBundlerEntity] = []
    for wallet, attributed in sorted(crew_mints.items()):
        if len(attributed) < MIN_REPEAT_BUNDLER_MINTS:
            continue
        buys = tuple(sorted(crew_buys[wallet], key=lambda buy: buy.signature))
        mint_slots = {
            mint.mint: mint.creation_slot for mint in mints if mint.mint in attributed
        }
        mint_sigs = {
            mint.mint: mint.creation_signature
            for mint in mints
            if mint.mint in attributed
        }
        repeat_bundlers.append(
            RepeatBundlerEntity(
                bundler_wallet=wallet,
                entity_creator=entity_creator,
                mints=tuple(sorted(attributed)),
                buy_count=len(buys),
                first_buy_slot=min(mint_slots.values()),
                last_buy_slot=max(mint_slots.values()),
                evidence_ids=tuple(
                    sorted(f"transaction:{buy.signature}:pump-buy" for buy in buys)
                    + [
                        f"transaction:{mint_sigs[mint]}:pump-create"
                        for mint in sorted(attributed)
                    ]
                ),
            )
        )
    return EntityBundleAnalysis(
        launches=launches,
        repeat_bundlers=tuple(repeat_bundlers),
        bundled_launch_count=sum(1 for launch in launches if launch.bundle_size > 0),
    )


def cross_entity_bundles_to_json(
    participations: tuple[BundleParticipationRecord, ...],
) -> dict[str, object]:
    """Serialize creation-slot buys made by this entity's crew for OTHER creators.

    A wallet buying creation slots across multiple unrelated creators is itself
    the missing link: it proves a shared bundling operator even when no funding
    edge connects the entities. Grouped per wallet with distinct-creator count.
    """

    by_wallet: dict[str, list[BundleParticipationRecord]] = {}
    for item in participations:
        by_wallet.setdefault(item.bundler_wallet, []).append(item)
    wallets = []
    for wallet in sorted(by_wallet):
        items = by_wallet[wallet]
        creators = {item.creator for item in items}
        wallets.append(
            {
                "wallet": wallet,
                "external_creator_count": len(creators),
                "external_creators": sorted(creators),
                "buys": [
                    {
                        "mint": item.mint,
                        "creator": item.creator,
                        "creation_slot": item.creation_slot,
                        "buy_signature": item.buy_signature,
                        "transaction_index": item.transaction_index,
                        "max_sol_cost_lamports": item.max_sol_cost_lamports,
                    }
                    for item in items
                ],
            }
        )
    return {
        "wallet_count": len(wallets),
        "linked_entity_count": len({item.creator for item in participations}),
        "wallets": wallets,
    }


def entity_bundle_analysis_to_json(
    analysis: EntityBundleAnalysis,
) -> dict[str, object]:
    """Serialize launch bundle evidence for interfaces."""

    return {
        "bundled_launch_count": analysis.bundled_launch_count,
        "repeat_bundler_count": len(analysis.repeat_bundlers),
        "launches": [
            {
                "mint": launch.mint,
                "symbol": launch.symbol,
                "creation_slot": launch.creation_slot,
                "creator_transaction_index": launch.creator_transaction_index,
                "bundle_size": launch.bundle_size,
                "first_buyer_wallet": launch.first_buyer_wallet,
                "first_buy_transaction_index": launch.first_buy_transaction_index,
                "total_max_sol_lamports": launch.total_max_sol_lamports,
                "buys": [
                    {
                        "wallet": buy.wallet,
                        "signature": buy.signature,
                        "transaction_index": buy.transaction_index,
                        "token_amount": buy.token_amount,
                        "max_sol_cost_lamports": buy.max_sol_cost_lamports,
                    }
                    for buy in launch.buys
                ],
            }
            for launch in analysis.launches
        ],
    }
