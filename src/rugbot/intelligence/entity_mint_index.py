"""Entity-wide Pump.fun mint discovery with finalized RPC confirmation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from rugbot.integrations.pumpfun_creator_index import (
    PumpfunCreatedTokenCandidate,
    fetch_pumpfun_created_tokens,
)
from rugbot.intelligence.token_resolver import resolve_token_or_wallet

MAX_ENTITY_WALLETS = 100
INDEX_CONCURRENCY = 5
CONFIRM_CONCURRENCY = 3


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
    bonding_curve: str
    relation: str


@dataclass(frozen=True, slots=True)
class EntityMintDiscovery:
    """Finalized entity mints plus explicit partial-discovery warnings."""

    mints: tuple[FinalizedEntityMint, ...]
    warnings: tuple[str, ...]


async def discover_finalized_entity_mints(  # noqa: C901
    *,
    target_wallet: str,
    graph_wallets: tuple[str, ...],
    endpoint: str,
    fallback_endpoints: tuple[str, ...] = (),
) -> EntityMintDiscovery:
    """Discover explicit creator mints across every currently known graph wallet."""

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

    confirm_semaphore = asyncio.Semaphore(CONFIRM_CONCURRENCY)

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
            bonding_curve=resolution.bonding_curve,
            relation=(
                "target_creator"
                if candidate.creator == target_wallet
                else "linked_graph_creator"
            ),
        )

    confirmations = await asyncio.gather(
        *(confirmed(candidate) for candidate in candidates.values())
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
            "bonding_curve": mint.bonding_curve,
            "relation": mint.relation,
            "status": "FINALIZED MINT",
        }
        for mint in discovery.mints
    ]
