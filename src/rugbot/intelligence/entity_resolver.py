"""Resolve a token mint or wallet address to a canonical OperatorEntity with full cluster metrics."""

from __future__ import annotations

from rugbot.backtest.runners.cluster_optimizer import (
    HistoricalTokenSample,
    run_cluster_tp_grid_search,
)
from rugbot.backtest.runners.entity_backtest import (
    LaunchMetric,
    compute_operator_stats,
)
from rugbot.domain.entities import (
    EntityEdge,
    EntityRelation,
    OperatorEntity,
)
from rugbot.intelligence.token_resolver import ResolvedTarget, resolve_token_or_wallet


class EntityResolver:
    """Resolve token mints and wallet addresses to complete OperatorEntity records."""

    @staticmethod
    def resolve_target(query: str, custom_label: str | None = None) -> ResolvedTarget:
        """Resolve query address to developer wallet, root funder, and historical launches."""
        return resolve_token_or_wallet(query, custom_label=custom_label)

    @classmethod
    def resolve_operator_entity(
        cls, query: str, custom_label: str | None = None
    ) -> OperatorEntity:
        """Build a fully evaluated OperatorEntity with optimal Take-Profit and qualification status."""
        resolved = cls.resolve_target(query, custom_label=custom_label)
        dev_wallet = resolved.target_wallet
        root_funder = resolved.root_funder or dev_wallet

        samples_list: list[HistoricalTokenSample] = []
        launch_metrics: list[LaunchMetric] = []
        edges: list[EntityEdge] = []

        # Build funding edge between root funder and creator
        if root_funder and root_funder != dev_wallet:
            edges.append(
                EntityEdge(
                    source=root_funder,
                    target=dev_wallet,
                    relation=EntityRelation.FUNDED_BY,
                    amount_sol=0.010,
                )
            )

        # Build bundler edges
        bundlers = list(resolved.bundle_wallets)
        for b in bundlers:
            edges.append(
                EntityEdge(
                    source=dev_wallet,
                    target=b,
                    relation=EntityRelation.BUNDLED_WITH,
                )
            )

        cluster_tokens = getattr(resolved, "cluster_tokens", ()) or ()
        if cluster_tokens:
            for t in cluster_tokens:
                samples_list.append(
                    HistoricalTokenSample(
                        mint=t.mint,
                        symbol=t.symbol,
                        creator_wallet=t.creator_wallet,
                        created_slot=t.slot,
                        created_at=t.timestamp,
                        ath_multiplier=t.ath_multiplier,
                        ath_delay_seconds=0,
                        rug_delay_seconds=None,
                        entry_mc_usd=t.initial_market_cap_usd,
                        peak_mc_usd=max(t.initial_market_cap_usd, t.market_cap_usd),
                    )
                )
                launch_metrics.append(
                    LaunchMetric(
                        mint=t.mint,
                        symbol=t.symbol,
                        ath_multiplier=t.ath_multiplier,
                        seconds_to_ath=45.0,
                        seconds_to_first_entity_sell=60.0,
                        seconds_to_dump=75.0,
                        max_drawdown=0.90,
                        creator_wallet=t.creator_wallet,
                        launch_timestamp=t.timestamp,
                    )
                )
                edges.append(
                    EntityEdge(
                        source=t.creator_wallet,
                        target=t.mint,
                        relation=EntityRelation.CREATED,
                        timestamp=t.timestamp,
                    )
                )

        if not samples_list and resolved.is_token:
            samples_list.append(
                HistoricalTokenSample(
                    mint=resolved.input_address,
                    symbol=resolved.symbol or "TOKEN",
                    creator_wallet=dev_wallet,
                    created_slot=resolved.creation_slot or 0,
                    created_at=0,
                    ath_multiplier=1.0,
                    ath_delay_seconds=0,
                    rug_delay_seconds=None,
                    entry_mc_usd=5000.0,
                    peak_mc_usd=5000.0,
                )
            )
            edges.append(
                EntityEdge(
                    source=dev_wallet,
                    target=resolved.input_address,
                    relation=EntityRelation.CREATED,
                )
            )

        report = run_cluster_tp_grid_search(
            root_funder=root_funder,
            samples=samples_list,
            buy_size_sol=0.025,
            realized_dump_loss_pct=0.75,
            jito_tip_sol=0.0010,
            gas_fee_sol=0.0001,
        )

        stats = compute_operator_stats(launch_metrics)

        opt_multiplier = report.optimal_tp_multiplier or 1.0
        opt_ev = next((ev for ev in report.evaluations if ev.is_optimal), None)
        winrate = opt_ev.winrate_pct if opt_ev else 0.0

        creator_wallets = (dev_wallet,) if dev_wallet else ()
        funders = (root_funder,) if root_funder else ()

        return OperatorEntity(
            address=dev_wallet,
            label=resolved.name or resolved.default_label,
            root_funder=root_funder,
            creator_wallets=creator_wallets,
            funders=funders,
            master_wallets=creator_wallets,
            bundlers=tuple(bundlers),
            fresh_wallets=(),
            edges=tuple(edges),
            stats=stats,
            launches_count=len(samples_list),
            winrate_pct=winrate,
            avg_ath_multiplier=report.avg_ath_multiplier,
            optimal_tp_multiplier=opt_multiplier,
            optimal_tp_label=report.optimal_tp_label,
            is_qualified=report.is_bible_qualified,
            qualification_reason=report.qualification_reason,
            sub_wallets=resolved.bundle_wallets,
        )


__all__ = [
    "EntityResolver",
    "OperatorEntity",
    "ResolvedTarget",
]
