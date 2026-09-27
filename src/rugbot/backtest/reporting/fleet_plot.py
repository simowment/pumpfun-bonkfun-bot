"""Interactive chart of an operator's launches, as seen from our entry."""

from __future__ import annotations

import statistics
from typing import TYPE_CHECKING

import plotly.graph_objects as go
from plotly.subplots import make_subplots

if TYPE_CHECKING:
    from pathlib import Path

    from rugbot.backtest.launch_replay import LaunchReplay

PATH_WINDOW_S = 600
MEDIAN_STEP_S = 5
LAUNCH_LINE_COLOR = "rgba(82, 81, 78, 0.35)"
MEDIAN_COLOR = "#2a78d6"
REFERENCE_COLOR = "#8a8984"
SURFACE_COLOR = "#fcfcfb"
TEXT_COLOR = "#0b0b0b"
GRID_COLOR = "#ebebe8"


def _multiple_at(path: list[tuple[float, float]], seconds: float) -> float:
    """Last observed multiple at or before ``seconds`` (path is time-ordered)."""
    value = path[0][1]
    for at, multiple in path:
        if at > seconds:
            break
        value = multiple
    return value


def write_fleet_plot(
    replays: list[LaunchReplay], out: Path, *, entry_delay_slots: int
) -> None:
    """Write price-vs-entry paths and per-launch peaks to an HTML file.

    Args:
        replays: Operator launches, each with its entry already fixed.
        out: HTML file to write.
        entry_delay_slots: Entry slot offset, shown in the titles.
    """
    paths = {
        replay.mint: [p for p in replay.path_after_entry() if p[0] <= PATH_WINDOW_S]
        for replay in replays
    }
    fig = make_subplots(
        rows=2,
        cols=1,
        vertical_spacing=0.14,
        subplot_titles=(
            f"Price vs our fill (block +{entry_delay_slots}), first "
            f"{PATH_WINDOW_S // 60} min — grey: each launch, blue: median",
            "Peak multiple reached after our fill, per launch",
        ),
    )
    for replay in replays:
        path = paths[replay.mint]
        fig.add_trace(
            go.Scatter(
                x=[at for at, _ in path],
                y=[multiple for _, multiple in path],
                mode="lines",
                line={"color": LAUNCH_LINE_COLOR, "width": 1},
                name=replay.mint[:8],
                hovertemplate=f"{replay.mint[:8]}  %{{x:.0f}}s  %{{y:.2f}}x"
                "<extra></extra>",
                showlegend=False,
            ),
            row=1,
            col=1,
        )
    grid = list(range(0, PATH_WINDOW_S + 1, MEDIAN_STEP_S))
    median = [
        statistics.median(_multiple_at(path, at) for path in paths.values() if path)
        for at in grid
    ]
    fig.add_trace(
        go.Scatter(
            x=grid,
            y=median,
            mode="lines",
            line={"color": MEDIAN_COLOR, "width": 2},
            hovertemplate="median  %{x:.0f}s  %{y:.2f}x<extra></extra>",
            showlegend=False,
        ),
        row=1,
        col=1,
    )
    fig.add_hline(y=1, line={"color": REFERENCE_COLOR, "dash": "dot"}, row=1, col=1)

    ranked = sorted(replays, key=lambda replay: replay.profile.ath_multiple)
    fig.add_trace(
        go.Bar(
            x=[replay.mint[:8] for replay in ranked],
            y=[replay.profile.ath_multiple for replay in ranked],
            marker={"color": MEDIAN_COLOR},
            customdata=[
                (replay.profile.entry_mc_sol, replay.profile.seconds_to_ath)
                for replay in ranked
            ],
            hovertemplate="%{x}  peak %{y:.2f}x<br>entry MC %{customdata[0]:.1f} SOL"
            "  peak after %{customdata[1]:.0f}s<extra></extra>",
            showlegend=False,
        ),
        row=2,
        col=1,
    )
    for level in (1, 2):
        fig.add_hline(
            y=level, line={"color": REFERENCE_COLOR, "dash": "dot"}, row=2, col=1
        )
    fig.update_xaxes(title_text="seconds after our fill", row=1, col=1)
    fig.update_yaxes(title_text="price / our fill price", type="log", row=1, col=1)
    fig.update_yaxes(title_text="peak multiple", row=2, col=1)
    fig.update_xaxes(tickangle=-45, row=2, col=1)
    fig.update_xaxes(gridcolor=GRID_COLOR)
    fig.update_yaxes(gridcolor=GRID_COLOR)
    fig.update_layout(
        height=950,
        paper_bgcolor=SURFACE_COLOR,
        plot_bgcolor=SURFACE_COLOR,
        font={"color": TEXT_COLOR, "size": 12},
        margin={"l": 60, "r": 20, "t": 60, "b": 100},
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(out), include_plotlyjs=True)
