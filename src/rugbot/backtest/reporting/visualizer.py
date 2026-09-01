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
            "%Y-%m-%d %H:%M:%S"
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
            f"<b>{mint[:8]}... 1s OHLC Candlesticks & Executed Trades</b>",
            "<b>Volume (SOL)</b>",
            "<b>Cumulative Realized PnL & Equity (SOL)</b>",
        ),
        row_heights=[0.55, 0.2, 0.25],
    )

    # 1. Candlestick trace (1-second high-resolution candles)
    fig.add_trace(
        go.Candlestick(
            x=dates,
            open=opens,
            high=highs,
            low=lows,
            close=closes,
            name="1s OHLC Price",
            increasing_line_color="#22c55e",
            increasing_fillcolor="#22c55e",
            decreasing_line_color="#ef4444",
            decreasing_fillcolor="#ef4444",
            whiskerwidth=0.8,
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
                    size=15,
                    color="#22c55e",
                    line=dict(width=1.5, color="#ffffff"),
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
                    size=15,
                    color="#ef4444",
                    line=dict(width=1.5, color="#ffffff"),
                ),
                hovertext=sell_texts,
                hoverinfo="text",
            ),
            row=1,
            col=1,
        )

    # 3. Volume bars
    vol_colors = [
        "#22c55e" if c >= o else "#ef4444" for o, c in zip(opens, closes, strict=False)
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
    eq_x = (
        dates[: len(records)]
        if len(dates) >= len(records)
        else [f"T#{r.trade_index}" for r in records]
    )
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
            text=f"<b>1s OHLC Candlestick Report: {mint}</b><br><span style='font-size: 13px; color: #94a3b8;'>1s Candles: {len(candles)} | Trades: {len(records)} | Total Fees: -{total_fees_sol:.4f} SOL</span>",
            x=0.02,
            y=0.98,
        ),
        paper_bgcolor="#090d16",
        plot_bgcolor="#131b2e",
        showlegend=True,
        height=950,
        margin=dict(l=60, r=40, t=100, b=60),
    )
    # Category type prevents empty time voids stretching across non-trading periods
    fig.update_xaxes(type="category", gridcolor="#1e293b", row=1, col=1)
    fig.update_xaxes(type="category", gridcolor="#1e293b", row=2, col=1)
    fig.update_xaxes(type="category", gridcolor="#1e293b", row=3, col=1)
    fig.update_yaxes(title_text="Price (SOL)", gridcolor="#1e293b", row=1, col=1)
    fig.update_yaxes(title_text="Vol (SOL)", gridcolor="#1e293b", row=2, col=1)
    fig.update_yaxes(title_text="PnL (SOL)", gridcolor="#1e293b", row=3, col=1)

    fig.write_html(str(out), include_plotlyjs=True)

    # Also export TradingView Lightweight Charts HTML alongside
    tv_path = out.with_name(out.stem + "_tradingview.html")
    export_tradingview_html_report(mint, candles, records, total_fees_sol, tv_path)

    # Also export mplfinance high-res PNG image
    png_path = out.with_suffix(".png")
    export_mplfinance_png_chart(mint, candles, records, png_path)

    return out


def export_mplfinance_png_chart(
    mint: str,
    candles: list[OHLCCandle],
    records: list[TradePerformanceRecord],
    output_path: Path | str,
) -> Path | None:
    """Export a high-resolution PNG financial chart with human-readable Market Cap in USD ($k)."""
    import datetime

    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import pandas as pd

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if not candles:
        return None

    try:
        # Approximate SOL/USD rate for clean market cap scaling
        sol_usd = 145.0
        total_supply = 1_000_000_000.0

        dates = [
            datetime.datetime.fromtimestamp(c.timestamp, tz=datetime.UTC)
            for c in candles
        ]
        mcaps_k = [
            (c.close * total_supply * sol_usd) / 1000.0 for c in candles
        ]
        highs_k = [(c.high * total_supply * sol_usd) / 1000.0 for c in candles]
        lows_k = [(c.low * total_supply * sol_usd) / 1000.0 for c in candles]
        opens_k = [(c.open * total_supply * sol_usd) / 1000.0 for c in candles]
        volumes = [c.volume for c in candles]

        df = pd.DataFrame(
            {
                "date": dates,
                "open": opens_k,
                "high": highs_k,
                "low": lows_k,
                "close": mcaps_k,
                "volume": volumes,
            }
        )
        df.sort_values("date", inplace=True)

        plt.style.use("dark_background")
        fig, (ax1, ax2) = plt.subplots(
            2,
            1,
            figsize=(12, 7.5),
            gridspec_kw={"height_ratios": [3.2, 1]},
            sharex=True,
        )
        fig.patch.set_facecolor("#090d16")
        ax1.set_facecolor("#0d1322")
        ax2.set_facecolor("#0d1322")

        # 1. Plot Main Price / Market Cap Curve
        ax1.plot(
            df["date"],
            df["close"],
            color="#22c55e",
            linewidth=2.2,
            label="Market Cap ($k USD)",
            zorder=3,
        )
        ax1.fill_between(
            df["date"], df["close"], color="#22c55e", alpha=0.12, zorder=2
        )

        # Draw Candlestick wicks and bodies on top of line for key moves
        for _, row in df.iterrows():
            d = row["date"]
            o = row["open"]
            c = row["close"]
            h = row["high"]
            l_val = row["low"]
            col = "#22c55e" if c >= o else "#ef4444"
            ax1.vlines(d, l_val, h, color=col, linewidth=1.0, alpha=0.7, zorder=3)

        # 2. Highlight ATH Peak
        peak_idx = df["high"].idxmax()
        peak_row = df.loc[peak_idx]
        peak_val = peak_row["high"]
        ax1.scatter(
            [peak_row["date"]],
            [peak_val],
            color="#fbbf24",
            s=90,
            zorder=5,
            edgecolor="#ffffff",
            linewidth=1.5,
        )
        ax1.annotate(
            f"ATH Peak: ${peak_val:.1f}k",
            xy=(peak_row["date"], peak_val),
            xytext=(0, 14),
            textcoords="offset points",
            ha="center",
            color="#fbbf24",
            fontsize=11,
            fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.25", fc="#1e293b", ec="#fbbf24", lw=1),
        )

        # 3. Detect and Annotate Big Red Dump / Rug
        last_row = df.iloc[-1]
        floor_val = last_row["close"]
        if peak_val > floor_val * 2.5:
            ax1.annotate(
                f"[RUG DUMP] ➜ ${floor_val:.1f}k Floor",
                xy=(last_row["date"], floor_val),
                xytext=(-120, 25),
                textcoords="offset points",
                color="#ef4444",
                fontsize=11,
                fontweight="bold",
                bbox=dict(
                    boxstyle="round,pad=0.25", fc="#1e293b", ec="#ef4444", lw=1.2
                ),
                arrowprops=dict(arrowstyle="->", color="#ef4444", lw=2.2),
            )

        ax1.set_ylabel("Market Cap ($k USD)", color="#e2e8f0", fontsize=12, fontweight="600")
        ax1.set_title(
            f"Pump.fun On-Chain Financial Chart — {mint[:14]}... (ATH: ${peak_val:.1f}k | Floor: ${floor_val:.1f}k)",
            color="#38bdf8",
            fontsize=13,
            fontweight="bold",
            pad=14,
        )
        ax1.grid(True, linestyle="--", alpha=0.2, color="#334155")
        ax1.legend(loc="upper left", framealpha=0.3)

        # 4. Volume Panel
        vol_colors = [
            "#22c55e" if c >= o else "#ef4444"
            for o, c in zip(df["open"], df["close"], strict=False)
        ]
        ax2.bar(
            df["date"],
            df["volume"],
            color=vol_colors,
            width=0.0003,
            alpha=0.85,
            edgecolor=None,
        )
        ax2.set_ylabel("Volume (SOL)", color="#e2e8f0", fontsize=11, fontweight="600")
        ax2.set_xlabel("Time (UTC)", color="#e2e8f0", fontsize=11, fontweight="600")
        ax2.grid(True, linestyle="--", alpha=0.2, color="#334155")
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))

        plt.tight_layout()
        plt.savefig(str(out), dpi=150, facecolor=fig.get_facecolor(), edgecolor="none")
        plt.close(fig)
        return out
    except Exception:
        return None


def export_tradingview_html_report(
    mint: str,
    candles: list[OHLCCandle],
    records: list[TradePerformanceRecord],
    total_fees_sol: float,
    output_path: Path | str,
) -> Path:
    """Export an interactive TradingView Lightweight Charts HTML application."""
    import json

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if not candles:
        out.write_text(
            "<html><body><h2>No OHLC data</h2></body></html>", encoding="utf-8"
        )
        return out

    candle_data = [
        {
            "time": c.timestamp,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
        }
        for c in candles
    ]
    vol_data = [
        {
            "time": c.timestamp,
            "value": c.volume,
            "color": "#22c55e" if c.close >= c.open else "#ef4444",
        }
        for c in candles
    ]

    markers = []
    for r in records:
        markers.append(
            {
                "time": candles[0].timestamp,
                "position": "belowBar",
                "color": "#22c55e",
                "shape": "arrowUp",
                "text": f"BUY {r.entry_sol:.3f} SOL @ {candles[0].open:.9f}",
            }
        )
        pnl_sign = "+" if r.net_pnl_sol >= 0 else ""
        markers.append(
            {
                "time": candles[-1].timestamp,
                "position": "aboveBar",
                "color": "#ef4444",
                "shape": "arrowDown",
                "text": f"SELL {r.exit_sol:.3f} SOL ({pnl_sign}{r.net_pnl_sol:.4f} SOL / {pnl_sign}{r.roi_pct:.1f}%)",
            }
        )

    cd_json = json.dumps(candle_data)
    vol_json = json.dumps(vol_data)
    mk_json = json.dumps(markers)

    peak_price = max(c.high for c in candles)
    total_vol = sum(c.volume for c in candles)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8" />
    <title>TradingView Chart - {mint}</title>
    <script src="https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"></script>
    <style>
        body {{ margin: 0; padding: 0; background: #090d16; color: #e5e7eb; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
        .header {{ padding: 12px 20px; background: #111827; border-bottom: 1px solid #1f2937; display: flex; justify-content: space-between; align-items: center; }}
        .title {{ font-size: 15px; font-weight: bold; color: #38bdf8; }}
        .stats {{ font-size: 13px; color: #9ca3af; }}
        .badge {{ background: #1e293b; color: #10b981; padding: 3px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; margin-left: 8px; }}
        #chart {{ width: 100vw; height: calc(100vh - 55px); }}
    </style>
</head>
<body>
    <div class="header">
        <div class="title">📈 {mint} <span class="badge">1s LIVE</span></div>
        <div class="stats">Candles: {len(candles)} | Volume: {total_vol:.2f} SOL | Peak: {peak_price:.10f} SOL | Fees: -{total_fees_sol:.4f} SOL</div>
    </div>
    <div id="chart"></div>
    <script>
        const chart = LightweightCharts.createChart(document.getElementById('chart'), {{
            width: window.innerWidth,
            height: window.innerHeight - 55,
            layout: {{ background: {{ color: '#090d16' }}, textColor: '#9ca3af' }},
            grid: {{ vertLines: {{ color: '#131b2e' }}, horzLines: {{ color: '#131b2e' }} }},
            crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
            rightPriceScale: {{ borderColor: '#1f2937' }},
            timeScale: {{ borderColor: '#1f2937', timeVisible: true, secondsVisible: true }},
        }});

        const candlestickSeries = chart.addCandlestickSeries({{
            upColor: '#22c55e',
            downColor: '#ef4444',
            borderVisible: false,
            wickUpColor: '#22c55e',
            wickDownColor: '#ef4444',
        }});
        candlestickSeries.setData({cd_json});

        const volumeSeries = chart.addHistogramSeries({{
            color: '#3b82f6',
            priceFormat: {{ type: 'volume' }},
            priceScaleId: '',
            scaleMargins: {{ top: 0.8, bottom: 0 }},
        }});
        volumeSeries.setData({vol_json});

        candlestickSeries.setMarkers({mk_json});
        chart.timeScale().fitContent();

        window.addEventListener('resize', () => {{
            chart.resize(window.innerWidth, window.innerHeight - 55);
        }});
    </script>
</body>
</html>"""
    out.write_text(html, encoding="utf-8")
    return out
