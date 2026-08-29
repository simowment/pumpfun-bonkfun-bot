"""Historique batch enricher for rug_discover."""

# ruff: noqa: BLE001, C901, PLC0415, PLR0912, PLR0915, PLR2004, S110, S608, TRY003, TRY300

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import base58

from rugbot.discover.basket import scan_wallet_basket
from rugbot.discover.store import (
    ensure_discover_schema,
    fetch_launches_for_wallet,
    fetch_trades_for_wallet_launches,
    save_entity_mints,
    update_launch_metrics,
    upsert_candidate,
    upsert_dossier,
)
from rugbot.integrations.pumpfun_creator_index import fetch_pumpfun_created_tokens
from rugbot.intelligence.bundle_analysis import (
    analyze_entity_bundles,
    cross_entity_bundles_to_json,
    entity_bundle_analysis_to_json,
)
from rugbot.intelligence.entity_mint_index import discover_finalized_entity_mints
from rugbot.intelligence.token_resolver import resolve_token_or_wallet
from rugbot.interfaces.cli.check_mint import (
    _build_funding_chain,
    _resolve_entity_wallets,
)
from rugbot.runtime.config import load_provider_settings, resolve_dotenv
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.models import BundleParticipationRecord
from rugbot.utils.logger import get_logger

logger = get_logger(__name__)


def _validate_pubkey(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("address must not be empty")
    try:
        decoded = base58.b58decode(cleaned)
    except ValueError as exc:
        raise ValueError(f"address must be base58: {cleaned}") from exc
    if len(decoded) != 32 or base58.b58encode(decoded).decode("ascii") != cleaned:
        raise ValueError(f"address must be Solana pubkey: {cleaned}")
    return cleaned


def _resolve_wallet(
    raw: str,
    rpc_url: str,
    fallback_endpoints: tuple[str, ...],
) -> tuple[str, str | None, Any | None]:
    """Resolve wallet from mint or wallet input. Returns (wallet, mint_if_any, resolved)."""

    cleaned = _validate_pubkey(raw)
    try:
        resolved = resolve_token_or_wallet(
            cleaned,
            rpc_url=rpc_url,
            fallback_endpoints=fallback_endpoints,
            skip_metadata=True,
        )
        return (
            resolved.target_wallet,
            (cleaned if resolved.is_token else None),
            resolved,
        )
    except Exception:
        return cleaned, None, None


def enrich_wallet(
    wallet_or_mint: str,
    *,
    state_dir: Path = Path(".state/discover"),
    use_entity: bool = False,
) -> dict[str, Any]:
    """Enrich historique batch for a wallet (or mint→creator). Persists dossier + candidate."""

    resolve_dotenv()
    providers = load_provider_settings()
    rpc_url = providers.rpc_http
    fallback = providers.rpc_http_fallbacks
    if not rpc_url:
        raise ValueError("SOLANA_RPC_HTTP is required for enrich")

    target_wallet, mint_hint, _cached_resolved = _resolve_wallet(
        wallet_or_mint, rpc_url, fallback
    )
    logger.info(
        "enrich start wallet=%s mint_hint=%s entity=%s",
        target_wallet,
        mint_hint,
        use_entity,
    )

    db = DatabaseManager(state_dir / "rugbot.db")
    ensure_discover_schema(db)

    # 1. funding chain
    funding_rows: list[dict[str, Any]] = []
    funding_summary: str | None = None
    funding_error: str | None = None
    funding_wallets: list[str] = []
    try:
        # need bundle wallets: try resolve again for token hint to get bundle
        bundle_wallets: list[str] = []
        if mint_hint is not None and _cached_resolved is not None:
            bundle_wallets = list(_cached_resolved.bundle_wallets[:3])
        elif mint_hint is not None:
            try:
                resolved = resolve_token_or_wallet(
                    mint_hint,
                    rpc_url=rpc_url,
                    fallback_endpoints=fallback,
                    skip_metadata=True,
                )
                bundle_wallets = list(resolved.bundle_wallets[:3])
            except Exception:
                bundle_wallets = []
        # if wallet mode, fetch funding for wallet directly
        trace_wallets = [target_wallet, *bundle_wallets]
        # de-dupe
        seen: set[str] = set()
        uniq: list[str] = []
        for w in trace_wallets:
            if w and w not in seen:
                seen.add(w)
                uniq.append(w)
        funding_rows, funding_summary = _build_funding_chain(uniq, rpc_url, fallback)
        funding_wallets = _resolve_entity_wallets(
            funding_rows, target_wallet, bundle_wallets
        )
    except RuntimeError as exc:
        funding_error = str(exc)
        logger.warning("funding chain failed: %s", exc)
    except Exception as exc:
        funding_error = str(exc)
        logger.warning("funding chain error: %s", exc)

    entity_wallets = (
        funding_wallets if (use_entity and funding_wallets) else [target_wallet]
    )

    # 2. fetch historical mints via Pump index
    by_mint: dict[str, Any] = {}
    for w in entity_wallets:
        try:
            cands = fetch_pumpfun_created_tokens(w)
        except Exception as exc:
            logger.warning("fetch_pumpfun_created_tokens failed for %s: %s", w, exc)
            continue
        for c in cands:
            prev = by_mint.get(c.mint)
            if prev is None or int(c.created_timestamp) < int(prev.created_timestamp):
                by_mint[c.mint] = c

    deduped_sorted = sorted(
        by_mint.values(), key=lambda c: int(c.created_timestamp), reverse=True
    )

    # optional entity dedup already done; if use_entity False, filter to target_wallet only
    if not use_entity:
        deduped_sorted = [c for c in deduped_sorted if c.creator == target_wallet]
        # if empty (e.g., index miss), still try target_wallet fetch
        if not deduped_sorted:
            try:
                cands = fetch_pumpfun_created_tokens(target_wallet)
                by_mint2: dict[str, Any] = {}
                for c in cands:
                    prev = by_mint2.get(c.mint)
                    if prev is None or int(c.created_timestamp) < int(
                        prev.created_timestamp
                    ):
                        by_mint2[c.mint] = c
                deduped_sorted = sorted(
                    by_mint2.values(),
                    key=lambda c: int(c.created_timestamp),
                    reverse=True,
                )
            except Exception:
                pass

    # 3. Confirm the newest entity launches and aggregate creation-slot bundles.
    entity_mints = asyncio.run(
        discover_finalized_entity_mints(
            target_wallet=target_wallet,
            graph_wallets=tuple(
                wallet for wallet in entity_wallets if wallet != target_wallet
            ),
            endpoint=rpc_url,
            fallback_endpoints=fallback,
            anchor_mint=mint_hint,
        )
    )
    save_entity_mints(db, entity_mints.mints)
    bundle_analysis = analyze_entity_bundles(
        entity_mints.mints,
        entity_creator=target_wallet,
    )
    participations = tuple(
        BundleParticipationRecord(
            bundler_wallet=buy.wallet,
            mint=mint.mint,
            creator=mint.creator,
            creation_slot=mint.creation_slot,
            buy_signature=buy.signature,
            transaction_index=buy.transaction_index,
            max_sol_cost_lamports=buy.max_sol_cost_lamports,
        )
        for mint in entity_mints.mints
        for buy in mint.bundle_buys
    )
    repository = SQLiteTrackerRepository(db)
    repository.save_bundle_participations(participations)
    entity_creators = {mint.creator for mint in entity_mints.mints}
    cross_entity_participations = tuple(
        participation
        for participation in repository.get_bundle_participations(
            tuple(sorted({item.bundler_wallet for item in participations})),
            exclude_creator=target_wallet,
        )
        if participation.creator not in entity_creators
    )

    # 4. Attribute locally observed creator/bundler sells without inferring a rug.
    launches = fetch_launches_for_wallet(db, target_wallet)
    trades = fetch_trades_for_wallet_launches(db, target_wallet)
    bundle_payload = entity_bundle_analysis_to_json(bundle_analysis)
    bundle_launches = {
        str(item["mint"]): item
        for item in bundle_payload["launches"]
        if isinstance(item, dict) and isinstance(item.get("mint"), str)
    }
    repeat_bundler_wallets = tuple(
        item.bundler_wallet for item in bundle_analysis.repeat_bundlers
    )
    for launch in launches:
        mint = str(launch["mint"])
        creator = str(launch["creator"])
        creator_sell = db.connection.execute(
            "SELECT MIN(slot) AS slot FROM discover_trades "
            "WHERE mint = ? AND wallet = ? AND side = 'sell'",
            (mint, creator),
        ).fetchone()
        bundler_sell_count = 0
        if repeat_bundler_wallets:
            placeholders = ", ".join("?" for _ in repeat_bundler_wallets)
            bundler_sells = db.connection.execute(
                f"SELECT COUNT(*) AS count FROM discover_trades "
                f"WHERE mint = ? AND side = 'sell' "
                f"AND wallet IN ({placeholders})",
                (mint, *repeat_bundler_wallets),
            ).fetchone()
            bundler_sell_count = int(bundler_sells["count"])
        update_launch_metrics(
            db,
            mint,
            dev_sell_slot=(
                int(creator_sell["slot"])
                if creator_sell is not None and isinstance(creator_sell["slot"], int)
                else None
            ),
            bundler_sell_count=bundler_sell_count,
            bundle_json=(
                json.dumps(bundle_launches[mint], sort_keys=True)
                if mint in bundle_launches
                else None
            ),
        )

    wallet_trade_baskets = [
        dict(row)
        for row in db.connection.execute(
            """
            SELECT wallet,
                   COUNT(DISTINCT mint) AS token_count,
                   SUM(CASE WHEN side = 'buy' THEN 1 ELSE 0 END) AS buy_count,
                   SUM(CASE WHEN side = 'sell' THEN 1 ELSE 0 END) AS sell_count,
                   MIN(slot) AS first_slot,
                   MAX(slot) AS last_slot
            FROM discover_trades
            WHERE wallet IS NOT NULL
            GROUP BY wallet
            ORDER BY token_count DESC, buy_count DESC, wallet
            """
        ).fetchall()
    ]
    basket_scan: dict[str, object] = {
        "status": "not_run",
        "wallet": None,
        "pages_scanned": 0,
        "candidate_count": 0,
        "complete": False,
        "warning": None,
    }
    suspect_wallet = next(
        (
            str(item["wallet"])
            for item in wallet_trade_baskets
            if isinstance(item.get("buy_count"), int)
            and int(item["buy_count"]) > 0
            and str(item["wallet"]) != target_wallet
        ),
        None,
    )
    if suspect_wallet is not None and providers.solscan_api_key is not None:
        basket_scan = scan_wallet_basket(
            suspect_wallet,
            entity_mints=frozenset(by_mint),
            state_dir=state_dir,
            max_pages=5,
        )
    elif suspect_wallet is not None:
        basket_scan["warning"] = "SOLSCAN_API_KEY is unavailable"

    # 5. Build the fact-only dossier report.
    report: dict[str, Any] = {
        "wallet": target_wallet,
        "input": wallet_or_mint,
        "mint_hint": mint_hint,
        "entity_mode": use_entity,
        "entity_wallets": entity_wallets,
        "funding_chain": funding_rows,
        "funding_summary": funding_summary,
        "funding_error": funding_error,
        "funding_wallets": funding_wallets,
        "historical_mints": [
            {
                "mint": c.mint,
                "creator": c.creator,
                "symbol": c.symbol,
                "created_timestamp": c.created_timestamp,
            }
            for c in deduped_sorted[:50]
        ],
        "score": None,
        "score_status": "not_computed_without_executable_point_in_time_quotes",
        "entity_mint_discovery": {
            "confirmed_mint_count": len(entity_mints.mints),
            "warnings": list(entity_mints.warnings),
        },
        "bundle_analysis": bundle_payload,
        "cross_entity_bundles": cross_entity_bundles_to_json(
            cross_entity_participations
        ),
        "wallet_trade_baskets": wallet_trade_baskets,
        "suspect_wallet_basket_scan": basket_scan,
        "launches": launches,
        "trades": trades,
        "fees_lamports": None,
        "fees_status": "not_computed_from_finalized_transactions",
        "enriched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    # 6. persist candidate + dossier
    first_seen_slot: int | None = None
    if launches:
        try:
            first_seen_slot = min(
                int(r.get("created_slot", 0))
                for r in launches
                if r.get("created_slot") is not None
            )
        except Exception:
            first_seen_slot = None

    try:
        upsert_candidate(
            db,
            wallet=target_wallet,
            first_seen_slot=first_seen_slot,
            launch_count=len(deduped_sorted),
            winrate=None,
            best_tp=None,
        )
    except Exception as exc:
        logger.warning("upsert candidate failed: %s", exc)

    try:
        upsert_dossier(db, wallet=target_wallet, report_json=json.dumps(report))
    except Exception as exc:
        logger.warning("upsert dossier failed: %s", exc)

    # also enrich discover_launches enriched_at for known mints (best-effort)
    try:
        import datetime as _dt

        now_iso = _dt.datetime.utcnow().isoformat() + "Z"
        conn = db.connection
        for c in deduped_sorted[:20]:
            try:
                conn.execute(
                    "UPDATE discover_launches SET enriched_at = ? WHERE mint = ?",
                    (now_iso, c.mint),
                )
                conn.commit()
            except Exception:
                pass
    except Exception:
        pass

    logger.info("enrich done wallet=%s mints=%d", target_wallet, len(deduped_sorted))
    return report
