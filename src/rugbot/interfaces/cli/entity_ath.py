"""CLI: profile an entity's launches by market cap (SOL) with a plot."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

from rugbot.decision.lite_profiler import LiteProfileReport, profile_launches
from rugbot.integrations.pumpfun_api import get_client
from rugbot.interfaces.cli.wallet import (
    LITE_PROFILE_FEE_BPS,
    _fetch_candles_with_fallback,
    _launch_window_is_valid,
    _lite_mint_mcap_sol,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class EntityAthSummary:
    """Per-entity ATH market-cap summary in SOL."""

    name: str
    scanned: int
    launches: int
    mcap_scored: int
    entry_mcap_avg_sol: float
    entry_mcap_median_sol: float
    ath_mcap_avg_sol: float
    ath_mcap_median_sol: float
    ath_mcap_max_sol: float
    ath_mcap_min_sol: float
    ath_multiple_avg: float
    ath_multiple_median: float
    ath_multiple_max: float
    qualifies: bool


def parse_entity_specs(
    targets: list[str], entity_args: list[str]
) -> dict[str, list[str]]:
    """Merge bare targets and ``--entity NAME=w1,w2`` groups.

    Args:
        targets: Bare wallet addresses grouped under ``"target"``.
        entity_args: Repeatable ``NAME=w1,w2,w3`` specs.

    Returns:
        Entity name to deduped wallet list, preserving first-seen order.

    Raises:
        ValueError: When an ``--entity`` spec lacks ``NAME=``.
    """
    entities: dict[str, list[str]] = {}
    bare = [w.strip() for w in targets if w and w.strip()]
    if bare:
        seen: set[str] = set()
        ordered: list[str] = []
        for wallet in bare:
            if wallet not in seen:
                seen.add(wallet)
                ordered.append(wallet)
        entities["target"] = ordered
    for spec in entity_args:
        name, sep, wallets_raw = spec.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"Bad --entity spec {spec!r}")  # noqa: TRY003
        name = name.strip()
        wallets = [w.strip() for w in wallets_raw.split(",") if w.strip()]
        existing = entities.setdefault(name, [])
        known = set(existing)
        for wallet in wallets:
            if wallet not in known:
                known.add(wallet)
                existing.append(wallet)
    return entities


def summarize_entity(
    name: str, report: LiteProfileReport, scanned: int
) -> EntityAthSummary:
    """Summarize one entity's lite profile report.

    Args:
        name: Entity group name.
        report: Lite profile report for the entity's launches.
        scanned: Mints collected for the entity before candle filtering.

    Returns:
        Frozen per-entity summary with SOL mcap stats and qualification.
    """
    entries = sorted(
        launch.entry_mcap_sol for launch in report.launches if launch.entry_mcap_sol > 0
    )
    entry_median = float(statistics.median(entries)) if entries else 0.0
    optimal = report.optimal_tp
    return EntityAthSummary(
        name=name,
        scanned=scanned,
        launches=report.launch_count,
        mcap_scored=report.mcap_scored_count,
        entry_mcap_avg_sol=float(report.entry_mcap_sol_avg),
        entry_mcap_median_sol=entry_median,
        ath_mcap_avg_sol=float(report.ath_mcap_sol_avg),
        ath_mcap_median_sol=float(report.ath_mcap_sol_median),
        ath_mcap_max_sol=float(report.ath_mcap_sol_max),
        ath_mcap_min_sol=float(report.ath_mcap_sol_min),
        ath_multiple_avg=float(report.ath_avg),
        ath_multiple_median=float(report.ath_median),
        ath_multiple_max=float(report.ath_max),
        qualifies=bool(optimal is not None and optimal.qualifies),
    )


def _summary_to_json(summary: EntityAthSummary) -> dict[str, object]:
    """Convert a summary to a JSON-serializable dict.

    Args:
        summary: Per-entity summary.

    Returns:
        Plain dict of summary fields.
    """
    return {
        "name": summary.name,
        "scanned": summary.scanned,
        "launches": summary.launches,
        "mcap_scored": summary.mcap_scored,
        "entry_mcap_avg_sol": summary.entry_mcap_avg_sol,
        "entry_mcap_median_sol": summary.entry_mcap_median_sol,
        "ath_mcap_avg_sol": summary.ath_mcap_avg_sol,
        "ath_mcap_median_sol": summary.ath_mcap_median_sol,
        "ath_mcap_max_sol": summary.ath_mcap_max_sol,
        "ath_mcap_min_sol": summary.ath_mcap_min_sol,
        "ath_multiple_avg": summary.ath_multiple_avg,
        "ath_multiple_median": summary.ath_multiple_median,
        "ath_multiple_max": summary.ath_multiple_max,
        "qualifies": summary.qualifies,
    }


class _EntityAthParser(argparse.ArgumentParser):
    """Argument parser that exits 1 (not 2) on argument errors."""

    def error(self, message: str) -> None:
        """Print usage to stderr and exit with status 1.

        Args:
            message: Argparse error message.
        """
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the entity ATH market-cap profiler parser.

    Returns:
        Configured argument parser for ``rug_entity_ath``.
    """
    parser = _EntityAthParser(
        prog="rug_entity_ath",
        description="Profile entity launches by market cap (SOL).",
    )
    parser.add_argument("targets", nargs="*", help="Bare wallet addresses.")
    parser.add_argument(
        "--entity",
        action="append",
        default=[],
        dest="entities",
        help="Repeatable NAME=w1,w2,w3 entity group.",
    )
    parser.add_argument(
        "--per-wallet-cap",
        type=int,
        default=10,
        help="Newest coins taken per wallet (default: 10).",
    )
    parser.add_argument(
        "--max-mints",
        type=int,
        default=24,
        help="Max mints scored per entity (default: 24).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable JSON output.",
    )
    parser.add_argument(
        "--plot",
        type=str,
        default=None,
        help="Write a PNG plot to PATH.",
    )
    return parser


def _collect_entity_mints(
    client: object, wallets: list[str], per_wallet_cap: int, max_mints: int
) -> list[str]:
    """Collect up to per-wallet-cap newest mints per wallet, capped per entity.

    Args:
        client: Pump.fun API client.
        wallets: Entity wallet addresses in order.
        per_wallet_cap: Max newest coins kept per wallet.
        max_mints: Max merged mints kept per entity.

    Returns:
        Deduped mint list in first-seen order, capped at max_mints.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for wallet in wallets:
        try:
            page = client.fetch_user_created_coins(  # type: ignore[union-attr]
                wallet, limit=50, offset=0
            )
        except Exception:  # noqa: BLE001, S112 — per-wallet fail-soft
            continue
        coins = page.get("coins", []) if isinstance(page, dict) else []
        taken = 0
        for coin in coins:
            if taken >= per_wallet_cap:
                break
            if not isinstance(coin, dict):
                continue
            mint = coin.get("mint")
            if not isinstance(mint, str) or not mint:
                continue
            taken += 1
            if mint not in seen:
                seen.add(mint)
                merged.append(mint)
    return merged[:max_mints]


def _profile_entity_mints(
    client: object, mints: list[str], sol_price: float | None
) -> tuple[LiteProfileReport, dict[str, float]]:
    """Fetch candles/mcaps for mints and run the lite profiler.

    Args:
        client: Pump.fun API client.
        mints: Mint addresses to score.
        sol_price: USD per SOL, or None when the quote failed.

    Returns:
        Tuple of the lite profile report and mint to ATH mcap SOL map.
    """
    candles_by_mint: dict[str, list[dict]] = {}
    mcap_sol_by_mint: dict[str, tuple[float, float]] = {}
    ath_by_mint: dict[str, float] = {}
    for mint in mints:
        try:
            token = client.fetch_token(mint)  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 — per-mint fetch is fail-soft
            token = None
        try:
            candles, _interval = _fetch_candles_with_fallback(client, mint)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001, S112 — per-mint fail-soft
            continue
        created_ms = token.get("created_timestamp") if isinstance(token, dict) else None
        if not _launch_window_is_valid(candles, created_ms):
            continue
        candles_by_mint[mint] = candles
        if sol_price is not None:
            pair = _lite_mint_mcap_sol(token, candles, sol_price)
            if pair is not None:
                mcap_sol_by_mint[mint] = pair
                ath_by_mint[mint] = pair[1]
    report = profile_launches(
        candles_by_mint,
        fee_bps=LITE_PROFILE_FEE_BPS,
        mcap_sol_by_mint=mcap_sol_by_mint,
    )
    return report, ath_by_mint


def _write_plot(
    path: str,
    summaries: list[EntityAthSummary],
    ath_by_entity: dict[str, list[float]],
) -> None:
    """Write the 1x2 entity ATH mcap (SOL) PNG plot.

    Args:
        path: Destination PNG path; parent dirs are created.
        summaries: Per-entity summaries in display order.
        ath_by_entity: Entity name to per-token ATH mcap SOL values.
    """
    names = [summary.name for summary in summaries]
    positions = np.arange(len(names))
    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 5))
    fig.suptitle("Entity ATH market cap (SOL) — entry = first-candle close")
    for i, summary in enumerate(summaries):
        values = [v for v in ath_by_entity.get(summary.name, []) if v > 0]
        if values:
            left.scatter([positions[i]] * len(values), values, label=None, zorder=3)
        if summary.ath_mcap_avg_sol > 0:
            left.hlines(
                summary.ath_mcap_avg_sol,
                positions[i] - 0.3,
                positions[i] + 0.3,
            )
        if summary.entry_mcap_avg_sol > 0:
            left.plot(
                [positions[i] - 0.3, positions[i] + 0.3],
                [summary.entry_mcap_avg_sol] * 2,
                linestyle="--",
                marker="",
            )
        left.annotate(
            f"avg {summary.ath_mcap_avg_sol:.1f} SOL\n"
            f"N={summary.mcap_scored}/{summary.scanned}",
            (positions[i], summary.ath_mcap_avg_sol or 1.0),
            textcoords="offset points",
            xytext=(4, 6),
            fontsize=8,
        )
    left.set_xticks(list(positions), names, rotation=20, ha="right")
    left.set_yscale("log")
    left.set_ylabel("ATH mcap (SOL, log)")
    left.set_title("Per-token ATH mcap (dashed = entry avg)")
    width = 0.35
    entry_vals = [s.entry_mcap_avg_sol for s in summaries]
    ath_vals = [s.ath_mcap_avg_sol for s in summaries]
    right.bar(positions - width / 2, entry_vals, width, label="avg entry")
    right.bar(positions + width / 2, ath_vals, width, label="avg ATH")
    for i in range(len(names)):
        right.text(
            positions[i] - width / 2,
            entry_vals[i],
            f"{entry_vals[i]:.1f}",
            ha="center",
            va="bottom",
            fontsize=8,
        )
        right.text(
            positions[i] + width / 2,
            ath_vals[i],
            f"{ath_vals[i]:.1f}",
            ha="center",
            va="bottom",
            fontsize=8,
        )
    right.set_xticks(list(positions), names, rotation=20, ha="right")
    right.set_ylabel("mcap (SOL)")
    right.set_title("Avg entry vs avg ATH mcap")
    right.legend()
    fig.tight_layout()
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out))
    plt.close(fig)


def main(argv: Sequence[str] | None = None) -> int:  # noqa: C901, PLR0911
    """Profile entity launches by market cap (SOL).

    Args:
        argv: Optional CLI args for testing.

    Returns:
        Process exit code (0 normally; 1 on argument error).
    """
    args = build_arg_parser().parse_args(argv)
    if args.per_wallet_cap is None or args.per_wallet_cap < 1:
        print("error: --per-wallet-cap must be >= 1", file=sys.stderr)
        return 1
    if args.max_mints is None or args.max_mints < 1:
        print("error: --max-mints must be >= 1", file=sys.stderr)
        return 1
    try:
        entities = parse_entity_specs(list(args.targets), list(args.entities))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not entities:
        print("error: no targets; pass wallets or --entity NAME=w1,w2", file=sys.stderr)
        return 1
    try:
        client = get_client()
    except Exception as exc:  # noqa: BLE001 — fail-soft, never traceback
        print(f"entity ATH abstained: client init failed ({type(exc).__name__})")
        return 0
    try:
        quote = client.fetch_sol_price()
        raw_price = quote.get("solPrice") if isinstance(quote, dict) else None
        sol_price = (
            float(raw_price)
            if isinstance(raw_price, (int, float)) and raw_price > 0
            else None
        )
    except Exception:  # noqa: BLE001 — SOL quote is fail-soft
        sol_price = None
    summaries: list[EntityAthSummary] = []
    ath_by_entity: dict[str, list[float]] = {}
    payload_entities: list[dict[str, object]] = []
    for name, wallets in entities.items():
        mints = _collect_entity_mints(
            client, wallets, args.per_wallet_cap, args.max_mints
        )
        try:
            report, ath_map = _profile_entity_mints(client, mints, sol_price)
        except Exception:  # noqa: BLE001 — per-entity profiling is fail-soft
            report, ath_map = profile_launches({}, fee_bps=LITE_PROFILE_FEE_BPS), {}
        summary = summarize_entity(name, report, len(mints))
        summaries.append(summary)
        ath_by_entity[name] = [ath_map[mint] for mint in mints if mint in ath_map]
        payload_entities.append(_summary_to_json(summary))
    if args.plot:
        try:
            _write_plot(args.plot, summaries, ath_by_entity)
        except Exception as exc:  # noqa: BLE001 — plot failure never crashes
            print(f"plot failed ({type(exc).__name__}); continuing")
    if args.json:
        print(json.dumps({"entities": payload_entities}, indent=2))
        return 0
    for summary in summaries:
        print(
            f"{summary.name}: scanned={summary.scanned} "
            f"launches={summary.launches} mcap_scored={summary.mcap_scored} "
            f"entry_avg={summary.entry_mcap_avg_sol:.2f} SOL "
            f"ath_avg={summary.ath_mcap_avg_sol:.2f} SOL "
            f"ath_median={summary.ath_mcap_median_sol:.2f} SOL "
            f"ath_max={summary.ath_mcap_max_sol:.2f} SOL "
            f"ath_x_avg={summary.ath_multiple_avg:.2f}x "
            f"qualifies={summary.qualifies}"
        )
    return 0
