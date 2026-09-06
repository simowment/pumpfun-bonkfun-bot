"""Autonomous rugger discovery - bible-corrected, ENTITY-level, stats manual.

Read-only (§7): surfaces operator ENTITIES (funding clusters) with discovery
evidence - archetype, funding chain, spam cap, in-window cadence - and
*recommends* arming but never auto-arms or trades.

Bible alignment (MEMECOIN_BIBLE_SCRAPED.md §1-§3, AGENTS.md §14):
  * The unit of analysis is the ENTITY (creator + mother/sous-meres +
    burners resolved from the funding chain), because the best ruggers switch
    wallets (§3.2 CEX -> fresh wallet per token; §2 Method 2). A single-wallet
    view hides a wallet-switcher's true track record - each burner looks like a
    1-launch wallet with no history.
  * Mass spammers are EXCLUDED, not ranked: a wallet whose lifetime Pump.fun
    creations exceed ``max_creations`` (bible §1 "Max 5 a 10 creations ...
    elimine les spammeurs de masse") is classified ``mass_spammer``. This is
    the deliberate inversion of the previous count-DESC ranker that surfaced
    2000+-creation spam wallets as top targets.
  * Winrate/EV stats are NOT auto-computed: batch metadata scoring proved
    unreliable (rate-limited fetches fabricated 0% winrates). Qualification
    stats are MANUAL - run ``rug_check <wallet> --score --entity`` per target.
    Only the funding-chain resolver (``_build_funding_chain`` /
    ``_resolve_entity_wallets``) is reused here.

``use_rpc=False`` degrades to an honestly-labelled in-window-only view (no
entity resolution). Nothing in this module fabricates a track record.
"""

# ruff: noqa: C901, PLC0415, PLR0912, PLR0913, PLR0915, S608, TRY003

from __future__ import annotations

import datetime as dt
import json
import statistics
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from rugbot.discover.candidates import _parse_since
from rugbot.discover.store import (
    ensure_discover_schema,
    fetch_rugger_cache,
    get_discover_state_dir,
    save_rugger_cache,
    update_launch_metrics,
    upsert_candidate,
)
from rugbot.storage.database import DatabaseManager
from rugbot.utils.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

PPM_DENOMINATOR = 1_000_000
TYPE1_ARCHETYPE = "type1_serial_same_wallet"
TYPE2_ARCHETYPE = "type2_funding_cluster"
MASS_SPAMMER_ARCHETYPE = "mass_spammer"

STATUS_STATS_MANUAL = "stats_manual"
STATUS_ABSTAIN = "abstain"
STATUS_NO_RPC = "no_rpc_in_window_only"
STATUS_MASS_SPAMMER = "mass_spammer"

TYPE2_NOTE = (
    "Best ruggers switch wallets (bible 3.2 CEX->fresh-wallet-per-token, "
    "bible 2 Method 2), so analysis is ENTITY-level: the funding chain "
    "(creator + mother/sous-meres + burners) is resolved via live RPC. "
    "Single-wallet same-wallet deployers are Type 1; recurrent-funder / "
    "multi-wallet clusters are Type 2. Mass spammers (lifetime creations > "
    "max-creations) are excluded, not ranked. Winrate/EV stats are manual: "
    "run rug_check <wallet> --score --entity per target."
)

MIN_LAUNCHES_FLOOR = 1
LIMIT_FLOOR = 1
LIMIT_CEILING = 500
MAX_CREATIONS_DEFAULT = 10
MAX_CREATIONS_CEILING = 100
SAMPLE_COUNT_DEFAULT = 10

# RPC cost bounds (bible §5: audit 2-3 qualified ruggers, not the whole firehose).
RPC_SEED_POOL_MAX = 150
RPC_SCORE_MAX = 40
ENTITY_WALLET_CAP = 12
BUNDLE_WALLETS_MAX = 3
FUNDING_COUNT_WORKERS = 8
CACHE_TTL_SECONDS = 6 * 3600

_STATUS_ORDER = {
    STATUS_STATS_MANUAL: 0,
    STATUS_ABSTAIN: 1,
    STATUS_NO_RPC: 2,
    STATUS_MASS_SPAMMER: 3,
}


@dataclass(frozen=True, slots=True)
class LaunchCadence:
    """Serial-deploy timing evidence for one creator wallet (in-window)."""

    launch_count: int
    first_created_at: str | None
    last_created_at: str | None
    median_interval_minutes: float | None
    mean_interval_minutes: float | None


@dataclass(frozen=True, slots=True)
class AthExitProfile:
    """ATH amplitude and dev-dump attribution across a creator's launches."""

    launches_with_trade_evidence: int
    median_ath_multiplier_ppm: int | None
    peak_ath_multiplier_ppm: int | None
    dev_dump_count: int
    dev_dump_rate_ppm: int
    median_slots_to_dump: int | None


@dataclass(frozen=True, slots=True)
class Qualification:
    """Bible entity-level qualification (winrate/EV) plus observed distribution."""

    status: str
    reason: str
    message: str
    sample_count: int
    launches_scored: int
    winrate_pct: float | None
    net_ev_pct: float | None
    optimal_tp_pct: int | None
    robust_zone: list[int]
    rows: list[dict[str, Any]]
    launches_with_trade_evidence: int
    observed_median_ath_multiplier_ppm: int | None
    observed_peak_ath_multiplier_ppm: int | None
    observed_dev_dump_rate_ppm: int


@dataclass(frozen=True, slots=True)
class RuggerEvidence:
    """One ranked operator ENTITY with the §14 evidence contract."""

    rank: int
    operator: str
    archetype: str
    archetype_note: str
    entity_wallets: list[str]
    funding_summary: str | None
    primary_funder: str | None
    lifetime_creation_count: int | None
    in_window_launch_count: int
    launch_cadence: LaunchCadence
    ath_exit_profile: AthExitProfile
    qualification: Qualification
    next_action: str


def _parse_iso(value: object) -> dt.datetime | None:
    """Parse a persisted ISO-8601 timestamp into an aware UTC datetime."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt.UTC)


def _median_int(values: list[int]) -> int | None:
    """Return the integer median of a list, or None when empty."""

    if not values:
        return None
    return int(statistics.median(values))


def recompute_launch_metrics(db: DatabaseManager) -> int:
    """Recompute per-launch evidence metrics from persisted trades (no RPC).

    Idempotent. For every mint that has trade evidence, aggregates from
    ``discover_trades`` and persists via ``update_launch_metrics``:
      * ``volume_lamports``    = SUM(quote_amount_base_units WHERE side='buy')
      * ``ath_quote_lamports`` = MAX(price_ppm)
      * ``dev_sell_slot`` / ``dump_slot`` = MIN(slot WHERE wallet=creator AND
        side='sell')
      * ``bundler_sell_count`` = distinct non-creator wallets that bought in the
        creation slot (block-0 same-slot bundlers) and later sold.

    Returns the number of launch rows updated.
    """

    conn = db.connection
    launches: dict[str, tuple[str, int]] = {
        str(row["mint"]): (str(row["creator"]), int(row["created_slot"]))
        for row in conn.execute(
            "SELECT mint, creator, created_slot FROM discover_launches"
        ).fetchall()
    }
    if not launches:
        return 0

    buy_volume: dict[str, int] = {}
    ath_ppm: dict[str, int] = {}
    dev_sell_slot: dict[str, int] = {}
    same_slot_buyers: dict[str, set[str]] = {}
    sellers: dict[str, set[str]] = {}

    trade_rows = conn.execute(
        "SELECT mint, slot, side, wallet, quote_amount_base_units, price_ppm "
        "FROM discover_trades"
    ).fetchall()
    for trade in trade_rows:
        mint = str(trade["mint"])
        launch = launches.get(mint)
        if launch is None:
            continue
        creator, created_slot = launch
        slot = trade["slot"]
        side = trade["side"]
        wallet = trade["wallet"]
        price_ppm = trade["price_ppm"]
        if side == "buy":
            quote = trade["quote_amount_base_units"]
            buy_volume[mint] = buy_volume.get(mint, 0) + int(quote or 0)
            if (
                isinstance(wallet, str)
                and wallet
                and wallet != creator
                and slot is not None
                and int(slot) == created_slot
            ):
                same_slot_buyers.setdefault(mint, set()).add(wallet)
        elif isinstance(wallet, str) and wallet:
            sellers.setdefault(mint, set()).add(wallet)
            if wallet == creator and slot is not None:
                prior = dev_sell_slot.get(mint)
                if prior is None or int(slot) < prior:
                    dev_sell_slot[mint] = int(slot)
        if price_ppm is not None:
            prior_ath = ath_ppm.get(mint)
            if prior_ath is None or int(price_ppm) > prior_ath:
                ath_ppm[mint] = int(price_ppm)

    touched = set(buy_volume) | set(ath_ppm) | set(dev_sell_slot) | set(sellers)
    updated = 0
    for mint in touched:
        bundler_sell_count = len(
            same_slot_buyers.get(mint, set()) & sellers.get(mint, set())
        )
        dump_slot = dev_sell_slot.get(mint)
        update_launch_metrics(
            db,
            mint,
            volume_lamports=buy_volume.get(mint, 0),
            ath_quote_lamports=ath_ppm.get(mint),
            dev_sell_slot=dump_slot,
            dump_slot=dump_slot,
            bundler_sell_count=bundler_sell_count,
        )
        updated += 1
    return updated


_LAUNCH_COLUMNS = (
    "mint, created_slot, created_at, ath_quote_lamports, dev_sell_slot, "
    "dump_slot, volume_lamports, bundler_sell_count, mc_1s_lamports"
)


def _in_window_launches(
    db: DatabaseManager, creator: str, since_iso: str | None
) -> list[dict[str, object]]:
    """Return a creator's persisted launches (optionally within ``since``)."""

    conn = db.connection
    if since_iso is not None:
        rows = conn.execute(
            f"SELECT {_LAUNCH_COLUMNS} FROM discover_launches "
            "WHERE creator = ? AND created_at IS NOT NULL AND created_at >= ? "
            "ORDER BY created_at ASC",
            (creator, since_iso),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_LAUNCH_COLUMNS} FROM discover_launches "
            "WHERE creator = ? ORDER BY created_at ASC",
            (creator,),
        ).fetchall()
    return [dict(row) for row in rows]


def _launch_cadence(launches: list[dict[str, object]]) -> LaunchCadence:
    """Compute serial-deploy cadence (median + mean interval) from timestamps."""

    timestamps = sorted(
        parsed
        for parsed in (_parse_iso(row.get("created_at")) for row in launches)
        if parsed is not None
    )
    intervals_minutes = [
        (timestamps[index + 1] - timestamps[index]).total_seconds() / 60.0
        for index in range(len(timestamps) - 1)
    ]
    return LaunchCadence(
        launch_count=len(launches),
        first_created_at=timestamps[0].isoformat() if timestamps else None,
        last_created_at=timestamps[-1].isoformat() if timestamps else None,
        median_interval_minutes=(
            round(statistics.median(intervals_minutes), 2)
            if intervals_minutes
            else None
        ),
        mean_interval_minutes=(
            round(statistics.fmean(intervals_minutes), 2) if intervals_minutes else None
        ),
    )


def _ath_exit_profile(
    db: DatabaseManager, launches: list[dict[str, object]]
) -> AthExitProfile:
    """Compute ATH-multiplier distribution and dev-dump attribution.

    The ATH multiplier is derived from observed trade prices only: the lowest
    observed price (entry proxy) to the highest observed price (ATH). Launches
    without trade evidence are excluded rather than assumed, and the excluded
    count is reported honestly.
    """

    conn = db.connection
    mints = [str(row["mint"]) for row in launches]
    price_by_mint: dict[str, tuple[int, int]] = {}
    if mints:
        placeholders = ",".join("?" for _ in mints)
        rows = conn.execute(
            "SELECT mint, MIN(price_ppm) AS entry_ppm, MAX(price_ppm) AS ath_ppm "
            f"FROM discover_trades WHERE mint IN ({placeholders}) "
            "AND price_ppm IS NOT NULL GROUP BY mint",
            tuple(mints),
        ).fetchall()
        for row in rows:
            entry_ppm = row["entry_ppm"]
            peak_ppm = row["ath_ppm"]
            if entry_ppm is not None and peak_ppm is not None:
                price_by_mint[str(row["mint"])] = (int(entry_ppm), int(peak_ppm))

    multipliers_ppm: list[int] = []
    slots_to_dump: list[int] = []
    dev_dump_count = 0
    for row in launches:
        pair = price_by_mint.get(str(row["mint"]))
        if pair is not None:
            entry_ppm, peak_ppm = pair
            if entry_ppm > 0:
                multipliers_ppm.append(peak_ppm * PPM_DENOMINATOR // entry_ppm)
        dump = row.get("dev_sell_slot")
        created_slot = row.get("created_slot")
        if dump is not None:
            dev_dump_count += 1
            if created_slot is not None and int(dump) >= int(created_slot):
                slots_to_dump.append(int(dump) - int(created_slot))

    total = len(launches)
    dev_dump_rate_ppm = (dev_dump_count * PPM_DENOMINATOR // total) if total else 0
    return AthExitProfile(
        launches_with_trade_evidence=len(multipliers_ppm),
        median_ath_multiplier_ppm=_median_int(multipliers_ppm),
        peak_ath_multiplier_ppm=max(multipliers_ppm) if multipliers_ppm else None,
        dev_dump_count=dev_dump_count,
        dev_dump_rate_ppm=dev_dump_rate_ppm,
        median_slots_to_dump=_median_int(slots_to_dump),
    )


def _block0_bundle_wallets(
    db: DatabaseManager, creator: str, since_iso: str | None
) -> list[str]:
    """Distinct block-0 (same-slot) non-creator buyers across a creator's launches.

    These are the bible §2 "double signature / bundle" satellite wallets; they
    seed the funding-chain trace so the entity resolves beyond the lone creator.
    """

    conn = db.connection
    if since_iso is not None:
        rows = conn.execute(
            "SELECT DISTINCT t.wallet AS w FROM discover_trades t "
            "JOIN discover_launches l ON l.mint = t.mint "
            "WHERE l.creator = ? AND l.created_at >= ? AND t.side = 'buy' "
            "AND t.slot = l.created_slot AND t.wallet IS NOT NULL AND t.wallet != ? "
            "LIMIT ?",
            (creator, since_iso, creator, BUNDLE_WALLETS_MAX),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT t.wallet AS w FROM discover_trades t "
            "JOIN discover_launches l ON l.mint = t.mint "
            "WHERE l.creator = ? AND t.side = 'buy' AND t.slot = l.created_slot "
            "AND t.wallet IS NOT NULL AND t.wallet != ? LIMIT ?",
            (creator, creator, BUNDLE_WALLETS_MAX),
        ).fetchall()
    return [str(row["w"]) for row in rows if row["w"]]


def _classify_archetype(entity_wallets: list[str], funding_summary: str | None) -> str:
    """Type 2 when a recurrent funder or a multi-wallet cluster is resolved."""

    recurrent = (
        isinstance(funding_summary, str) and "recurrent" in funding_summary.lower()
    )
    if recurrent or len(entity_wallets) > 1:
        return TYPE2_ARCHETYPE
    return TYPE1_ARCHETYPE


def _primary_funder(
    funding_rows: list[dict[str, Any]], entity_wallets: set[str]
) -> str | None:
    """Most frequent upstream funder of the entity (mother/relay/CEX hot wallet)."""

    funders = [
        row["from"]
        for row in funding_rows
        if isinstance(row.get("from"), str)
        and row.get("from") != "unknown"
        and row.get("to") in entity_wallets
    ]
    if not funders:
        funders = [
            row["from"]
            for row in funding_rows
            if isinstance(row.get("from"), str) and row.get("from") != "unknown"
        ]
    if not funders:
        return None
    return Counter(funders).most_common(1)[0][0]


def _observed_fields(ath_profile: AthExitProfile) -> dict[str, Any]:
    return {
        "launches_with_trade_evidence": ath_profile.launches_with_trade_evidence,
        "observed_median_ath_multiplier_ppm": ath_profile.median_ath_multiplier_ppm,
        "observed_peak_ath_multiplier_ppm": ath_profile.peak_ath_multiplier_ppm,
        "observed_dev_dump_rate_ppm": ath_profile.dev_dump_rate_ppm,
    }


def _manual_stats_qualification(
    creator: str,
    ath_profile: AthExitProfile,
) -> Qualification:
    """Stats are manual by design: batch scoring proved flaky, so no winrate/EV."""

    return Qualification(
        status=STATUS_STATS_MANUAL,
        reason="stats_manual (winrate/EV not auto-computed)",
        message=(
            "Score this entity manually (bible 1 step 5): "
            f"uv run rug_check {creator} --score --entity"
        ),
        sample_count=SAMPLE_COUNT_DEFAULT,
        launches_scored=0,
        winrate_pct=None,
        net_ev_pct=None,
        optimal_tp_pct=None,
        robust_zone=[],
        rows=[],
        **_observed_fields(ath_profile),
    )


def _next_action(archetype: str, operator: str, primary_funder: str | None) -> str:
    if archetype == TYPE2_ARCHETYPE:
        if primary_funder:
            return (
                f"arm listener on funding source {primary_funder} to detect staged "
                "burners (observe-only; no auto-arm)"
            )
        return (
            f"arm on upstream funder of {operator} to detect staged burners "
            "(observe-only; no auto-arm)"
        )
    return f"re-arm listener on dev wallet {operator} (observe-only; no auto-arm)"


def _archetype_note(
    archetype: str,
    entity_wallets: list[str],
    funding_summary: str | None,
    in_window_count: int,
    lifetime: int | None,
    max_creations: int,
) -> str:
    if archetype == TYPE2_ARCHETYPE:
        cluster = f"Funding cluster of {len(entity_wallets)} wallets"
        if funding_summary:
            cluster += f"; recurrent funder signal: {funding_summary}"
        return cluster + " - wallet-switching operator (bible 3.2 / 2 Method 2)."
    lifetime_txt = f", lifetime {lifetime}" if lifetime is not None else ""
    return (
        f"Same wallet deployed {in_window_count} launches in-window{lifetime_txt} - "
        f"serial same-wallet deployer (bible 1, capped at {max_creations} creations)."
    )


def _build_evidence(
    db: DatabaseManager,
    creator: str,
    in_window_count: int,
    since_iso: str | None,
    *,
    entity_wallets: list[str],
    funding_summary: str | None,
    primary_funder: str | None,
    lifetime: int | None,
    archetype: str,
    status_override: str | None = None,
    reason_override: str | None = None,
    max_creations: int = MAX_CREATIONS_DEFAULT,
) -> RuggerEvidence:
    """Assemble the §14 evidence contract for one operator entity."""

    launches = _in_window_launches(db, creator, since_iso)
    cadence = _launch_cadence(launches)
    ath_profile = _ath_exit_profile(db, launches)
    if status_override is not None:
        qualification = Qualification(
            status=status_override,
            reason=reason_override or status_override,
            message=(
                "Excluded before entity resolution (bible 1 spam cap / fail-closed)."
                if status_override == STATUS_MASS_SPAMMER
                else "Fail-closed: no fabricated winrate/EV."
            ),
            sample_count=SAMPLE_COUNT_DEFAULT,
            launches_scored=0,
            winrate_pct=None,
            net_ev_pct=None,
            optimal_tp_pct=None,
            robust_zone=[],
            rows=[],
            **_observed_fields(ath_profile),
        )
    else:
        qualification = _manual_stats_qualification(creator, ath_profile)
    return RuggerEvidence(
        rank=0,
        operator=creator,
        archetype=archetype,
        archetype_note=_archetype_note(
            archetype,
            entity_wallets,
            funding_summary,
            in_window_count,
            lifetime,
            max_creations,
        ),
        entity_wallets=entity_wallets,
        funding_summary=funding_summary,
        primary_funder=primary_funder,
        lifetime_creation_count=lifetime,
        in_window_launch_count=in_window_count,
        launch_cadence=cadence,
        ath_exit_profile=ath_profile,
        qualification=qualification,
        next_action=_next_action(archetype, creator, primary_funder),
    )


def _load_cached_enrichment(db: DatabaseManager, creator: str) -> dict[str, Any] | None:
    """Return a fresh cached entity enrichment payload, or None when stale/absent."""

    row = fetch_rugger_cache(db, creator)
    if row is None:
        return None
    enriched_at = _parse_iso(row.get("enriched_at"))
    if enriched_at is None:
        return None
    age = (dt.datetime.now(dt.UTC) - enriched_at).total_seconds()
    if age > CACHE_TTL_SECONDS:
        return None
    try:
        payload = json.loads(str(row["payload_json"]))
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    if not isinstance(payload, dict) or "entity_wallets" not in payload:
        return None
    return payload


def _enrich_seed(
    db: DatabaseManager,
    creator: str,
    in_window_count: int,
    since_iso: str | None,
    *,
    rpc_url: str,
    fallbacks: tuple[str, ...],
    lifetime: int,
    max_creations: int,
) -> RuggerEvidence:
    """Resolve one spam-capped creator's funding entity via live RPC (cached)."""

    cached = _load_cached_enrichment(db, creator)
    if cached is not None:
        payload = cached
    else:
        from rugbot.interfaces.cli.check_mint import (
            _build_funding_chain,
            _resolve_entity_wallets,
        )

        bundle_wallets = _block0_bundle_wallets(db, creator, since_iso)
        try:
            funding_rows, funding_summary = _build_funding_chain(
                [creator, *bundle_wallets], rpc_url, fallbacks
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("funding chain failed for %s: %s", creator, exc)
            funding_rows, funding_summary = [], None
        try:
            resolved = _resolve_entity_wallets(funding_rows, creator, bundle_wallets)
        except Exception as exc:  # noqa: BLE001
            logger.warning("entity resolve failed for %s: %s", creator, exc)
            resolved = [creator]
        entity_wallets = [w for w in resolved if isinstance(w, str) and w]
        if creator not in entity_wallets:
            entity_wallets.insert(0, creator)
        entity_wallets = entity_wallets[:ENTITY_WALLET_CAP]
        primary_funder = _primary_funder(funding_rows, set(entity_wallets))
        payload = {
            "entity_wallets": entity_wallets,
            "funding_summary": funding_summary,
            "primary_funder": primary_funder,
            "lifetime_creation_count": lifetime,
        }
        try:
            save_rugger_cache(db, wallet=creator, payload_json=json.dumps(payload))
            upsert_candidate(
                db,
                wallet=creator,
                launch_count=lifetime,
                winrate=None,
                best_tp=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("rugger cache save failed for %s: %s", creator, exc)

    entity_wallets = list(payload.get("entity_wallets") or [creator])
    funding_summary = payload.get("funding_summary")
    primary_funder = payload.get("primary_funder")
    archetype = _classify_archetype(entity_wallets, funding_summary)
    return _build_evidence(
        db,
        creator,
        in_window_count,
        since_iso,
        entity_wallets=entity_wallets,
        funding_summary=funding_summary if isinstance(funding_summary, str) else None,
        primary_funder=primary_funder if isinstance(primary_funder, str) else None,
        lifetime=lifetime,
        archetype=archetype,
        max_creations=max_creations,
    )


def _screen_lifetime_counts(
    creators: list[str], max_creations: int
) -> dict[str, int | None]:
    """Bounded-concurrency lifetime creation counts (early-stop at the spam cap).

    Fail-closed: a wallet whose count cannot be fetched maps to None (unknown) and
    is abstained rather than scored, so we never rank an unverifiable operator.
    """

    from rugbot.integrations.pumpfun_creator_index import (
        fetch_pumpfun_created_tokens,
    )

    def _count(creator: str) -> tuple[str, int | None]:
        try:
            tokens = fetch_pumpfun_created_tokens(creator, stop_after=max_creations + 1)
            return creator, len(tokens)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lifetime count failed for %s: %s", creator, exc)
            return creator, None

    result: dict[str, int | None] = {}
    with ThreadPoolExecutor(max_workers=FUNDING_COUNT_WORKERS) as executor:
        for creator, count in executor.map(_count, creators):
            result[creator] = count
    return result


def _rank_key(evidence: RuggerEvidence) -> tuple[int, int, str]:
    return (
        _STATUS_ORDER.get(evidence.qualification.status, 9),
        -evidence.in_window_launch_count,
        evidence.operator,
    )


def _finalize(evidence: list[RuggerEvidence], limit: int) -> list[RuggerEvidence]:
    evidence.sort(key=_rank_key)
    return [replace(item, rank=rank) for rank, item in enumerate(evidence[:limit], 1)]


def rank_ruggers(
    state_dir: Path | None = None,
    *,
    since: str | None = None,
    min_launches: int = 2,
    limit: int = 50,
    max_creations: int = MAX_CREATIONS_DEFAULT,
    use_rpc: bool = True,
) -> list[RuggerEvidence]:
    """Rank operator ENTITIES by discovery evidence (stats are manual).

    Opens ``.state/discover/rugbot.db``, recomputes launch metrics from persisted
    trades, seeds recently-active creators, then - with live RPC - excludes mass
    spammers by lifetime creation cap and resolves each survivor's funding
    entity/archetype. Winrate/EV are NOT auto-computed; each row carries the
    manual ``rug_check --score --entity`` command instead. Ranked by status,
    then in-window activity.

    ``use_rpc=False`` degrades to an honestly-labelled in-window-only view (no
    entity resolution, no spam screening beyond the local window).
    """

    if min_launches < MIN_LAUNCHES_FLOOR:
        raise ValueError(f"min_launches must be >= {MIN_LAUNCHES_FLOOR}")
    if not LIMIT_FLOOR <= limit <= LIMIT_CEILING:
        raise ValueError(f"limit must be between {LIMIT_FLOOR} and {LIMIT_CEILING}")
    if not 1 <= max_creations <= MAX_CREATIONS_CEILING:
        raise ValueError(f"max_creations must be between 1 and {MAX_CREATIONS_CEILING}")

    resolved_dir = get_discover_state_dir(state_dir)
    db = DatabaseManager(resolved_dir / "rugbot.db")
    try:
        ensure_discover_schema(db)
        recompute_launch_metrics(db)
        since_iso = (
            _parse_since(since).astimezone(dt.UTC).isoformat()
            if since is not None
            else None
        )
        conn = db.connection
        if since_iso is not None:
            seed_rows = conn.execute(
                "SELECT creator, COUNT(*) AS in_window FROM discover_launches "
                "WHERE created_at IS NOT NULL AND created_at >= ? "
                "GROUP BY creator HAVING COUNT(*) >= ? "
                "ORDER BY in_window DESC, creator ASC",
                (since_iso, min_launches),
            ).fetchall()
        else:
            seed_rows = conn.execute(
                "SELECT creator, COUNT(*) AS in_window FROM discover_launches "
                "GROUP BY creator HAVING COUNT(*) >= ? "
                "ORDER BY in_window DESC, creator ASC",
                (min_launches,),
            ).fetchall()
        seeds = [(str(row["creator"]), int(row["in_window"])) for row in seed_rows]

        rpc_url: str | None = None
        fallbacks: tuple[str, ...] = ()
        if use_rpc:
            from rugbot.runtime.config import load_provider_settings, resolve_dotenv

            resolve_dotenv()
            providers = load_provider_settings()
            rpc_url = providers.rpc_http
            fallbacks = providers.rpc_http_fallbacks
            if not rpc_url:
                logger.warning(
                    "SOLANA_RPC_HTTP unavailable -> falling back to in-window-only "
                    "ranking (no entity winrate); set it for bible qualification"
                )

        if not use_rpc or rpc_url is None:
            evidence = [
                _build_evidence(
                    db,
                    creator,
                    in_window,
                    since_iso,
                    entity_wallets=[creator],
                    funding_summary=None,
                    primary_funder=None,
                    lifetime=None,
                    archetype=TYPE1_ARCHETYPE,
                    status_override=STATUS_NO_RPC,
                    reason_override=(
                        "no_rpc_in_window_only (set SOLANA_RPC_HTTP / use_rpc for "
                        "entity resolution)"
                    ),
                    max_creations=max_creations,
                )
                for creator, in_window in seeds[:limit]
            ]
            return _finalize(evidence, limit)

        pool = seeds[:RPC_SEED_POOL_MAX]
        lifetime_map = _screen_lifetime_counts(
            [creator for creator, _ in pool], max_creations
        )
        evidence: list[RuggerEvidence] = []
        scored = 0
        spam_excluded = 0
        for creator, in_window in pool:
            lifetime = lifetime_map.get(creator)
            if lifetime is None:
                evidence.append(
                    _build_evidence(
                        db,
                        creator,
                        in_window,
                        since_iso,
                        entity_wallets=[creator],
                        funding_summary=None,
                        primary_funder=None,
                        lifetime=None,
                        archetype=TYPE1_ARCHETYPE,
                        status_override=STATUS_ABSTAIN,
                        reason_override="lifetime_count_unavailable (fail-closed)",
                        max_creations=max_creations,
                    )
                )
                continue
            if lifetime > max_creations:
                spam_excluded += 1
                evidence.append(
                    _build_evidence(
                        db,
                        creator,
                        in_window,
                        since_iso,
                        entity_wallets=[creator],
                        funding_summary=None,
                        primary_funder=None,
                        lifetime=lifetime,
                        archetype=MASS_SPAMMER_ARCHETYPE,
                        status_override=STATUS_MASS_SPAMMER,
                        reason_override=(
                            f"lifetime_creations {lifetime} > max_creations "
                            f"{max_creations} (bible 1 spam cap)"
                        ),
                        max_creations=max_creations,
                    )
                )
                continue
            if scored >= RPC_SCORE_MAX:
                evidence.append(
                    _build_evidence(
                        db,
                        creator,
                        in_window,
                        since_iso,
                        entity_wallets=[creator],
                        funding_summary=None,
                        primary_funder=None,
                        lifetime=lifetime,
                        archetype=TYPE1_ARCHETYPE,
                        status_override=STATUS_ABSTAIN,
                        reason_override="rpc_score_budget_exhausted",
                        max_creations=max_creations,
                    )
                )
                continue
            evidence.append(
                _enrich_seed(
                    db,
                    creator,
                    in_window,
                    since_iso,
                    rpc_url=rpc_url,
                    fallbacks=fallbacks,
                    lifetime=lifetime,
                    max_creations=max_creations,
                )
            )
            scored += 1
        logger.info(
            "rank_ruggers scored=%d spam_excluded=%d pool=%d",
            scored,
            spam_excluded,
            len(pool),
        )
        return _finalize(evidence, limit)
    finally:
        db.close()


__all__ = [
    "MASS_SPAMMER_ARCHETYPE",
    "STATUS_ABSTAIN",
    "STATUS_MASS_SPAMMER",
    "STATUS_NO_RPC",
    "STATUS_STATS_MANUAL",
    "TYPE1_ARCHETYPE",
    "TYPE2_ARCHETYPE",
    "TYPE2_NOTE",
    "AthExitProfile",
    "LaunchCadence",
    "Qualification",
    "RuggerEvidence",
    "rank_ruggers",
    "recompute_launch_metrics",
]
