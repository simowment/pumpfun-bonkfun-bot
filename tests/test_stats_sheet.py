"""Tests for the reusable token statistics sheet module and cabal sheet CLI."""

# ruff: noqa: S106

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from rich.table import Table

if TYPE_CHECKING:
    import pytest

from rugbot.discover.cabal import CabalCluster, CabalStore
from rugbot.interfaces.cli.cabal import build_parser, run_trades
from rugbot.reporting.stats_sheet import (
    StatsSummary,
    TokenStatsSheet,
    TokenTradeStatRow,
    copy_to_clipboard,
)

MINT_1 = "5SKLS8gvhJgAtmtmRr7SQg7NjoehutwK3vAzYPAYpump"
MINT_2 = "F2reRYPQUPkagQy7t5VaZgtiHqXNFQ91ej15GRypump"


def test_token_trade_stat_row_serialization() -> None:
    """Verify TokenTradeStatRow fields and dictionary conversion."""
    row = TokenTradeStatRow(
        mint=MINT_1,
        symbol="UPONLY",
        token_name="Up Only",
        launch_time="2026-09-14 10:00:00 UTC",
        launch_timestamp=1726308000.0,
        entry_time="2026-09-14 10:00:05 UTC",
        entry_timestamp=1726308005.0,
        entry_age_seconds=5.0,
        entry_price_sol=0.00003,
        entry_sol_amount=0.50,
        entry_mcap_usd=5000.0,
        ath_mcap_usd=25000.0,
        ath_multiplier=5.0,
        time_to_peak_seconds=120.0,
        confluence_count=2,
        cabal_cluster_id="cabal-001",
        buyer_wallet="Wallet111111111111111111111111111111111",
        exit_time="2026-09-14 10:02:00 UTC",
        exit_reason="take_profit_ladder",
        exit_mcap_usd=21250.0,
        realized_pnl_sol=0.65,
        net_roi_pct=130.0,
        is_win=True,
        status="CLOSED",
    )
    d = row.to_dict()
    assert d["mint"] == MINT_1
    assert d["symbol"] == "UPONLY"
    assert d["ath_multiplier"] == 5.0
    assert d["entry_age_seconds"] == 5.0
    assert d["is_win"] is True


def test_stats_summary_calculation() -> None:
    """Verify aggregated KPI metrics computed by TokenStatsSheet."""
    sheet = TokenStatsSheet(title="TEST SHEET")

    # Win row
    r1 = TokenTradeStatRow(
        mint=MINT_1,
        symbol="WIN1",
        entry_sol_amount=0.50,
        entry_age_seconds=4.0,
        ath_multiplier=5.0,
        time_to_peak_seconds=60.0,
        realized_pnl_sol=0.60,
        net_roi_pct=120.0,
        is_win=True,
    )
    # Loss row
    r2 = TokenTradeStatRow(
        mint=MINT_2,
        symbol="LOSS1",
        entry_sol_amount=0.50,
        entry_age_seconds=6.0,
        ath_multiplier=1.2,
        time_to_peak_seconds=30.0,
        realized_pnl_sol=-0.40,
        net_roi_pct=-80.0,
        is_win=False,
    )
    sheet.add_row(r1)
    sheet.add_row(r2)

    summary = sheet.compute_summary()
    assert isinstance(summary, StatsSummary)
    assert summary.sample_count == 2
    assert summary.win_count == 1
    assert summary.loss_count == 1
    assert summary.winrate_pct == 50.0
    assert summary.total_entry_sol == 1.00
    assert round(summary.total_realized_pnl_sol, 4) == 0.20
    assert summary.net_ev_sol_per_trade == 0.10
    assert summary.avg_entry_age_seconds == 5.0
    assert summary.avg_time_to_peak_seconds == 45.0
    assert summary.profit_factor == 1.50


def test_token_stats_sheet_renderers(tmp_path: Path) -> None:
    """Verify terminal table, CSV, markdown, and JSON rendering."""
    sheet = TokenStatsSheet(title="UNIT TEST SHEET")
    sheet.add_row(
        TokenTradeStatRow(
            mint=MINT_1,
            symbol="UPONLY",
            launch_time="2026-09-14 10:00:00 UTC",
            entry_age_seconds=3.2,
            entry_sol_amount=0.25,
            ath_mcap_usd=50000.0,
            ath_multiplier=5.0,
            time_to_peak_seconds=90.0,
            exit_reason="tp_2x",
            realized_pnl_sol=0.33,
            net_roi_pct=132.0,
            is_win=True,
        )
    )

    # 1. Terminal Table
    table = sheet.to_table()
    assert "UNIT TEST SHEET" in table
    assert MINT_1[:8] in table
    assert "UPONLY" in table
    assert "+0.3300" in table
    assert "SUMMARY:" in table

    # 2. Markdown Table
    md = sheet.to_markdown()
    assert "### UNIT TEST SHEET" in md
    assert "UPONLY" in md
    assert "**Winrate: 100.0%**" in md

    # 3. JSON output
    js = sheet.to_json()
    assert "UNIT TEST SHEET" in js
    assert MINT_1 in js

    # 4. CSV output and file saving
    csv_file = tmp_path / "sheet_test.csv"
    csv_text = sheet.to_csv(file_path=csv_file)
    assert "mint,symbol,token_name,launch_time" in csv_text
    assert MINT_1 in csv_text
    assert csv_file.exists()
    assert MINT_1 in csv_file.read_text(encoding="utf-8")


def test_token_stats_sheet_builders() -> None:
    """Verify from_executions and from_cabal_clusters builder factories."""
    mock_client = MagicMock()
    # Align created_timestamp with 2026-09-14T10:00:00Z (timestamp 1789380000.0)
    created_ts_sec = 1789380000.0
    mock_client.fetch_token.return_value = {
        "mint": MINT_1,
        "symbol": "MOCK",
        "name": "Mock Token",
        "created_timestamp": int(created_ts_sec * 1000),
        "ath_market_cap": 75000.0,
        "ath_market_cap_timestamp": int((created_ts_sec + 120.0) * 1000),
    }

    # 1. from_executions
    mock_executions = [
        {
            "position_id": "pos-001",
            "mint": MINT_1,
            "wallet_address": "Wallet111111111111111111111111111111111",
            "entry_price_sol": 0.00003,
            "entry_sol_amount": 0.50,
            "high_price_seen": 0.00015,
            "realized_pnl_sol": 0.65,
            "roi_pct": 130.0,
            "is_closed": 1,
            "exit_reason": "take_profit_ladder",
            "opened_at": "2026-09-14T10:00:03+00:00",
            "closed_at": "2026-09-14T10:02:00+00:00",
            "cabal_cluster_id": "cabal-001",
        }
    ]
    sheet_exec = TokenStatsSheet.from_executions(mock_executions, client=mock_client)
    assert len(sheet_exec.rows) == 1
    assert sheet_exec.rows[0].symbol == "MOCK"
    assert sheet_exec.rows[0].ath_multiplier == 5.0
    assert sheet_exec.rows[0].entry_age_seconds == 3.0

    # 2. from_cabal_clusters
    mock_cluster = CabalCluster(
        cluster_id="cabal-001",
        funder="Funder111111111111111111111111111111111",
        wallets=frozenset(["W1", "W2"]),
        winner_tokens=frozenset([MINT_1]),
        token_count=1,
        median_ath=5.0,
        mean_ath=5.0,
        winrate_2x=100.0,
        winrate_5x=100.0,
        avg_time_to_peak_sec=120.0,
        typical_buy_sol=0.80,
        tokens=(),
        discovered_at="2026-09-14T10:00:00+00:00",
    )
    sheet_cluster = TokenStatsSheet.from_cabal_clusters(
        [mock_cluster], client=mock_client
    )
    assert len(sheet_cluster.rows) == 1
    assert sheet_cluster.rows[0].mint == MINT_1
    assert sheet_cluster.rows[0].entry_sol_amount == 0.40  # 50% of 0.80 typical
    assert sheet_cluster.rows[0].ath_multiplier >= 2.0
    assert sheet_cluster.rows[0].is_win is True


def test_cli_trades_export_csv(tmp_path: Path) -> None:
    """Verify trades CLI command exports execution stats to CSV."""
    db_path = tmp_path / "test_trades.sqlite3"
    store = CabalStore(db_path=db_path)

    store.record_execution(
        SimpleNamespace(
            position_id="pos-001",
            mint=MINT_1,
            wallet_address="11111111111111111111111111111111",
            cabal_cluster_id="cabal-001",
            entry_price_sol=0.0001,
            entry_sol_amount=0.25,
            token_amount=2500.0,
            remaining_tokens=0.0,
            high_price_seen=0.0002,
            realized_pnl_sol=0.05,
            current_roi_pct=20.0,
            is_closed=True,
            exit_reason="take_profit_2x",
            opened_at="2026-09-14T10:00:00+00:00",
            closed_at="2026-09-14T10:05:00+00:00",
        )
    )

    parser = build_parser()
    csv_out = tmp_path / "exported_stats.csv"

    # Run trades --csv <path>
    args = parser.parse_args(
        [
            "trades",
            "--db",
            str(db_path),
            "--csv",
            str(csv_out),
        ]
    )
    ret = run_trades(args)
    assert ret == 0
    assert csv_out.exists()
    assert MINT_1 in csv_out.read_text(encoding="utf-8")


def test_parquet_export_and_import(tmp_path: Path) -> None:
    """Verify Apache Parquet export and roundtrip reconstruction."""
    sheet = TokenStatsSheet(title="PARQUET TEST SHEET")
    sheet.add_row(
        TokenTradeStatRow(
            mint=MINT_1,
            symbol="UPONLY",
            token_name="Up Only",
            launch_time="2026-09-14 10:00:00 UTC",
            entry_age_seconds=2.5,
            entry_sol_amount=0.50,
            ath_mcap_usd=500000.0,
            ath_multiplier=100.0,
            time_to_peak_seconds=600.0,
            realized_pnl_sol=1.25,
            net_roi_pct=250.0,
            is_win=True,
            status="CLOSED",
        )
    )

    parquet_file = tmp_path / "test_sheet.parquet"
    out_path = sheet.to_parquet(parquet_file)
    assert Path(out_path).exists()
    assert Path(out_path).stat().st_size > 0

    # Load back via from_parquet
    loaded_sheet = TokenStatsSheet.from_parquet(parquet_file)
    assert len(loaded_sheet.rows) == 1
    r = loaded_sheet.rows[0]
    assert r.mint == MINT_1
    assert r.symbol == "UPONLY"
    assert r.ath_multiplier == 100.0
    assert r.entry_sol_amount == 0.50
    assert r.realized_pnl_sol == 1.25
    assert r.is_win is True
    assert r.status == "CLOSED"


def test_rich_table_and_mints_panel() -> None:
    """Verify rich Table creation and copyable mints extraction."""
    sheet = TokenStatsSheet(title="RICH TEST")
    sheet.add_row(
        TokenTradeStatRow(
            mint=MINT_1,
            symbol="WOFI",
            realized_pnl_sol=1.2997,
            net_roi_pct=227.9,
            ath_multiplier=111587.0,
            entry_sol_amount=0.57,
            is_win=True,
        )
    )

    # Compact rich table
    t_compact = sheet.to_rich_table(full_mint=False)
    assert isinstance(t_compact, Table)
    assert len(t_compact.columns) == 12

    # Full mint rich table
    t_full = sheet.to_rich_table(full_mint=True)
    assert isinstance(t_full, Table)
    assert len(t_full.columns) == 12

    # Mints extraction
    mints = sheet.get_mints()
    assert mints == [MINT_1]


def test_clipboard_copy_helper() -> None:
    """Verify clipboard helper handles valid strings and empty strings gracefully."""
    # Empty string should fail-fast
    assert copy_to_clipboard("") is False
    assert copy_to_clipboard("   ") is False

    # Non-empty string should succeed or fail without crashing
    res = copy_to_clipboard(MINT_1)
    assert isinstance(res, bool)


def test_cli_trades_parquet_and_clipboard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify trades CLI command exports to Parquet, outputs raw mints, and copies mint."""
    db_path = tmp_path / "cabal_trades_test.sqlite3"
    store = CabalStore(db_path=db_path)

    store.record_execution(
        SimpleNamespace(
            position_id="pos-001",
            mint=MINT_1,
            wallet_address="11111111111111111111111111111111",
            cabal_cluster_id="cabal-001",
            entry_price_sol=0.0001,
            entry_sol_amount=0.25,
            token_amount=2500.0,
            remaining_tokens=0.0,
            high_price_seen=0.0002,
            realized_pnl_sol=0.05,
            current_roi_pct=20.0,
            is_closed=True,
            exit_reason="take_profit_2x",
            opened_at="2026-09-14T10:00:00+00:00",
            closed_at="2026-09-14T10:05:00+00:00",
        )
    )

    parser = build_parser()
    parquet_out = tmp_path / "stats.parquet"

    # 1. Export to Parquet
    args_export = parser.parse_args(
        [
            "trades",
            "--db",
            str(db_path),
            "--parquet",
            str(parquet_out),
            "--plain",
        ]
    )
    ret_export = run_trades(args_export)
    assert ret_export == 0
    assert parquet_out.exists()

    # 2. Output raw copyable mints
    args_mints = parser.parse_args(
        [
            "trades",
            "--db",
            str(db_path),
            "--mints",
        ]
    )
    ret_mints = run_trades(args_mints)
    assert ret_mints == 0
    captured = capsys.readouterr()
    assert MINT_1 in captured.out

    # 3. Copy specific mint by index
    args_copy = parser.parse_args(
        [
            "trades",
            "--db",
            str(db_path),
            "--copy",
            "1",
        ]
    )
    ret_copy = run_trades(args_copy)
    assert ret_copy == 0
