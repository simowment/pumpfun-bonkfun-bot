"""MetaTrader-style candle charts of replayed launches with our buy and sell."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from rugbot.backtest.launch_replay import describe_exit_rule, market_cap_sol
from rugbot.domain.ohlc import TradeTick, build_ohlc_candles
from rugbot.domain.pump_curve import TOKEN_SUPPLY_UI

if TYPE_CHECKING:
    from rugbot.backtest.launch_replay import ExitRule, LaunchReplay, LaunchTrade

PLOTS_DIR = Path(".state/plots")
# Candle sizes in seconds; the smallest one keeping a launch under
# MAX_CANDLES candles is used.
TIMEFRAMES_S = (1, 5, 15, 30, 60, 300, 900)
MAX_CANDLES = 300
# Chart window: creation until this long after our exit or the peak, if later.
AFTER_LAST_EVENT_S = 120
TRACES_PER_LAUNCH = 6
BACKGROUND = "#0b0b0b"
GRID = "#262626"
TEXT = "#d4d4d4"
UP = "#26a69a"
DOWN = "#ef5350"
BUY = "#3987e5"
SELL = "#ef5350"


def _timeframe(span_s: float) -> int:
    return next(
        (tf for tf in TIMEFRAMES_S if span_s / tf <= MAX_CANDLES), TIMEFRAMES_S[-1]
    )


def _mc_usd(trade: LaunchTrade) -> float:
    if trade.price_usd is None:
        raise ValueError(f"trade at slot {trade.slot} has no USD price")  # noqa: TRY003
    return trade.price_usd * TOKEN_SUPPLY_UI


def _launch_traces(replay: LaunchReplay, rule: ExitRule) -> tuple[list, str]:
    """Candles, volume, entry, exit and trade-line traces plus a chart title."""
    result = replay.run(rule)
    entry, exit_trade = replay.entry, result.exit_trade
    start = replay.trades[0].timestamp_s
    peak_at = entry.timestamp_s + replay.profile.seconds_to_ath
    end = min(
        max(exit_trade.timestamp_s, peak_at) + AFTER_LAST_EVENT_S,
        replay.trades[-1].timestamp_s,
    )
    ticks = [
        TradeTick(
            timestamp=int(trade.timestamp_s),
            price=_mc_usd(trade),
            volume=trade.amount_sol * _mc_usd(trade) / market_cap_sol(trade.price_sol),
            is_buy=trade.is_buy,
            signature="",
        )
        for trade in replay.trades
        if trade.timestamp_s <= end
    ]
    timeframe = _timeframe(end - start)
    candles = build_ohlc_candles(
        ticks, timeframe_seconds=timeframe, max_candles=math.ceil(MAX_CANDLES * 1.5)
    )
    times = [datetime.fromtimestamp(c.timestamp, tz=UTC) for c in candles]
    entry_time = datetime.fromtimestamp(entry.timestamp_s, tz=UTC)
    exit_time = datetime.fromtimestamp(exit_trade.timestamp_s, tz=UTC)
    entry_mc, exit_mc = _mc_usd(entry), _mc_usd(exit_trade)
    won = result.net_pnl_sol > 0
    levels = [
        entry_mc * (1 + sign * pct / 100)
        for pct, sign in ((rule.take_profit_pct, 1), (rule.stop_loss_pct, -1))
        if pct is not None
    ]
    traces = [
        go.Candlestick(
            x=times,
            open=[c.open for c in candles],
            high=[c.high for c in candles],
            low=[c.low for c in candles],
            close=[c.close for c in candles],
            increasing={"line": {"color": UP}, "fillcolor": UP},
            decreasing={"line": {"color": DOWN}, "fillcolor": DOWN},
            name="MC ($)",
        ),
        go.Bar(
            x=times,
            y=[c.volume for c in candles],
            marker={"color": [UP if c.close >= c.open else DOWN for c in candles]},
            name="volume ($)",
        ),
        go.Scatter(
            x=[entry_time],
            y=[entry_mc],
            mode="markers",
            marker={"symbol": "triangle-up", "size": 16, "color": BUY},
            hovertemplate=f"buy {replay.costs.quote_size_sol:g} SOL "
            f"(block +{replay.profile.entry_slot - replay.profile.create_slot})"
            "<br>MC $%{y:,.0f}<extra></extra>",
            name="buy",
        ),
        go.Scatter(
            x=[exit_time],
            y=[exit_mc],
            mode="markers",
            marker={"symbol": "triangle-down", "size": 16, "color": SELL},
            hovertemplate=f"sell ({result.exit_reason}) after {result.held_s:.0f}s"
            f"<br>MC $%{{y:,.0f}}<br>net {result.net_pnl_sol:+.4f} SOL"
            "<extra></extra>",
            name="sell",
        ),
        go.Scatter(
            x=[entry_time, exit_time],
            y=[entry_mc, exit_mc],
            mode="lines",
            line={"color": UP if won else DOWN, "dash": "dash", "width": 1},
            hoverinfo="skip",
            name="trade",
        ),
        go.Scatter(
            x=[x for _ in levels for x in (times[0], times[-1], None)],
            y=[y for level in levels for y in (level, level, None)],
            mode="lines",
            line={"color": "#8a8984", "dash": "dot", "width": 1},
            hovertemplate="TP/SL $%{y:,.0f}<extra></extra>",
            name="TP/SL",
        ),
    ]
    created = datetime.fromtimestamp(start, tz=UTC)
    title = (
        f"{replay.mint}  {created:%m-%d %H:%M} UTC  {timeframe}s  "
        f"buy @ ${entry_mc:,.0f} → sell @ ${exit_mc:,.0f} "
        f"({result.exit_reason}, {result.held_s:.0f}s)  net "
        f"{result.net_pnl_sol:+.4f} SOL  peak {replay.profile.ath_multiple:.2f}x"
    )
    return traces, title


def write_launch_charts(
    replays: list[LaunchReplay],
    rule: ExitRule,
    out: Path,
    *,
    shown_mint: str | None = None,
) -> Path:
    """Write one candle chart per launch, picked from a dropdown, to HTML.

    Args:
        replays: Replayed launches, each with its entry already fixed.
        rule: Exit rule whose sell is marked on every chart.
        out: HTML file to write.
        shown_mint: Launch displayed when the page opens; the first otherwise.

    Returns:
        The written file.
    """
    ordered = sorted(replays, key=lambda replay: replay.profile.create_slot)
    shown = next(
        (i for i, replay in enumerate(ordered) if replay.mint == shown_mint), 0
    )
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=(0.78, 0.22),
        vertical_spacing=0.03,
    )
    titles = []
    for index, replay in enumerate(ordered):
        traces, title = _launch_traces(replay, rule)
        titles.append(title)
        for trace_index, trace in enumerate(traces):
            trace.visible = index == shown
            trace.showlegend = False
            fig.add_trace(trace, row=2 if trace_index == 1 else 1, col=1)
    buttons = [
        {
            "label": f"{datetime.fromtimestamp(replay.profile.created_at_s, tz=UTC):%m-%d %H:%M}"
            f"  {replay.mint[:8]}  {replay.profile.ath_multiple:.2f}x",
            "method": "update",
            "args": [
                {
                    "visible": [
                        slot // TRACES_PER_LAUNCH == index
                        for slot in range(TRACES_PER_LAUNCH * len(ordered))
                    ]
                },
                {"title.text": titles[index]},
            ],
        }
        for index, replay in enumerate(ordered)
    ]
    fig.update_layout(
        title={"text": titles[shown], "x": 0.01, "font": {"size": 13}},
        updatemenus=[
            {
                "buttons": buttons,
                "x": 1.0,
                "xanchor": "right",
                "y": 1.045,
                "yanchor": "top",
                "bgcolor": "#1f1f1f",
                "font": {"color": TEXT},
                "active": shown,
            }
        ],
        annotations=[
            {
                "text": f"exit rule: {describe_exit_rule(rule)}",
                "xref": "paper",
                "yref": "paper",
                "x": 0.01,
                "y": 1.02,
                "showarrow": False,
                "font": {"color": TEXT, "size": 11},
            }
        ],
        height=680,
        paper_bgcolor=BACKGROUND,
        plot_bgcolor=BACKGROUND,
        font={"color": TEXT, "size": 12},
        margin={"l": 60, "r": 20, "t": 90, "b": 40},
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
    )
    fig.update_xaxes(gridcolor=GRID, showspikes=True, spikecolor="#6b6b6b")
    fig.update_yaxes(gridcolor=GRID, side="right")
    fig.update_yaxes(title_text="market cap ($)", tickformat="$,.0f", row=1, col=1)
    fig.update_yaxes(title_text="vol ($)", row=2, col=1)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(out), include_plotlyjs=True)
    return out
