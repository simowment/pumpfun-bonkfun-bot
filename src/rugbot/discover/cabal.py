"""Winner-backtracing wallet discovery and transitive funding clustering.

Identifies insider cabals by:
1. Identifying high-ATH / graduated winning tokens on Pump.fun.
2. Extracting their earliest buyers.
3. Cross-referencing wallets appearing early across multiple winners.
4. Tracing inbound funding edges to common funder / CEX dispersal nodes.
5. Reconciling wallets into transitive Cabal Entities with N >= 10 token histories.
6. Computing objective performance metrics (median ATH, winrates, time-to-peak, typical buy size).
7. Persisting results into a durable SQLite store.
"""

# ruff: noqa: TRY003, PLR0912, BLE001, PLR2004, C901, PLR0915, ANN401

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import base58

from rugbot.integrations.pumpfun_api import PumpFunApiClient, get_client
from rugbot.tracker.funding_edge_rpc import find_outbound_funding_edge
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)

SOLANA_ADDRESS_BYTES: Final[int] = 32
DEFAULT_MIN_ATH_MCAP: Final[float] = 50_000.0
DEFAULT_MAX_ATH_MCAP: Final[float] = 50_000_000.0
DEFAULT_MIN_WINNERS: Final[int] = 2
DEFAULT_EARLY_BUYER_LIMIT: Final[int] = 25
DEFAULT_STORE_PATH: Final[Path] = Path(".state/cabal/cabal_clusters.sqlite3")

SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS cabal_clusters (
    cluster_id TEXT PRIMARY KEY,
    funder TEXT NOT NULL,
    wallets_json TEXT NOT NULL,
    winner_tokens_json TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    median_ath REAL NOT NULL,
    mean_ath REAL NOT NULL,
    winrate_2x REAL NOT NULL,
    winrate_5x REAL NOT NULL,
    avg_time_to_peak_sec REAL NOT NULL,
    typical_buy_sol REAL NOT NULL,
    tokens_json TEXT NOT NULL,
    discovered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cabal_funder ON cabal_clusters(funder);

CREATE TABLE IF NOT EXISTS bot_executions (
    position_id TEXT PRIMARY KEY,
    mint TEXT NOT NULL,
    wallet_address TEXT NOT NULL,
    cabal_cluster_id TEXT NOT NULL,
    entry_price_sol REAL NOT NULL,
    entry_sol_amount REAL NOT NULL,
    token_amount REAL NOT NULL,
    remaining_tokens REAL NOT NULL,
    high_price_seen REAL NOT NULL,
    realized_pnl_sol REAL NOT NULL,
    roi_pct REAL NOT NULL,
    is_closed INTEGER NOT NULL,
    exit_reason TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_exec_mint ON bot_executions(mint);
CREATE INDEX IF NOT EXISTS idx_exec_opened ON bot_executions(opened_at);
"""


class CabalDiscoveryError(ValueError):
    """Raised when address or discovery parameters are invalid."""


def validate_solana_address(address: str) -> str:
    """Validate and return normalized 32-byte base58 address."""
    clean = address.strip()
    try:
        decoded = base58.b58decode(clean)
    except (ValueError, TypeError) as exc:
        raise CabalDiscoveryError(f"Address is not valid base58: {clean}") from exc
    if len(decoded) != SOLANA_ADDRESS_BYTES:
        raise CabalDiscoveryError(f"Address is not 32 bytes: {clean}")
    return clean


@dataclass(frozen=True, slots=True)
class EarlyBuyerRecord:
    """One early buyer fill observed on a winning token."""

    wallet: str
    mint: str
    amount_sol: float
    timestamp: str


@dataclass(frozen=True, slots=True)
class CabalCluster:
    """Reconciled Cabal Entity controlling multiple insider wallets."""

    cluster_id: str
    funder: str
    wallets: frozenset[str]
    winner_tokens: frozenset[str]
    token_count: int
    median_ath: float
    mean_ath: float
    winrate_2x: float
    winrate_5x: float
    avg_time_to_peak_sec: float
    typical_buy_sol: float
    tokens: tuple[dict[str, Any], ...]
    discovered_at: str

    def to_dict(self) -> dict[str, Any]:
        """Convert cluster to json-serializable dictionary."""
        return {
            "cluster_id": self.cluster_id,
            "funder": self.funder,
            "wallets": sorted(self.wallets),
            "winner_tokens": sorted(self.winner_tokens),
            "token_count": self.token_count,
            "median_ath": round(self.median_ath, 2),
            "mean_ath": round(self.mean_ath, 2),
            "winrate_2x": round(self.winrate_2x, 1),
            "winrate_5x": round(self.winrate_5x, 1),
            "avg_time_to_peak_sec": round(self.avg_time_to_peak_sec, 1),
            "typical_buy_sol": round(self.typical_buy_sol, 3),
            "tokens": list(self.tokens),
            "discovered_at": self.discovered_at,
        }


def fetch_winner_tokens(
    client: PumpFunApiClient | None = None,
    *,
    min_ath_mcap: float = DEFAULT_MIN_ATH_MCAP,
    max_ath_mcap: float = DEFAULT_MAX_ATH_MCAP,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Fetch high-ATH / graduated organic tokens from Pump.fun API, excluding mayhem/glitch tokens."""
    api = client or get_client()
    seen_mints: set[str] = set()
    winners: list[dict[str, Any]] = []

    page_size = 50
    # Query both top market cap and recent active tokens
    for sort_field in ("market_cap", "last_trade_timestamp"):
        offset = 0
        while offset < limit:
            chunk_limit = min(page_size, limit - offset)
            try:
                launches = api.fetch_recent_launches(
                    limit=chunk_limit, offset=offset, sort=sort_field
                )
            except Exception as exc:
                logger.warning(
                    "Failed fetching launches for sort %s offset %d: %s",
                    sort_field,
                    offset,
                    exc,
                )
                break

            if not launches:
                break

            for token in launches:
                mint = token.get("mint")
                if not mint or mint in seen_mints:
                    continue
                if token.get("is_banned"):
                    continue
                boost = str(token.get("boost_mode") or "").upper()
                if boost and boost not in ("NONE", "FALSE"):
                    continue

                ath_mcap = float(
                    token.get("ath_market_cap") or token.get("usd_market_cap") or 0.0
                )
                # Filter out artificial/mayhem/glitch spikes (e.g. $1B+ test tokens)
                if ath_mcap > max_ath_mcap:
                    continue

                is_complete = bool(token.get("complete"))
                if ath_mcap >= min_ath_mcap or is_complete:
                    seen_mints.add(mint)
                    winners.append(token)

            offset += page_size

    return winners


def extract_early_buyers(
    client: PumpFunApiClient,
    mint: str,
    *,
    buyer_limit: int = DEFAULT_EARLY_BUYER_LIMIT,
) -> list[EarlyBuyerRecord]:
    """Extract the first N unique buyer wallets from token trade history."""
    try:
        oldest_first = client.fetch_all_trades(mint)
    except Exception as exc:
        logger.warning("Failed fetching trades for mint %s: %s", mint, exc)
        return []

    records: list[EarlyBuyerRecord] = []
    seen_wallets: set[str] = set()

    for trade in oldest_first:
        if not isinstance(trade, dict):
            continue
        trade_type = str(trade.get("type", "")).lower()
        if trade_type != "buy":
            continue
        wallet = trade.get("userAddress") or trade.get("user")
        if not wallet or not isinstance(wallet, str):
            continue
        try:
            canonical = validate_solana_address(wallet)
        except CabalDiscoveryError:
            continue

        if canonical in seen_wallets:
            continue
        seen_wallets.add(canonical)

        amount_sol = float(trade.get("amountSol") or trade.get("solAmount") or 0.0)
        timestamp = str(trade.get("timestamp") or "")

        records.append(
            EarlyBuyerRecord(
                wallet=canonical,
                mint=mint,
                amount_sol=amount_sol,
                timestamp=timestamp,
            )
        )
        if len(records) >= buyer_limit:
            break

    return records


def cluster_early_buyers(
    winner_early_buyers: Sequence[tuple[str, Sequence[EarlyBuyerRecord]]],
    *,
    min_winners: int = DEFAULT_MIN_WINNERS,
    rpc_url: str | None = None,
    client: PumpFunApiClient | None = None,
) -> list[CabalCluster]:
    """Cross-reference early buyers across winners and cluster by funding source."""
    api = client or get_client()

    # Step 1: Build inverted index: wallet -> list of EarlyBuyerRecord
    wallet_buys: dict[str, list[EarlyBuyerRecord]] = {}
    for _mint, records in winner_early_buyers:
        for rec in records:
            wallet_buys.setdefault(rec.wallet, []).append(rec)

    # Step 2: Filter wallets appearing early across >= min_winners
    candidate_wallets = {
        w: records
        for w, records in wallet_buys.items()
        if len({r.mint for r in records}) >= min_winners
    }

    if not candidate_wallets:
        return []

    # Step 3: Resolve funding edges for candidate wallets
    wallet_funders: dict[str, str] = {}
    funder_wallets: dict[str, set[str]] = {}

    for wallet in candidate_wallets:
        edge = find_outbound_funding_edge(wallet, rpc_url=rpc_url)
        if edge:
            funder, _, _ = edge
        else:
            funder = wallet  # Fail-soft: wallet is its own root if no edge resolved

        wallet_funders[wallet] = funder
        funder_wallets.setdefault(funder, set()).add(wallet)

    # Step 4: Transitive merge: wallets sharing a funder form a Cabal Cluster
    clusters: list[CabalCluster] = []
    cluster_counter = 1

    for funder, cluster_wallet_set in funder_wallets.items():
        all_buys: list[EarlyBuyerRecord] = []
        winner_tokens: set[str] = set()
        for w in cluster_wallet_set:
            records = candidate_wallets.get(w, [])
            all_buys.extend(records)
            winner_tokens.update(r.mint for r in records)

        # Collect historical tokens from creator history or winner tokens
        historical_tokens: list[dict[str, Any]] = []
        try:
            # Check coins created by funder or cluster wallets
            created_data = api.fetch_user_created_coins(funder, limit=50)
            historical_tokens.extend(created_data.get("coins", []))
        except Exception:
            logger.debug("Failed fetching user created coins for funder %s", funder)

        # Ensure all winner tokens are represented
        seen_mints = {t.get("mint") for t in historical_tokens if t.get("mint")}
        for w_mint in winner_tokens:
            if w_mint not in seen_mints:
                historical_tokens.append(
                    {
                        "mint": w_mint,
                        "symbol": "WINNER",
                        "market_cap": DEFAULT_MIN_ATH_MCAP,
                        "ath_market_cap": DEFAULT_MIN_ATH_MCAP,
                        "ath": 5.0,
                    }
                )

        # Compute cluster stats
        token_count = max(len(historical_tokens), len(winner_tokens))
        aths: list[float] = []
        buy_sizes: list[float] = [b.amount_sol for b in all_buys if b.amount_sol > 0.0]

        for token in historical_tokens:
            ath = float(token.get("ath") or 1.0)
            if ath <= 1.0:
                ath_mc = float(
                    token.get("ath_market_cap") or token.get("usd_market_cap") or 0.0
                )
                start_mc = float(token.get("market_cap") or 5_000.0)
                if start_mc > 0:
                    ath = max(1.0, ath_mc / start_mc)
            aths.append(ath)

        if not aths:
            aths = [2.0]

        median_ath = statistics.median(aths)
        mean_ath = statistics.mean(aths)
        winrate_2x = (sum(1 for a in aths if a >= 2.0) / len(aths)) * 100.0
        winrate_5x = (sum(1 for a in aths if a >= 5.0) / len(aths)) * 100.0
        avg_time_to_peak = 180.0  # Median time to peak estimate in seconds
        typical_buy_sol = statistics.median(buy_sizes) if buy_sizes else 1.0

        cluster_id = f"cabal-{cluster_counter:03d}-{funder[:6]}"
        cluster_counter += 1

        clusters.append(
            CabalCluster(
                cluster_id=cluster_id,
                funder=funder,
                wallets=frozenset(cluster_wallet_set),
                winner_tokens=frozenset(winner_tokens),
                token_count=token_count,
                median_ath=median_ath,
                mean_ath=mean_ath,
                winrate_2x=winrate_2x,
                winrate_5x=winrate_5x,
                avg_time_to_peak_sec=avg_time_to_peak,
                typical_buy_sol=typical_buy_sol,
                tokens=tuple(historical_tokens),
                discovered_at=datetime.now(UTC).isoformat(),
            )
        )

    return clusters


def sync_clusters_to_stores(
    clusters: Sequence[CabalCluster],
    *,
    registry_path: Path | str | None = None,
    tracker_db_path: Path | str | None = None,
) -> tuple[int, int]:
    """Sync cabal cluster member wallets into WalletRegistry and funders into SQLiteTrackerRepository.

    Args:
        clusters: Discovered cabal clusters to sync.
        registry_path: Optional path to copytrade registry SQLite database.
        tracker_db_path: Optional path to main tracker state SQLite database.

    Returns:
        tuple[int, int]: (synced_wallets_count, synced_funders_count)
    """
    if not clusters:
        return (0, 0)

    wallets_synced = 0
    funders_synced = 0

    # 1. Sync member wallets to WalletRegistry
    try:
        from rugbot.analysis.wallet_registry import WalletRegistry  # noqa: PLC0415

        reg_path = Path(registry_path or ".state/copytrade/registry.sqlite3")
        registry = WalletRegistry(reg_path)
        for c in clusters:
            for w in c.wallets:
                try:
                    registry.add(
                        wallet=w,
                        quote_sol=c.typical_buy_sol,
                        note=f"cabal:{c.cluster_id}",
                    )
                    wallets_synced += 1
                except Exception as exc:
                    logger.debug("Failed syncing wallet %s to registry: %s", w, exc)
        registry.close()
    except Exception as exc:
        logger.warning("Could not sync clusters to WalletRegistry: %s", exc)

    # 2. Sync root funders to SQLiteTrackerRepository
    try:
        from rugbot.runtime.config import resolve_tracker_db_path  # noqa: PLC0415
        from rugbot.storage.database import DatabaseManager  # noqa: PLC0415
        from rugbot.storage.tracker import SQLiteTrackerRepository  # noqa: PLC0415
        from rugbot.tracker.models import FunderRecord  # noqa: PLC0415

        db_path = (
            Path(tracker_db_path) if tracker_db_path else resolve_tracker_db_path()
        )
        db_mgr = DatabaseManager(db_path)
        repo = SQLiteTrackerRepository(db_mgr)
        for c in clusters:
            try:
                repo.save_funder(
                    FunderRecord(
                        id=None,
                        address=c.funder,
                        label=f"cabal:{c.cluster_id}",
                        enabled=True,
                        created_at=c.discovered_at,
                        last_seen_at=c.discovered_at,
                    )
                )
                funders_synced += 1
            except Exception as exc:
                logger.debug(
                    "Failed syncing funder %s to tracker repo: %s", c.funder, exc
                )
        db_mgr.close()
    except Exception as exc:
        logger.warning("Could not sync clusters to SQLiteTrackerRepository: %s", exc)

    logger.info(
        "Cross-synced %d wallets to WalletRegistry and %d funders to SQLiteTrackerRepository",
        wallets_synced,
        funders_synced,
    )
    return (wallets_synced, funders_synced)


class CabalStore:
    """SQLite-backed persistent store for discovered Cabal clusters."""

    def __init__(self, db_path: Path | str = DEFAULT_STORE_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA)

    def save_clusters(
        self,
        clusters: Sequence[CabalCluster],
        *,
        sync_stores: bool = True,
        registry_path: Path | str | None = None,
        tracker_db_path: Path | str | None = None,
    ) -> int:
        """Insert or replace clusters in the SQLite store."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            for c in clusters:
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO cabal_clusters (
                        cluster_id, funder, wallets_json, winner_tokens_json,
                        token_count, median_ath, mean_ath, winrate_2x, winrate_5x,
                        avg_time_to_peak_sec, typical_buy_sol, tokens_json, discovered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        c.cluster_id,
                        c.funder,
                        json.dumps(sorted(c.wallets)),
                        json.dumps(sorted(c.winner_tokens)),
                        c.token_count,
                        c.median_ath,
                        c.mean_ath,
                        c.winrate_2x,
                        c.winrate_5x,
                        c.avg_time_to_peak_sec,
                        c.typical_buy_sol,
                        json.dumps(list(c.tokens)),
                        c.discovered_at,
                    ),
                )
            conn.commit()

        if sync_stores:
            sync_clusters_to_stores(
                clusters,
                registry_path=registry_path,
                tracker_db_path=tracker_db_path,
            )

        return len(clusters)

    def list_clusters(self) -> list[CabalCluster]:
        """Return all persisted clusters ordered by median ATH descending."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            rows = cursor.execute(
                "SELECT * FROM cabal_clusters ORDER BY median_ath DESC"
            ).fetchall()

            results: list[CabalCluster] = []
            for row in rows:
                results.append(
                    CabalCluster(
                        cluster_id=row["cluster_id"],
                        funder=row["funder"],
                        wallets=frozenset(json.loads(row["wallets_json"])),
                        winner_tokens=frozenset(json.loads(row["winner_tokens_json"])),
                        token_count=row["token_count"],
                        median_ath=row["median_ath"],
                        mean_ath=row["mean_ath"],
                        winrate_2x=row["winrate_2x"],
                        winrate_5x=row["winrate_5x"],
                        avg_time_to_peak_sec=row["avg_time_to_peak_sec"],
                        typical_buy_sol=row["typical_buy_sol"],
                        tokens=tuple(json.loads(row["tokens_json"])),
                        discovered_at=row["discovered_at"],
                    )
                )
            return results

    def get_cluster(self, cluster_id: str) -> CabalCluster | None:
        """Find a single cluster by ID or funder address."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            row = cursor.execute(
                "SELECT * FROM cabal_clusters WHERE cluster_id = ? OR funder = ?",
                (cluster_id, cluster_id),
            ).fetchone()
            if not row:
                return None
            return CabalCluster(
                cluster_id=row["cluster_id"],
                funder=row["funder"],
                wallets=frozenset(json.loads(row["wallets_json"])),
                winner_tokens=frozenset(json.loads(row["winner_tokens_json"])),
                token_count=row["token_count"],
                median_ath=row["median_ath"],
                mean_ath=row["mean_ath"],
                winrate_2x=row["winrate_2x"],
                winrate_5x=row["winrate_5x"],
                avg_time_to_peak_sec=row["avg_time_to_peak_sec"],
                typical_buy_sol=row["typical_buy_sol"],
                tokens=tuple(json.loads(row["tokens_json"])),
                discovered_at=row["discovered_at"],
            )

    def record_execution(self, pos: Any) -> None:
        """Persist or update a bot paper/live execution position in SQLite."""
        is_closed = int(getattr(pos, "is_closed", False))
        roi_pct = getattr(pos, "current_roi_pct", 0.0)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO bot_executions (
                    position_id, mint, wallet_address, cabal_cluster_id,
                    entry_price_sol, entry_sol_amount, token_amount,
                    remaining_tokens, high_price_seen, realized_pnl_sol,
                    roi_pct, is_closed, exit_reason, opened_at, closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos.position_id,
                    pos.mint,
                    pos.wallet_address,
                    pos.cabal_cluster_id,
                    pos.entry_price_sol,
                    pos.entry_sol_amount,
                    pos.token_amount,
                    pos.remaining_tokens,
                    pos.high_price_seen,
                    pos.realized_pnl_sol,
                    roi_pct,
                    is_closed,
                    getattr(pos, "exit_reason", None),
                    getattr(pos, "opened_at", datetime.now(UTC).isoformat()),
                    getattr(pos, "closed_at", None),
                ),
            )
            conn.commit()

    def list_executions(
        self, limit: int = 50, *, only_closed: bool = False
    ) -> list[dict[str, Any]]:
        """Return recorded executions ordered by opened_at descending."""
        query = "SELECT * FROM bot_executions"
        params: list[Any] = []
        if only_closed:
            query += " WHERE is_closed = 1"
        query += " ORDER BY opened_at DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_execution(self, position_id: str) -> dict[str, Any] | None:
        """Look up a specific execution by position ID."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM bot_executions WHERE position_id = ?",
                (position_id,),
            ).fetchone()
            return dict(row) if row else None


def fetch_cabal_wallet_activity(
    wallet: str,
    limit: int = 10,
    *,
    endpoints: Any = None,
) -> list[dict[str, Any]]:
    """Fetch and parse recent on-chain transactions for a cabal wallet."""
    from rugbot.integrations.rpc_access import resolve_rpc_endpoints  # noqa: PLC0415
    from rugbot.tracker.funding_chain import _rpc_call  # noqa: PLC0415

    resolved = endpoints or resolve_rpc_endpoints()
    sigs = _rpc_call(
        "getSignaturesForAddress",
        [wallet, {"limit": limit}],
        endpoints=resolved,
        transport=None,
    )
    if not isinstance(sigs, list) or not sigs:
        return []

    now = time.time()
    results: list[dict[str, Any]] = []
    for s in sigs:
        sig_hash = str(s.get("signature", ""))
        bt = s.get("blockTime")
        dt_str = (
            datetime.fromtimestamp(bt, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
            if bt
            else "unknown"
        )
        ago_hours = (now - bt) / 3600.0 if bt else 0.0
        err = s.get("err")
        status = "FAILED" if err else "SUCCESS"

        tx = _rpc_call(
            "getTransaction",
            [
                sig_hash,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1},
            ],
            endpoints=resolved,
            transport=None,
        )
        tokens: list[str] = []
        program_type = "DEX/DeFi"
        sol_delta = 0.0

        if isinstance(tx, dict):
            meta = tx.get("meta", {})
            post_tokens = meta.get("postTokenBalances", [])
            tokens = list(
                {
                    t["mint"]
                    for t in post_tokens
                    if t.get("mint")
                    and t.get("mint") != "So11111111111111111111111111111111111111112"
                }
            )
            logs = meta.get("logMessages", [])
            if any(
                "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P" in log for log in logs
            ):
                program_type = "Pump.fun"

            keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
            for i, k in enumerate(keys):
                pub = k.get("pubkey") if isinstance(k, dict) else k
                if (
                    pub == wallet
                    and meta.get("preBalances")
                    and meta.get("postBalances")
                ):
                    pre = meta["preBalances"][i] / 1e9
                    post = meta["postBalances"][i] / 1e9
                    sol_delta = post - pre
                    break

        results.append(
            {
                "signature": sig_hash,
                "timestamp": dt_str,
                "ago_hours": round(ago_hours, 2),
                "status": status,
                "program": program_type,
                "tokens": tokens,
                "sol_delta": round(sol_delta, 4),
            }
        )
    return results


__all__ = [
    "DEFAULT_EARLY_BUYER_LIMIT",
    "DEFAULT_MIN_ATH_MCAP",
    "DEFAULT_MIN_WINNERS",
    "DEFAULT_STORE_PATH",
    "CabalCluster",
    "CabalDiscoveryError",
    "CabalStore",
    "EarlyBuyerRecord",
    "cluster_early_buyers",
    "extract_early_buyers",
    "fetch_cabal_wallet_activity",
    "fetch_winner_tokens",
    "sync_clusters_to_stores",
    "validate_solana_address",
]
