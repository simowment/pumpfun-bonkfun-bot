"""Candidates query for rug_discover — bible Method 2 bundler extraction."""

# ruff: noqa: BLE001, C901, PLR0912, PLR0913, PLR0915, PLR2004, PLC0415, S110, TRY003, TRY301

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from rugbot.discover.store import ensure_discover_schema
from rugbot.storage.database import DatabaseManager
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)

_DUMPED_MCAP_USD = 5000
_DUMPED_ATH_MULTIPLIER = 1.2


def _parse_since(value: str) -> dt.datetime:
    """Parse --since value like 24h, 7d, 2025-01-01."""

    cleaned = value.strip()
    if not cleaned:
        raise ValueError("--since must not be empty")
    m = re.match(r"^(\d+)([hdm])$", cleaned)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)
        delta = {
            "h": dt.timedelta(hours=amount),
            "d": dt.timedelta(days=amount),
            "m": dt.timedelta(minutes=amount),
        }[unit]
        return dt.datetime.now(dt.UTC) - delta
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(cleaned, fmt).replace(tzinfo=dt.UTC)
        except ValueError:
            continue
    try:
        iso = cleaned.replace("Z", "")
        parsed = dt.datetime.fromisoformat(iso)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)
    except ValueError as exc:
        raise ValueError(f"invalid --since value: {cleaned}") from exc


def _parse_created_at(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        iso = value.replace("Z", "")
        parsed = dt.datetime.fromisoformat(iso)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)
    except Exception:
        return None


def _is_dumped(row: dict[str, Any]) -> bool:
    """Heuristic: dumped if mcap <5000 or ath_multiplier <=1.2 or explicit dump slot."""
    # explicit dump tracking
    if row.get("dump_slot") is not None:
        return True
    # if no mcap/ath data, treat as dumped to keep window usable (fail-open for missing metrics)
    mc = row.get("mc_1s_lamports")
    ath = row.get("ath_quote_lamports")
    vol = row.get("volume_lamports")
    # try raw_json mcap if present
    if mc is None and ath is None:
        # check raw/bundle for hints; missing -> considered dumped
        return True
    # mcap check (lamports -> SOL -> USD rough: 1 SOL ~ 150 USD, so 5000 USD ~ 33 SOL)
    # mc_1s_lamports is in lamports; 15k USD ~ 100 SOL = 100e9 lamports unrealistic;
    # actual stored is lamports, but spec says MC 15k (USD). Keep direct comparison on lamports
    # if value > 1e12 it's lamports, else USD. Handle both.
    if ath is not None and mc is not None:
        try:
            if float(ath) > 0 and float(mc) > 0:
                mult = float(ath) / float(mc) if float(mc) != 0 else 999
                if mult <= _DUMPED_ATH_MULTIPLIER:
                    return True
        except Exception:
            pass
    # inactive / no vol also signals dumped
    if vol is not None:
        try:
            if int(vol) == 0:
                return True
        except Exception:
            pass
    return True


def query_candidates(
    *,
    state_dir: Path = Path(".state/discover"),
    since: str = "24h",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Legacy: query launches with finalized creation times inside window."""

    if limit <= 0 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    since_dt = _parse_since(since)
    since_iso = since_dt.astimezone(dt.UTC).isoformat()
    db = DatabaseManager(state_dir / "rugbot.db")
    ensure_discover_schema(db)
    conn = db.connection
    rows = conn.execute(
        """
        SELECT * FROM discover_launches
        WHERE created_at >= ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (since_iso, limit),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            crow = conn.execute(
                "SELECT winrate, best_tp FROM discover_candidates WHERE wallet = ?",
                (d["creator"],),
            ).fetchone()
            if crow is not None:
                d["winrate"] = crow["winrate"]
                d["best_tp"] = crow["best_tp"]
            else:
                d["winrate"] = None
                d["best_tp"] = None
        except Exception:
            d["winrate"] = None
            d["best_tp"] = None
        result.append(d)
    return result


_OFFSET_TO_POSITION = {1: "B0", 2: "B1", 3: "B2", 4: "B3"}


def _get_creation_tx_index(launch: dict[str, Any]) -> int | None:
    """Extract creation_transaction_index fail-closed.

    Priority: creation_transaction_index column, then raw_json result.transactionIndex.
    Returns None if missing (fail-closed).
    """

    val = launch.get("creation_transaction_index")
    if val is not None:
        try:
            return int(val)
        except Exception:
            pass
    raw = launch.get("raw_json")
    if raw:
        try:
            j = json.loads(str(raw)) if isinstance(raw, str) else raw
            if isinstance(j, dict):
                result = j.get("result") if isinstance(j.get("result"), dict) else j
                if (
                    isinstance(result, dict)
                    and result.get("transactionIndex") is not None
                ):
                    return int(result["transactionIndex"])
        except Exception:
            pass
    return None


def query_bundler_candidates(
    *,
    state_dir: Path = Path(".state/discover"),
    age_min: int = 50,
    age_max: int = 70,
    dumped: bool = True,
    mc_le: int | None = 15000,
    vol_min: int | None = 20000,
    vol_max: int | None = 30000,
    since: str | None = None,
    limit: int = 50,
    max_offset: int = 4,
    max_creations: int | None = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, int]:
    """Bible Method 2: extract bundler wallets for launches in age window.

    Bundler criterion (bible): same slot as creation AND tx_index offset 1..max_offset
    (B0=+1, B1=+2, B2=+3, B3=+4). Fail-closed if creation_transaction_index or
    transaction_index is null — excluded, no guess.

    Returns (bundlers, filtered_launches, fail_message).
    fail_message is non-None when 0 launches in window (fail-closed).
    """

    if limit <= 0 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    if age_min < 0 or age_max < 0 or age_min > age_max:
        raise ValueError("age-min/age-max must satisfy 0 <= min <= max")
    if max_creations is not None and not (1 <= max_creations <= 100):
        raise ValueError("max-creations must be between 1 and 100")

    db = DatabaseManager(state_dir / "rugbot.db")
    ensure_discover_schema(db)
    conn = db.connection

    now = dt.datetime.now(dt.UTC)
    # fetch all launches ordered desc; filter in python for age + mc/vol + dumped
    # use since as fallback broad filter to limit scan
    since_iso: str | None = None
    if since is not None:
        since_dt = _parse_since(since)
        since_iso = since_dt.astimezone(dt.UTC).isoformat()
    else:
        # default broad since to cover age_max plus buffer
        since_iso = (now - dt.timedelta(minutes=age_max + 1440)).isoformat()

    rows = conn.execute(
        "SELECT * FROM discover_launches WHERE created_at IS NOT NULL AND created_at >= ? ORDER BY created_at DESC",
        (since_iso,),
    ).fetchall()
    launches = [dict(r) for r in rows]

    filtered: list[dict[str, Any]] = []
    for launch in launches:
        created = _parse_created_at(str(launch.get("created_at") or ""))
        if created is None:
            continue
        age_min_val = (now - created).total_seconds() / 60
        if not (age_min <= age_min_val <= age_max):
            continue
        if dumped and not _is_dumped(launch):
            continue
        # Axiom-style filters: MC 1s <=15k, vol 20-30k, <=10 creations
        # mc_le / vol filters only apply when value is present
        mc = launch.get("mc_1s_lamports")
        vol = launch.get("volume_lamports")
        if mc_le is not None and mc is not None:
            try:
                # mc stored in lamports: convert lamports->USD approx (1 SOL=150 USD)
                # For filtering, if lamports value is huge (>1e9) treat as lamports->SOL->USD
                mc_val = int(mc)
                # normalize: if >1_000_000 then assume lamports, else assume USD cents
                if mc_val > 10_000_000:
                    mc_usd = (mc_val / 1_000_000_000) * 150
                else:
                    mc_usd = float(mc_val)
                if mc_usd > mc_le:
                    continue
            except Exception:
                pass
        if vol_min is not None and vol is not None:
            try:
                vol_val = int(vol)
                if vol_val > 10_000_000:
                    vol_usd = (vol_val / 1_000_000_000) * 150
                else:
                    vol_usd = float(vol_val)
                if vol_usd < vol_min:
                    continue
            except Exception:
                pass
        if vol_max is not None and vol is not None:
            try:
                vol_val = int(vol)
                if vol_val > 10_000_000:
                    vol_usd = (vol_val / 1_000_000_000) * 150
                else:
                    vol_usd = float(vol_val)
                if vol_usd > vol_max:
                    continue
            except Exception:
                pass
        filtered.append(launch)

    if not filtered:
        msg = "Aucun token créé il y a ~1h et dumpé dans collect — lance collect plus longtemps"
        return [], [], msg, 0

    # Group bundlers
    bundler_map: dict[str, dict[str, Any]] = {}
    # cache sell counts per wallet from discover_trades side=sell
    sell_counts: dict[str, int] = {}
    try:
        for r in conn.execute(
            "SELECT wallet, COUNT(*) as c FROM discover_trades WHERE side='sell' GROUP BY wallet"
        ):
            if r["wallet"]:
                sell_counts[str(r["wallet"])] = int(r["c"])
    except Exception:
        pass

    for launch in filtered:
        mint = str(launch["mint"])
        creator = str(launch["creator"])
        created_slot = (
            int(launch["created_slot"])
            if launch.get("created_slot") is not None
            else None
        )
        created_at = str(launch.get("created_at") or "")
        creation_tx_index = _get_creation_tx_index(launch)
        # fail-closed: if creation tx_index missing, no bundler for this mint
        if created_slot is None or creation_tx_index is None:
            continue

        # strict bundler criterion: same slot AND 1 <= offset <= max_offset
        bundler_trades: list[dict[str, Any]] = []
        try:
            q = conn.execute(
                "SELECT * FROM discover_trades WHERE mint=? AND slot=? AND side='buy' AND wallet IS NOT NULL AND wallet != ? AND tx_index IS NOT NULL",
                (mint, created_slot, creator),
            ).fetchall()
            for tr in q:
                try:
                    tx_idx = int(tr["tx_index"]) if tr["tx_index"] is not None else None
                except Exception:
                    tx_idx = None
                if tx_idx is None:
                    continue
                offset = tx_idx - creation_tx_index
                if 1 <= offset <= max_offset:
                    d = dict(tr)
                    d["_offset"] = offset
                    d["_position"] = _OFFSET_TO_POSITION.get(offset, f"B{offset - 1}")
                    bundler_trades.append(d)
        except Exception:
            pass

        for tr in bundler_trades:
            wallet = str(tr.get("wallet") or "")
            if not wallet:
                continue
            offset = int(tr.get("_offset") or 0)
            position = str(tr.get("_position") or "")
            entry = bundler_map.get(wallet)
            if entry is None:
                entry = {
                    "wallet": wallet,
                    "mints": [],
                    "creators": set(),
                    "amounts": [],
                    "last_seen": created_at,
                    "created_slots": [],
                    "offsets": [],
                    "positions": [],
                }
                bundler_map[wallet] = entry
            entry["mints"].append(mint)
            entry["creators"].add(creator)
            try:
                amt = int(tr.get("quote_amount_base_units") or 0)
            except Exception:
                amt = 0
            entry["amounts"].append(amt)
            entry["created_slots"].append(created_slot)
            entry["offsets"].append(offset)
            entry["positions"].append(position)
            entry["last_seen"] = max(entry["last_seen"], created_at)

    bundlers: list[dict[str, Any]] = []
    for wallet, entry in bundler_map.items():
        mints: list[str] = entry["mints"]
        amounts: list[int] = entry["amounts"]
        creators = entry["creators"]
        offsets: list[int] = entry.get("offsets", [])
        positions: list[str] = entry.get("positions", [])
        mints_count = len(mints)
        total_sol = sum(amounts) / 1_000_000_000 if amounts else 0
        total_lamports = sum(amounts)
        sells = sell_counts.get(wallet, 0)
        avg_offset = round(sum(offsets) / len(offsets), 2) if offsets else None
        # unique sorted bundle positions
        bundle_positions = sorted(
            set(positions), key=lambda x: int(x[1:]) if x[1:].isdigit() else 99
        )
        bundlers.append(
            {
                "wallet": wallet,
                "mints_count": mints_count,
                "total_sol": round(total_sol, 4),
                "total_lamports": total_lamports,
                "cross_entity": len(creators),
                "creators": sorted(creators)[:5],
                "sells": sells,
                "last_seen": entry["last_seen"],
                "mints": mints[:10],
                "offsets": offsets,
                "avg_offset": avg_offset,
                "bundle_positions": bundle_positions,
            }
        )

    bundlers.sort(key=lambda x: (-int(x["mints_count"]), -int(x["total_lamports"])))

    # Bible §1 max creations per dev: filter spam bundlers (same wallet on >N distinct mints)
    spam_bundlers_excluded = 0
    if max_creations is not None:
        before = len(bundlers)
        bundlers = [b for b in bundlers if int(b["mints_count"]) <= max_creations]
        spam_bundlers_excluded = before - len(bundlers)

    return bundlers[:limit], filtered[:limit], None, spam_bundlers_excluded


def query_creator_candidates(
    *,
    state_dir: Path = Path(".state/discover"),
    age_min: int = 50,
    age_max: int = 70,
    dumped: bool = True,
    mc_le: int | None = 15000,
    vol_min: int | None = 20000,
    vol_max: int | None = 30000,
    since: str | None = None,
    limit: int = 50,
    max_creations: int | None = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None, int]:
    """Creator mode: group by creator in age window, bible max creations filter."""

    if max_creations is not None and not (1 <= max_creations <= 100):
        raise ValueError("max-creations must be between 1 and 100")

    _bundlers, filtered, msg, _spam_bundlers = query_bundler_candidates(
        state_dir=state_dir,
        age_min=age_min,
        age_max=age_max,
        dumped=dumped,
        mc_le=mc_le,
        vol_min=vol_min,
        vol_max=vol_max,
        since=since,
        limit=500,
        max_creations=None,
    )
    if msg is not None:
        return [], [], msg, 0
    # group by creator
    from collections import Counter

    counter = Counter(str(launch["creator"]) for launch in filtered)
    # bible filter: exclude creators with > max_creations mints in window
    spam_creators_excluded = 0
    if max_creations is not None:
        before = len(counter)
        counter = Counter({k: v for k, v in counter.items() if v <= max_creations})
        spam_creators_excluded = before - len(counter)
    rows: list[dict[str, Any]] = []
    for creator, cnt in counter.most_common(limit):
        # total sol approx
        total_lamports = sum(
            int(launch.get("volume_lamports") or 0)
            for launch in filtered
            if str(launch["creator"]) == creator
        )
        last_seen = max(
            str(launch.get("created_at") or "")
            for launch in filtered
            if str(launch["creator"]) == creator
        )
        rows.append(
            {
                "wallet": creator,
                "mints_count": cnt,
                "total_sol": round(total_lamports / 1_000_000_000, 4),
                "total_lamports": total_lamports,
                "cross_entity": 1,
                "sells": 0,
                "last_seen": last_seen,
            }
        )
    return rows, filtered, None, spam_creators_excluded


def load_dossier(
    wallet: str,
    *,
    state_dir: Path = Path(".state/discover"),
) -> dict[str, Any]:
    """Assemble dossier JSON for a wallet."""

    import base58

    cleaned = wallet.strip()
    if not cleaned:
        raise ValueError("wallet must not be empty")
    try:
        decoded = base58.b58decode(cleaned)
        if len(decoded) != 32 or base58.b58encode(decoded).decode("ascii") != cleaned:
            raise ValueError("wallet must be Solana pubkey")
    except ValueError as exc:
        raise ValueError(f"wallet must be Solana pubkey: {cleaned}") from exc

    db = DatabaseManager(state_dir / "rugbot.db")
    ensure_discover_schema(db)
    conn = db.connection

    dossier_row = conn.execute(
        "SELECT * FROM discover_dossier WHERE wallet = ?", (cleaned,)
    ).fetchone()
    candidate_row = conn.execute(
        "SELECT * FROM discover_candidates WHERE wallet = ?", (cleaned,)
    ).fetchone()
    launches = conn.execute(
        "SELECT * FROM discover_launches WHERE creator = ? ORDER BY created_at DESC",
        (cleaned,),
    ).fetchall()
    trades = conn.execute(
        "SELECT t.* FROM discover_trades t JOIN discover_launches l ON l.mint=t.mint WHERE l.creator=? ORDER BY t.slot ASC",
        (cleaned,),
    ).fetchall()
    wallet_launch_participation = conn.execute(
        "SELECT p.* FROM discover_wallet_launch_participation p "
        "JOIN discover_entity_mints e ON e.mint = p.mint "
        "WHERE e.creator = ? ORDER BY p.creation_slot, p.wallet",
        (cleaned,),
    ).fetchall()

    if dossier_row is not None:
        try:
            report = json.loads(str(dossier_row["report_json"]))
            if isinstance(report, dict):
                report["candidate"] = (
                    dict(candidate_row) if candidate_row is not None else None
                )
                report["launches_db"] = [dict(r) for r in launches]
                report["trades_db"] = [dict(r) for r in trades]
                report["wallet_launch_participation"] = [
                    dict(row) for row in wallet_launch_participation
                ]
                return report
        except Exception:
            pass

    return {
        "wallet": cleaned,
        "candidate": dict(candidate_row) if candidate_row is not None else None,
        "dossier": dict(dossier_row) if dossier_row is not None else None,
        "launches": [dict(r) for r in launches],
        "trades": [dict(r) for r in trades],
        "wallet_launch_participation": [
            dict(row) for row in wallet_launch_participation
        ],
        "funding_chain": [],
        "score": None,
    }
