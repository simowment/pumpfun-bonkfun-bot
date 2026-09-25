"""Cross-wallet entity profile for one funder (measurement report, no verdicts).

From a funder wallet (or a mint resolved to its funder) prints the funded
burner set and their token timeline with post-bundle ATH outcomes, plus
the facts needed to judge catchability. A CEX-shaped funder is an
explicit dead end: its recipients are unrelated users, so no entity is
built. Reference lines appear as reference values only, never verdicts.
"""

# ruff: noqa: PLR0913, TRY003, C901, PLR0915

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any

from rugbot.integrations.pumpfun_api import get_client
from rugbot.interfaces.cli.creators import live_ath_resolver
from rugbot.interfaces.cli.triage import cadence_seconds
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.tracker.funding_chain import (
    FundedTransfer,
    enumerate_funded_paged,
    is_cex_shaped_source,
)
from rugbot.tracker.funding_edge_rpc import find_outbound_funding_edge
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

WIN_MULTIPLE = 2.0
CEX_FLEET_NOTE = "unattributable (CEX-shaped funder)"
BURNER_MINT_LIMIT = 5


def dedupe_burners(
    transfers: Sequence[FundedTransfer], max_burners: int
) -> list[dict[str, Any]]:
    """Dedupe outbound dispersals to one row per recipient, newest first.

    Args:
        transfers: Newest-first outbound transfers from the funder.
        max_burners: Maximum burner rows to keep.

    Returns:
        Burner dicts with wallet, funding amount and signature, capped.
    """
    seen: set[str] = set()
    burners: list[dict[str, Any]] = []
    for transfer in transfers:
        if transfer.recipient in seen:
            continue
        seen.add(transfer.recipient)
        burners.append(
            {
                "wallet": transfer.recipient,
                "funding_amount_sol": float(transfer.amount_sol),
                "funding_signature": transfer.signature,
            }
        )
        if len(burners) >= max(1, int(max_burners)):
            break
    return burners


def fresh_burners_pct(burners: Sequence[Mapping[str, Any]]) -> float | None:
    """Return the share of burners with zero lifetime creations.

    Args:
        burners: Burner dicts with a ``lifetime_creations`` key (None
            when the creator index was unreachable for that burner).

    Returns:
        Percentage of resolved burners with 0 creations, or None when no
        burner has a resolved count.
    """
    resolved = [b for b in burners if isinstance(b.get("lifetime_creations"), int)]
    if not resolved:
        return None
    fresh = sum(1 for b in resolved if int(b["lifetime_creations"]) == 0)
    return round(fresh / len(resolved) * 100, 2)


def funding_band(amounts: Sequence[float]) -> dict[str, float] | None:
    """Summarize dispersal amounts as a min/median/max band.

    Args:
        amounts: Funding amounts in SOL.

    Returns:
        Band dict, or None when empty.
    """
    if not amounts:
        return None
    return {
        "min_sol": float(min(amounts)),
        "median_sol": float(statistics.median(amounts)),
        "max_sol": float(max(amounts)),
    }


def assemble_timeline(
    entries: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Assemble per-mint timeline rows sorted oldest-first.

    Args:
        entries: Mint dicts with mint, symbol, burner, funding_amount_sol,
            created_ms (or None) and ath_multiple (or None) keys.

    Returns:
        Timeline rows oldest-first; entries without a timestamp sort last.
    """
    rows = [dict(e) for e in entries]
    rows.sort(
        key=lambda r: (
            r.get("created_ms") is None,
            float(r["created_ms"])
            if isinstance(r.get("created_ms"), (int, float))
            else 0.0,
        )
    )
    return rows


def entity_stats(
    timeline: Sequence[Mapping[str, Any]],
    *,
    win_multiple: float,
    active_days: float,
    now_s: float,
) -> dict[str, Any]:
    """Compute entity-level stats over a timeline (measurements only).

    Launches without candles are excluded from the winrate denominator,
    never zeroed.

    Args:
        timeline: Timeline rows with ath_multiple and created_ms keys.
        win_multiple: ATH multiple counting as a win.
        active_days: Active window in days for the ``active`` flag.
        now_s: Current time as Unix seconds.

    Returns:
        Stats dict with launches, scored, wins, winrate, ATH distribution,
        first/last launch, cadence and active flag. No verdict fields.
    """
    ath_values = [
        float(r["ath_multiple"])
        for r in timeline
        if isinstance(r.get("ath_multiple"), (int, float))
    ]
    scored = len(ath_values)
    wins = sum(1 for v in ath_values if v >= win_multiple)
    stamps = [
        float(r["created_ms"]) / 1000.0
        for r in timeline
        if isinstance(r.get("created_ms"), (int, float))
    ]
    last_s = max(stamps) if stamps else None
    return {
        "launches": len(timeline),
        "scored": scored,
        "wins": wins,
        "winrate_pct": round(wins / scored * 100, 2) if scored else None,
        "median_ath_multiple": float(statistics.median(ath_values))
        if ath_values
        else None,
        "max_ath_multiple": float(max(ath_values)) if ath_values else None,
        "first_launch_s": min(stamps) if stamps else None,
        "last_launch_s": last_s,
        "cadence_seconds": cadence_seconds(stamps),
        "active": bool(last_s is not None and now_s - last_s <= active_days * 86400),
        "no_candles": len(timeline) - scored,
    }


def cex_fleet_payload(
    *, funder: str, creation_count: int, recipient_count: int
) -> dict[str, Any]:
    """Build the explicit dead-end payload for a CEX-shaped funder.

    Args:
        funder: The CEX-shaped funder wallet.
        creation_count: Tokens created by the funder itself.
        recipient_count: Distinct wallets the funder paid.

    Returns:
        Payload with the fleet marked unattributable and no entity data.
    """
    return {
        "funder": funder,
        "fleet": CEX_FLEET_NOTE,
        "funder_creations": creation_count,
        "funded_recipients": recipient_count,
        "reference": {
            "win_multiple": WIN_MULTIPLE,
            "note": "thresholds are reference lines only, not pass/fail",
        },
    }


def assemble_payload(
    *,
    funder: str,
    chain: Mapping[str, Any],
    burners: Sequence[Mapping[str, Any]],
    timeline: Sequence[Mapping[str, Any]],
    stats: Mapping[str, Any],
    snipability: Mapping[str, Any],
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble the machine-readable entity payload (no verdicts).

    Args:
        funder: Funder wallet address.
        chain: Input-resolution chain (kind, creator, amount when known).
        burners: Wallet-set rows with funding amounts and creation counts.
        timeline: Oldest-first per-mint rows with ATH outcomes.
        stats: Entity-level stats.
        snipability: Catchability facts (cadence, active, band, fresh share).
        coverage: Coverage counts for the run.

    Returns:
        JSON-safe payload with a reference block and no verdict fields.
    """
    return {
        "funder": funder,
        "chain": dict(chain),
        "burners": [dict(b) for b in burners],
        "timeline": [dict(t) for t in timeline],
        "stats": dict(stats),
        "snipability": dict(snipability),
        "coverage": dict(coverage),
        "reference": {
            "win_multiple": WIN_MULTIPLE,
            "note": "thresholds are reference lines only, not pass/fail",
        },
    }


def _resolve_target(target: str, endpoint: str) -> tuple[str, dict[str, Any]]:
    """Resolve a mint-or-wallet target to its funder plus the chain.

    Args:
        target: Mint address or funder wallet address.
        endpoint: Solana RPC HTTP endpoint for edge resolution.

    Returns:
        Tuple of (funder wallet, chain dict). A mint resolves through its
        creator and outbound funding edge; anything else is treated as a
        funder wallet directly.

    Raises:
        ValueError: When a mint input cannot be resolved to a funder.
    """
    cleaned = target.strip()
    try:
        token = get_client().fetch_token(cleaned)
    except Exception as exc:  # noqa: BLE001 - fail-soft to wallet path
        logger.debug("token lookup failed for %s: %s", cleaned[:8], exc)
        token = {}
    creator = token.get("creator") if isinstance(token, dict) else None
    if isinstance(creator, str) and creator:
        edge = find_outbound_funding_edge(creator, rpc_url=endpoint or None)
        if edge is None:
            raise ValueError(f"no funding edge resolved for creator {creator}")
        funder, amount, signature = edge[0], float(edge[1]), edge[2]
        return funder, {
            "input_kind": "mint",
            "mint": cleaned,
            "creator": creator,
            "funding_amount_sol": amount,
            "funding_signature": signature,
        }
    return cleaned, {"input_kind": "wallet"}


def _burner_creations(wallet: str) -> tuple[int | None, list[dict[str, Any]]]:
    """Fetch one burner's lifetime creations and newest mints, fail-soft.

    Args:
        wallet: Burner wallet address.

    Returns:
        Tuple of (creation count or None; up to ``BURNER_MINT_LIMIT``
        newest mint dicts with mint, symbol and created_ms keys).
    """
    try:
        page = get_client().fetch_user_created_coins(
            wallet, limit=BURNER_MINT_LIMIT, offset=0
        )
    except Exception as exc:  # noqa: BLE001 - fail-soft: unresolved
        logger.debug("creator index failed for %s: %s", wallet[:8], exc)
        return None, []
    if not isinstance(page, dict):
        return None, []
    count = page.get("count")
    coins = page.get("coins")
    mints: list[dict[str, Any]] = []
    if isinstance(coins, list):
        for coin in coins[:BURNER_MINT_LIMIT]:
            if not isinstance(coin, Mapping):
                continue
            mint = coin.get("mint")
            if not isinstance(mint, str) or not mint:
                continue
            created = coin.get("created_timestamp")
            mints.append(
                {
                    "mint": mint,
                    "symbol": coin.get("symbol"),
                    "created_ms": int(created)
                    if isinstance(created, (int, float))
                    else None,
                }
            )
    return (int(count) if isinstance(count, int) else None), mints


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the entity command."""
    parser = argparse.ArgumentParser(
        prog="rug_entity",
        description="Cross-wallet entity profile for one funder.",
    )
    parser.add_argument("target", help="Funder wallet or mint address.")
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--all-pages", action="store_true")
    parser.add_argument("--min-sol", type=float, default=0.2)
    parser.add_argument("--max-sol", type=float, default=5.0)
    parser.add_argument("--max-burners", type=int, default=60)
    parser.add_argument("--active-days", type=float, default=7.0)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the cross-wallet entity profile.

    Args:
        argv: Optional argument vector; defaults to ``sys.argv``.

    Returns:
        Process exit code (0 normally, 1 when resolution fails).
    """
    args = build_parser().parse_args(argv)
    max_pages: int | None = None if bool(args.all_pages) else max(1, int(args.pages))
    max_burners = max(1, int(args.max_burners))
    min_sol = float(args.min_sol)
    max_sol = float(args.max_sol)
    active_days = float(args.active_days)
    now_s = time.time()

    resolve_dotenv()
    providers = load_provider_settings()
    endpoint = providers.rpc_http if providers else ""

    try:
        funder, chain = _resolve_target(str(args.target), endpoint)
    except ValueError as exc:
        print(f"[rug_entity] resolve failed: {exc}", file=sys.stderr)
        return 1

    try:
        transfers = enumerate_funded_paged(
            funder,
            max_pages=max_pages,
            min_sol=min_sol,
            max_sol=max_sol,
        )
    except Exception as exc:  # noqa: BLE001 - fail-soft: empty dispersals
        logger.debug("dispersal enumeration failed for %s: %s", funder[:8], exc)
        transfers = ()

    recipients = {t.recipient for t in transfers if isinstance(t, FundedTransfer)}
    burners = dedupe_burners(
        [t for t in transfers if isinstance(t, FundedTransfer)], max_burners
    )

    try:
        funder_page = get_client().fetch_user_created_coins(funder, limit=1, offset=0)
        creation_count = (
            int(funder_page.get("count", 0))
            if isinstance(funder_page, dict)
            and isinstance(funder_page.get("count"), int)
            else 0
        )
    except Exception:  # noqa: BLE001 - fail-soft: assume unattributed
        logger.debug("funder creation count failed for %s", funder[:8])
        creation_count = 0

    if is_cex_shaped_source(
        source_creation_count=creation_count,
        source_recipient_count=len(recipients),
    ):
        payload = cex_fleet_payload(
            funder=funder,
            creation_count=creation_count,
            recipient_count=len(recipients),
        )
        if bool(args.json):
            print(json.dumps(payload, sort_keys=True))
        else:
            print(f"funder: {funder}")
            print(f"fleet: {CEX_FLEET_NOTE}")
            print(f"funded recipients: {len(recipients)}")
        return 0

    funding_by_burner = {b["wallet"]: b["funding_amount_sol"] for b in burners}
    burner_rows: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    seen_mints: set[str] = set()
    staged_no_launch = 0
    for burner in burners:
        wallet = str(burner["wallet"])
        count, mints = _burner_creations(wallet)
        row = dict(burner)
        row["lifetime_creations"] = count
        burner_rows.append(row)
        if count == 0:
            staged_no_launch += 1
            continue
        for mint_info in mints:
            if mint_info["mint"] in seen_mints:
                continue
            seen_mints.add(mint_info["mint"])
            entries.append(
                {
                    "mint": mint_info["mint"],
                    "symbol": mint_info["symbol"],
                    "burner": wallet,
                    "funding_amount_sol": funding_by_burner.get(wallet),
                    "created_ms": mint_info["created_ms"],
                    "ath_multiple": live_ath_resolver(
                        mint_info["mint"], mint_info["created_ms"]
                    ),
                }
            )

    timeline = assemble_timeline(entries)
    stats = entity_stats(
        timeline,
        win_multiple=WIN_MULTIPLE,
        active_days=active_days,
        now_s=now_s,
    )
    snipability = {
        "cadence_seconds": stats["cadence_seconds"],
        "active": stats["active"],
        "funding_band_sol": funding_band(
            [float(b["funding_amount_sol"]) for b in burner_rows]
        ),
        "fresh_burners_pct": fresh_burners_pct(burner_rows),
    }
    coverage = {
        "pages_fetched": max_pages,
        "recipients": len(recipients),
        "burners_with_launches": sum(
            1
            for b in burner_rows
            if isinstance(b.get("lifetime_creations"), int)
            and int(b["lifetime_creations"]) > 0
        ),
        "staged_no_launch": staged_no_launch,
        "no_candles": stats["no_candles"],
    }
    payload = assemble_payload(
        funder=funder,
        chain=chain,
        burners=burner_rows,
        timeline=timeline,
        stats=stats,
        snipability=snipability,
        coverage=coverage,
    )
    if bool(args.json):
        print(json.dumps(payload, sort_keys=True))
        return 0
    print("=" * 78)
    print(f" RUG ENTITY  {funder}")
    print("=" * 78)
    print(f"burners: {len(burner_rows)} (recipients: {len(recipients)})")
    for item in burner_rows[:10]:
        print(
            f"  burner {item['wallet'][:8]}… funded "
            f"{item['funding_amount_sol']} SOL  "
            f"creations: {item['lifetime_creations']}"
        )
    print(
        f"launches: {stats['launches']} scored: {stats['scored']} "
        f"wins: {stats['wins']} winrate: {stats['winrate_pct']} "
        f"median ATH: {stats['median_ath_multiple']} "
        f"max ATH: {stats['max_ath_multiple']}"
    )
    print(
        f"cadence: {stats['cadence_seconds']}  active: {stats['active']}  "
        f"funding band: {snipability['funding_band_sol']}  "
        f"fresh burners: {snipability['fresh_burners_pct']}%"
    )
    print("reference: win multiple 2.0 is a reference line only, not pass/fail")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
