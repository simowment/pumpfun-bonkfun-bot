"""Entity-wide Pump.fun mint discovery with finalized RPC confirmation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rugbot.integrations.pumpfun_creator_index import (
    PumpfunCreatedTokenCandidate,
    fetch_pumpfun_created_tokens,
)
from rugbot.intelligence.token_resolver import BundleBuy, resolve_token_or_wallet

MAX_ENTITY_WALLETS = 100
MAX_CONFIRMED_ENTITY_MINTS = 15
INDEX_CONCURRENCY = 5
CONFIRM_CONCURRENCY = 8


@dataclass(frozen=True, slots=True)
class FinalizedEntityMint:
    """A Pump.fun creator-index nomination confirmed by finalized RPC."""

    mint: str
    creator: str
    name: str
    symbol: str
    created_timestamp: int
    creation_slot: int
    creation_signature: str
    creation_transaction_index: int | None
    bonding_curve: str
    relation: str
    bundle_buys: tuple[BundleBuy, ...] = ()


@dataclass(frozen=True, slots=True)
class EntityMintDiscovery:
    """Finalized entity mints plus explicit partial-discovery warnings."""

    mints: tuple[FinalizedEntityMint, ...]
    warnings: tuple[str, ...]


async def discover_finalized_entity_mints(  # noqa: C901, PLR0913
    *,
    target_wallet: str,
    graph_wallets: tuple[str, ...],
    endpoint: str,
    fallback_endpoints: tuple[str, ...] = (),
    max_mints: int = MAX_CONFIRMED_ENTITY_MINTS,
    anchor_mint: str | None = None,
) -> EntityMintDiscovery:
    """Discover explicit creator mints across every currently known graph wallet."""

    if max_mints < 1 or max_mints > MAX_CONFIRMED_ENTITY_MINTS:
        raise ValueError("max_mints must be between 1 and 15")  # noqa: TRY003
    wallets = tuple(dict.fromkeys((target_wallet, *graph_wallets)))
    if len(wallets) > MAX_ENTITY_WALLETS:
        return EntityMintDiscovery(
            mints=(),
            warnings=("entity mint discovery exceeds the 100-wallet bound",),
        )
    index_semaphore = asyncio.Semaphore(INDEX_CONCURRENCY)

    async def indexed(wallet: str) -> tuple[PumpfunCreatedTokenCandidate, ...]:
        async with index_semaphore:
            return await asyncio.to_thread(fetch_pumpfun_created_tokens, wallet)

    indexed_results = await asyncio.gather(
        *(indexed(wallet) for wallet in wallets),
        return_exceptions=True,
    )
    warnings: list[str] = []
    candidates: dict[str, PumpfunCreatedTokenCandidate] = {}
    for wallet, result in zip(wallets, indexed_results, strict=True):
        if isinstance(result, BaseException):
            warnings.append(
                f"creator mint index unavailable for {wallet}: {type(result).__name__}"
            )
            continue
        for candidate in result:
            previous = candidates.get(candidate.mint)
            if previous is not None and previous != candidate:
                warnings.append(f"conflicting creator nomination for {candidate.mint}")
                candidates.pop(candidate.mint, None)
                continue
            candidates[candidate.mint] = candidate

    confirm_semaphore = asyncio.Semaphore(8)

    async def confirmed(
        candidate: PumpfunCreatedTokenCandidate,
    ) -> FinalizedEntityMint | str:
        async with confirm_semaphore:
            try:
                resolution = await asyncio.to_thread(
                    resolve_token_or_wallet,
                    candidate.mint,
                    rpc_url=endpoint,
                    fallback_endpoints=fallback_endpoints,
                )
            except (OSError, RuntimeError, ValueError) as error:
                return f"mint confirmation failed for {candidate.mint}: {error}"
        if (
            not resolution.is_token
            or resolution.target_wallet != candidate.creator
            or resolution.creation_slot is None
            or resolution.creation_signature is None
            or resolution.bonding_curve is None
        ):
            return f"mint confirmation conflicted for {candidate.mint}"
        return FinalizedEntityMint(
            mint=candidate.mint,
            creator=candidate.creator,
            name=candidate.name,
            symbol=candidate.symbol,
            created_timestamp=candidate.created_timestamp,
            creation_slot=resolution.creation_slot,
            creation_signature=resolution.creation_signature,
            creation_transaction_index=resolution.creation_transaction_index,
            bonding_curve=resolution.bonding_curve,
            bundle_buys=tuple(
                buy for buy in resolution.bundle_buys if buy.wallet != candidate.creator
            ),
            relation=(
                "target_creator"
                if candidate.creator == target_wallet
                else "linked_graph_creator"
            ),
        )

    ordered_candidates = sorted(
        candidates.values(),
        key=lambda candidate: (-candidate.created_timestamp, candidate.mint),
    )
    anchor = candidates.get(anchor_mint) if anchor_mint is not None else None
    if anchor_mint is not None and anchor is None:
        warnings.append(f"anchor mint is absent from creator index: {anchor_mint}")
    if anchor is not None:
        ordered_candidates = sorted(
            candidates.values(),
            key=lambda candidate: (
                abs(candidate.created_timestamp - anchor.created_timestamp),
                -candidate.created_timestamp,
                candidate.mint,
            ),
        )
    if len(ordered_candidates) > max_mints:
        window = "around anchor" if anchor is not None else "newest"
        warnings.append(
            f"entity mint confirmation limited to {window} {max_mints} of "
            f"{len(ordered_candidates)} indexed mints"
        )
    confirmations = await asyncio.gather(
        *(confirmed(candidate) for candidate in ordered_candidates[:max_mints])
    )
    mints: list[FinalizedEntityMint] = []
    for confirmation in confirmations:
        if isinstance(confirmation, str):
            warnings.append(confirmation)
        else:
            mints.append(confirmation)
    return EntityMintDiscovery(
        mints=tuple(sorted(mints, key=lambda mint: (mint.creation_slot, mint.mint))),
        warnings=tuple(warnings),
    )


def entity_mint_discovery_to_json(
    discovery: EntityMintDiscovery,
) -> list[dict[str, object]]:
    """Serialize finalized entity-wide mint evidence for interfaces."""

    return [
        {
            "mint": mint.mint,
            "creator": mint.creator,
            "name": mint.name,
            "symbol": mint.symbol,
            "created_at": mint.created_timestamp,
            "slot": mint.creation_slot,
            "signature": mint.creation_signature,
            "transaction_index": mint.creation_transaction_index,
            "position_is_zero_or_one": mint.creation_transaction_index in {0, 1}
            if mint.creation_transaction_index is not None
            else None,
            "bundle_buys": [
                {
                    "wallet": buy.wallet,
                    "signature": buy.signature,
                    "transaction_index": buy.transaction_index,
                    "token_amount": buy.token_amount,
                    "max_sol_cost_lamports": buy.max_sol_cost_lamports,
                }
                for buy in mint.bundle_buys
            ],
            "bonding_curve": mint.bonding_curve,
            "relation": mint.relation,
            "status": "FINALIZED MINT",
        }
        for mint in discovery.mints
    ]
