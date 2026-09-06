"""§14.3 metric density and §14.2 synthetic-feed guards for cluster backtest surfaces.

Every value asserted here is produced by the real optimizer or read back from a
persisted SQLite tracker. No metric is mocked: reports come from
``run_cluster_tp_grid_search`` and the TUI record comes from ``RugbotTuiApp``
running its real event loop against a real repository.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from rich.console import Console

from rugbot.backtest.runners.cluster_optimizer import (
    HistoricalTokenSample,
    run_cluster_tp_grid_search,
)
from rugbot.domain.entities import LaunchRecord, TargetRecord
from rugbot.interfaces.tui.app import RugbotTuiApp, TargetsTable
from rugbot.interfaces.tui.widgets.panels.backtest_matrix_view import (
    BacktestMatrixWidget,
)
from rugbot.interfaces.tui.widgets.panels.inspector import TargetProfileCard
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.models import FunderRecord

TARGET_FUNDER = "2r2HuRi1vLzVxXnWAffWfsAMDkQpfG1c23KPDgR4wp5p"
UNRELATED_FUNDER = "FJz6SLz8CQBmm692kfp6e8s9FZPuqnKX5ZNcr7k5Kadd"
BUY_SIZE_SOL = 0.025

# Fees larger than any achievable gain at a 1.05x peak, so every grid row loses.
DOMINANT_JITO_TIP_SOL = 1.0
# Fees negligible against a 5x peak, so the optimizer must flag a winning row.
NEGLIGIBLE_JITO_TIP_SOL = 0.001


def _samples(ath_multiplier: float) -> tuple[HistoricalTokenSample, ...]:
    """Build three launches from one operator with a known peak multiplier."""
    created_at = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp())
    return tuple(
        HistoricalTokenSample(
            mint=f"mint{index}",
            symbol=f"SYM{index}",
            creator_wallet=TARGET_FUNDER,
            created_slot=1_000 + index,
            created_at=created_at + (index * 3600),
            ath_multiplier=ath_multiplier,
            ath_delay_seconds=45,
            rug_delay_seconds=90,
            entry_mc_usd=5000.0,
            peak_mc_usd=5000.0 * ath_multiplier,
        )
        for index in range(3)
    )


def _report(ath_multiplier: float, jito_tip_sol: float):
    return run_cluster_tp_grid_search(
        root_funder=TARGET_FUNDER,
        samples=_samples(ath_multiplier),
        buy_size_sol=BUY_SIZE_SOL,
        jito_tip_sol=jito_tip_sol,
        gas_fee_sol=0.0005,
    )


def test_optimal_evaluation_is_none_when_fees_dominate_every_tp_row():
    """An unprofitable cluster must expose no optimal row to render metrics from.

    ``run_cluster_tp_grid_search`` flags a row optimal only when the best
    expected value is positive. Callers that substituted the first grid row
    reported a winrate and a fee total for a strategy the same report labels
    UNPROFITABLE.
    """
    report = _report(ath_multiplier=1.05, jito_tip_sol=DOMINANT_JITO_TIP_SOL)

    assert report.evaluations, "the grid must still be evaluated"
    assert report.is_net_profitable is False
    assert report.optimal_tp_multiplier is None
    assert report.optimal_tp_label == "UNPROFITABLE"
    assert report.optimal_evaluation is None
    assert not any(row.is_optimal for row in report.evaluations)


def test_optimal_evaluation_returns_the_flagged_row_when_a_tp_is_profitable():
    report = _report(ath_multiplier=5.0, jito_tip_sol=NEGLIGIBLE_JITO_TIP_SOL)

    optimal = report.optimal_evaluation

    assert report.is_net_profitable is True
    assert optimal is not None
    assert optimal.is_optimal is True
    assert optimal.tp_multiplier == report.optimal_tp_multiplier
    assert optimal.net_roi_pct == report.optimal_roi_pct
    assert optimal.winrate_pct == 100.0


def test_report_carries_the_fee_legs_rendered_as_the_14_3_breakdown():
    """The three fee legs must be readable off the report, not re-derived."""
    report = _report(ath_multiplier=5.0, jito_tip_sol=NEGLIGIBLE_JITO_TIP_SOL)

    assert report.jito_tip_sol == NEGLIGIBLE_JITO_TIP_SOL
    assert report.gas_fee_sol == 0.0005
    assert report.dex_fee_pct > 0.0
    optimal = report.optimal_evaluation
    assert optimal is not None
    assert optimal.total_fees_paid_sol > 0.0


def test_target_record_defaults_do_not_fabricate_a_track_record():
    """Unmeasured is None; 0.0 would read as a measured zero winrate."""
    record = TargetRecord(address=TARGET_FUNDER)

    assert record.launches_count == 0
    assert record.winrate_pct is None
    assert record.avg_ath_pct is None


def test_target_profile_card_reports_unmeasured_instead_of_a_zero_winrate():
    card = TargetProfileCard(target=TargetRecord(address=TARGET_FUNDER))
    rendered = card._render_content()

    assert "winrate/ATH unmeasured" in rendered
    assert "0.0% WR" not in rendered
    assert "+0% avg ATH" not in rendered


def test_target_profile_card_renders_a_measured_track_record():
    card = TargetProfileCard(
        target=TargetRecord(
            address=TARGET_FUNDER,
            launches_count=12,
            winrate_pct=75.0,
            avg_ath_pct=180.0,
        )
    )
    rendered = card._render_content()

    assert "12 recorded launches" in rendered
    assert "75.0% WR" in rendered
    assert "+180% avg ATH" in rendered
    assert "winrate/ATH unmeasured" not in rendered


def _rendered_text(renderable) -> str:
    console = Console(record=True, width=140)
    console.print(renderable)
    return console.export_text()


def test_matrix_empty_state_states_the_missing_dataset():
    """The empty panel must not promise a grid the tracker cannot support."""
    rendered = _rendered_text(BacktestMatrixWidget()._render_empty())

    assert "No persisted launch-outcome dataset" in rendered
    assert "rug_wallet" in rendered
    assert "Press" not in rendered


def test_matrix_header_does_not_convert_sol_at_an_unsourced_rate():
    """The report carries no SOL price, so no fiat figure may be rendered."""
    report = _report(ath_multiplier=5.0, jito_tip_sol=NEGLIGIBLE_JITO_TIP_SOL)
    widget = BacktestMatrixWidget(report=report)

    rendered = _rendered_text(widget._render_matrix())

    assert f"Buy Size: {BUY_SIZE_SOL:.3f} SOL" in rendered
    assert "~$" not in rendered
    # §14.3 density that is computed from the samples must still be drawn.
    assert "WINRATE" in rendered
    assert "FEES PAID" in rendered
    assert "NET ROI" in rendered


def test_tui_backtest_action_abstains_and_scopes_to_the_target_cluster(tmp_path):
    """End-to-end: real repository, real event loop, no fabricated metrics."""

    async def _run() -> None:
        repo = SQLiteTrackerRepository(DatabaseManager(tmp_path / "rugbot.db"))
        now_iso = datetime.now(UTC).isoformat()
        repo.save_funder(
            FunderRecord(
                id=0,
                address=TARGET_FUNDER,
                label="Cluster Alpha",
                enabled=True,
                created_at=now_iso,
                last_seen_at=now_iso,
            )
        )
        for index, funder in enumerate(
            (TARGET_FUNDER, TARGET_FUNDER, UNRELATED_FUNDER)
        ):
            repo.save_launch(
                LaunchRecord(
                    mint=f"mint{index}",
                    creator_wallet=funder,
                    root_funder=funder,
                    symbol=f"SYM{index}",
                    name=f"Token {index}",
                    created_signature=f"sig{index}",
                    created_slot=1_000 + index,
                    created_at=1_756_000_000 + index,
                    depth=0,
                )
            )

        app = RugbotTuiApp(state_dir=tmp_path)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()

            table = app.query_one("#targets-table", TargetsTable)
            target = table.get_target(TARGET_FUNDER)
            assert target is not None

            updated, message = app._execute_backtest_simulation(target)

            # Scoped to the target cluster: the unrelated launch is excluded.
            assert updated.launches_count == 2
            # §14.2: no synthesized winrate, ATH, or R multiple.
            assert updated.winrate_pct is None
            assert updated.avg_ath_pct is None
            assert updated.perf_metric == "outcome dataset unavailable"
            assert "BACKTEST ABSTAINED" in message
            assert "2 recorded launches" in message

            # The bound key action must run the same abstention without raising.
            await pilot.press("b")
            await pilot.pause()

    asyncio.run(_run())
