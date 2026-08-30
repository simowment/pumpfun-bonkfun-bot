"""VectorBT-style visual performance reports using Plotly and terminal equity charts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rugbot.domain.ohlc import OHLCCandle


@dataclass(frozen=True, slots=True)
class TradePerformanceRecord:
    trade_index: int
    mint: str
    entry_sol: float
    exit_sol: float
    gross_pnl_sol: float
    net_pnl_sol: float
    roi_pct: float
    market_impact_pct: float
    holding_seconds: float
    is_win: bool
    cumulative_equity_sol: float
    drawdown_pct: float


def generate_terminal_equity_chart(
    records: list[TradePerformanceRecord],
    width: int = 50,
    height: int = 10,
) -> str:
    """Generate a clean ASCII equity curve for terminal display."""
    if not records:
        return "No trade records to plot."

    equities = [r.cumulative_equity_sol for r in records]
    min_eq = min(min(equities), 0.0)
    max_eq = max(max(equities), 0.01)
    span = max_eq - min_eq if max_eq != min_eq else 1.0

    lines: list[str] = []
    lines.append(
        f"Equity Curve (SOL) [Peak: {max_eq:+.4f} SOL | Trough: {min_eq:+.4f} SOL]"
    )
    lines.append("-" * (width + 12))

    grid = [[" " for _ in range(len(records))] for _ in range(height)]
    for col, eq in enumerate(equities):
        norm = (eq - min_eq) / span
        row = min(height - 1, max(0, int(norm * (height - 1))))
        grid[height - 1 - row][col] = "#" if eq >= 0 else "x"

    for r_idx in range(height):
        val = max_eq - (r_idx / (height - 1)) * span
        prefix = f"{val:+.4f} |"
        row_str = "".join(grid[r_idx])
        lines.append(f"{prefix} {row_str}")

    lines.append("-" * (width + 12))
    lines.append(
        "Trade # | " + "".join(f"{i % 10}" for i in range(1, len(records) + 1))
    )
    return "\n".join(lines)


def export_vectorbt_html_report(
    target: str,
    mode: str,
    records: list[TradePerformanceRecord],
    total_fees_sol: float,
    market_impact_drag_sol: float,
    output_path: Path | str,
) -> Path:
    """Export an interactive Plotly VectorBT-style performance report in HTML."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if not records:
        out.write_text(
            "<html><body><h2>No trades to display</h2></body></html>", encoding="utf-8"
        )
        return out

    trade_indices = [r.trade_index for r in records]
    equities = [r.cumulative_equity_sol for r in records]
    trade_pnls = [r.net_pnl_sol for r in records]
    drawdowns = [-abs(r.drawdown_pct) for r in records]
    mints = [r.mint for r in records]
    rois = [r.roi_pct for r in records]
    hold_secs = [r.holding_seconds for r in records]

    colors = ["#10b981" if p >= 0 else "#ef4444" for p in trade_pnls]

    total_net_pnl = sum(r.net_pnl_sol for r in records)
    wins = [r for r in records if r.is_win]
    winrate = (len(wins) / len(records) * 100.0) if records else 0.0
    max_dd = max([r.drawdown_pct for r in records], default=0.0)
    gross_wins = sum(r.net_pnl_sol for r in wins)
    gross_losses = abs(sum(r.net_pnl_sol for r in records if not r.is_win))
    profit_factor = (
        (gross_wins / gross_losses)
        if gross_losses > 0
        else (999.0 if gross_wins > 0 else 0.0)
    )

    # 3-Row Plotly Subplots
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=(
            f"<b>Cumulative Equity Curve (SOL)</b> | Net PnL: {total_net_pnl:+.4f} SOL (Winrate: {winrate:.1f}%)",
            "<b>Trade-by-Trade Realized PnL (SOL)</b>",
            f"<b>Underwater Drawdown (%)</b> | Max DD: -{max_dd:.1f}%",
        ),
        row_heights=[0.5, 0.25, 0.25],
    )

    # Row 1: Equity Curve
    hover_texts = [
        f"<b>Trade #{r.trade_index}</b><br>Mint: {r.mint}<br>Net PnL: {r.net_pnl_sol:+.4f} SOL ({r.roi_pct:+.1f}%)<br>Hold: {r.holding_seconds:.1f}s<br>Cumulative: {r.cumulative_equity_sol:+.4f} SOL"
        for r in records
    ]
    fig.add_trace(
        go.Scatter(
            x=trade_indices,
            y=equities,
            mode="lines+markers",
            name="Equity (SOL)",
            line=dict(color="#3b82f6", width=3),
            fill="tozeroy",
            fillcolor="rgba(59, 130, 246, 0.15)",
            marker=dict(size=6, color="#60a5fa"),
            hovertext=hover_texts,
            hoverinfo="text",
        ),
        row=1,
        col=1,
    )

    # Zero line on Equity
    fig.add_hline(y=0, line_dash="dash", line_color="#475569", row=1, col=1)

    # Row 2: Trade PnL Bar Chart
    fig.add_trace(
        go.Bar(
            x=trade_indices,
            y=trade_pnls,
            name="Trade PnL",
            marker=dict(color=colors),
            hovertext=[
                f"<b>Trade #{i}</b> ({m[:6]}..)<br>PnL: {p:+.4f} SOL ({roi:+.1f}%)<br>Hold: {h:.1f}s"
                for i, m, p, roi, h in zip(
                    trade_indices, mints, trade_pnls, rois, hold_secs, strict=False
                )
            ],
            hoverinfo="text",
        ),
        row=2,
        col=1,
    )

    # Row 3: Underwater Drawdown
    fig.add_trace(
        go.Scatter(
            x=trade_indices,
            y=drawdowns,
            mode="lines",
            name="Drawdown (%)",
            line=dict(color="#ef4444", width=2),
            fill="tozeroy",
            fillcolor="rgba(239, 68, 68, 0.2)",
            hovertext=[f"Drawdown: {dd:.1f}%" for dd in drawdowns],
            hoverinfo="text",
        ),
        row=3,
        col=1,
    )

    # Dark Professional Theme Layout
    fig.update_layout(
        template="plotly_dark",
        title=dict(
            text=f"<b>VectorBT Quantitative Backtest: {target}</b><br><span style='font-size: 13px; color: #94a3b8;'>Mode: {mode.upper()} | Samples: {len(records)} | Profit Factor: {profit_factor:.2f} | Fees: -{total_fees_sol:.4f} SOL | Impact Drag: -{market_impact_drag_sol:.4f} SOL</span>",
            x=0.02,
            y=0.98,
        ),
        paper_bgcolor="#090d16",
        plot_bgcolor="#131b2e",
        showlegend=False,
        height=850,
        margin=dict(l=60, r=40, t=100, b=60),
    )

    fig.update_xaxes(
        title_text="Trade Number (#)",
        gridcolor="#1e293b",
        row=3,
        col=1,
    )
    fig.update_yaxes(title_text="Equity (SOL)", gridcolor="#1e293b", row=1, col=1)
    fig.update_yaxes(title_text="PnL (SOL)", gridcolor="#1e293b", row=2, col=1)
    fig.update_yaxes(title_text="Drawdown (%)", gridcolor="#1e293b", row=3, col=1)

    fig.write_html(str(out), include_plotlyjs=True)
    return out


def generate_terminal_candlestick_chart(
    candles: list[OHLCCandle],
    width: int = 50,
    height: int = 12,
) -> str:
    """Generate a clean ASCII OHLC candlestick chart for terminal display."""
    if not candles:
        return "No OHLC candles to plot."

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    min_p = min(lows)
    max_p = max(highs)
    span = max_p - min_p if max_p != min_p else (max_p if max_p > 0 else 1.0)

    lines: list[str] = []
    lines.append(
        f"OHLC Candlestick Chart (SOL) [High: {max_p:.10f} | Low: {min_p:.10f}]"
    )
    lines.append("-" * (width + 18))

    sampled = candles[-width:] if len(candles) > width else candles
    grid = [[" " for _ in range(len(sampled))] for _ in range(height)]

    for col, c in enumerate(sampled):
        h_row = min(height - 1, max(0, int(((c.high - min_p) / span) * (height - 1))))
        l_row = min(height - 1, max(0, int(((c.low - min_p) / span) * (height - 1))))
        o_row = min(height - 1, max(0, int(((c.open - min_p) / span) * (height - 1))))
        c_row = min(height - 1, max(0, int(((c.close - min_p) / span) * (height - 1))))

        for r in range(l_row, h_row + 1):
            grid[height - 1 - r][col] = "|"
        body_min, body_max = min(o_row, c_row), max(o_row, c_row)
        char = "+" if c.close >= c.open else "-"
        for r in range(body_min, body_max + 1):
            grid[height - 1 - r][col] = char

    for r_idx in range(height):
        val = max_p - (r_idx / (height - 1)) * span
        prefix = f"{val:.10f} |"
        row_str = "".join(grid[r_idx])
        lines.append(f"{prefix} {row_str}")

    lines.append("-" * (width + 18))
    lines.append(
        "Candle #  | " + "".join(f"{i % 10}" for i in range(1, len(sampled) + 1))
    )
    return "\n".join(lines)


def export_vectorbt_ohlc_report(
    target: str,
    mint: str,
    candles: list[OHLCCandle],
    records: list[TradePerformanceRecord],
    total_fees_sol: float,
    output_path: Path | str,
) -> Path:
    """Export an interactive Plotly Candlestick OHLC report with overlaid Buy/Sell trade markers."""
    import datetime

    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if not candles:
        out.write_text(
            "<html><body><h2>No OHLC data to display</h2></body></html>",
            encoding="utf-8",
        )
        return out

    dates = [
        datetime.datetime.fromtimestamp(c.timestamp, tz=datetime.UTC).strftime(
            "%H:%M:%S"
        )
        for c in candles
    ]
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    volumes = [c.volume for c in candles]

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.06,
        subplot_titles=(
            f"<b>{mint[:8]}... OHLC Candlesticks & Executed Trades</b>",
            "<b>Volume (SOL)</b>",
            "<b>Cumulative Realized PnL & Equity (SOL)</b>",
        ),
        row_heights=[0.55, 0.2, 0.25],
    )

    # 1. Candlestick trace
    fig.add_trace(
        go.Candlestick(
            x=dates,
            open=opens,
            high=highs,
            low=lows,
            close=closes,
            name="OHLC Price",
            increasing_line_color="#10b981",
            decreasing_line_color="#ef4444",
        ),
        row=1,
        col=1,
    )

    # 2. Overlaid Trade Markers
    buy_times = []
    buy_prices = []
    buy_texts = []
    sell_times = []
    sell_prices = []
    sell_texts = []

    for r in records:
        buy_times.append(dates[0] if dates else "")
        buy_prices.append(opens[0] if opens else 0.0)
        buy_texts.append(f"<b>BUY</b>: {r.entry_sol:.4f} SOL")

        sell_times.append(dates[-1] if dates else "")
        sell_prices.append(closes[-1] if closes else 0.0)
        pnl_sign = "+" if r.net_pnl_sol >= 0 else ""
        sell_texts.append(
            f"<b>SELL</b>: {r.exit_sol:.4f} SOL<br>Net PnL: {pnl_sign}{r.net_pnl_sol:.4f} SOL ({pnl_sign}{r.roi_pct:.2f}%)"
        )

    if buy_times:
        fig.add_trace(
            go.Scatter(
                x=buy_times,
                y=buy_prices,
                mode="markers",
                name="Buy Entries",
                marker=dict(
                    symbol="triangle-up",
                    size=14,
                    color="#22c55e",
                    line=dict(width=1, color="#ffffff"),
                ),
                hovertext=buy_texts,
                hoverinfo="text",
            ),
            row=1,
            col=1,
        )
    if sell_times:
        fig.add_trace(
            go.Scatter(
                x=sell_times,
                y=sell_prices,
                mode="markers",
                name="Sell Exits",
                marker=dict(
                    symbol="triangle-down",
                    size=14,
                    color="#ef4444",
                    line=dict(width=1, color="#ffffff"),
                ),
                hovertext=sell_texts,
                hoverinfo="text",
            ),
            row=1,
            col=1,
        )

    # 3. Volume bars
    vol_colors = [
        "#10b981" if c >= o else "#ef4444" for o, c in zip(opens, closes, strict=False)
    ]
    fig.add_trace(
        go.Bar(
            x=dates,
            y=volumes,
            name="Volume",
            marker=dict(color=vol_colors),
        ),
        row=2,
        col=1,
    )

    # 4. Cumulative Equity
    equities = [r.cumulative_equity_sol for r in records] if records else [0.0]
    eq_x = [f"T#{r.trade_index}" for r in records] if records else ["T#0"]
    fig.add_trace(
        go.Scatter(
            x=eq_x,
            y=equities,
            mode="lines+markers",
            name="Cumulative PnL (SOL)",
            line=dict(color="#3b82f6", width=2),
            fill="tozeroy",
            fillcolor="rgba(59, 130, 246, 0.15)",
        ),
        row=3,
        col=1,
    )

    fig.update_layout(
        template="plotly_dark",
        title=dict(
            text=f"<b>VectorBT OHLC Candlestick Report: {mint}</b><br><span style='font-size: 13px; color: #94a3b8;'>Candles: {len(candles)} | Trades: {len(records)} | Total Fees: -{total_fees_sol:.4f} SOL</span>",
            x=0.02,
            y=0.98,
        ),
        paper_bgcolor="#090d16",
        plot_bgcolor="#131b2e",
        showlegend=True,
        height=900,
        margin=dict(l=60, r=40, t=100, b=60),
        xaxis_rangeslider_visible=False,
    )

    fig.write_html(str(out), include_plotlyjs=True)
    return out
