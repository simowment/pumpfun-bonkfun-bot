"""Resolve a token mint or wallet address to a canonical OperatorEntity with full cluster metrics."""

from __future__ import annotations

from rugbot.backtest.runners.cluster_optimizer import (
    HistoricalTokenSample,
    run_cluster_tp_grid_search,
)
from rugbot.domain.entities import OperatorEntity
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
        if resolved.cluster_tokens:
            for t in resolved.cluster_tokens:
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

        report = run_cluster_tp_grid_search(
            root_funder=root_funder,
            samples=samples_list,
            buy_size_sol=0.025,
            realized_dump_loss_pct=0.75,
            jito_tip_sol=0.0010,
            gas_fee_sol=0.0001,
        )

        opt_multiplier = report.optimal_tp_multiplier or 1.0
        opt_ev = next((ev for ev in report.evaluations if ev.is_optimal), None)
        winrate = opt_ev.winrate_pct if opt_ev else 0.0

        return OperatorEntity(
            address=dev_wallet,
            label=resolved.name or resolved.default_label,
            root_funder=root_funder,
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
