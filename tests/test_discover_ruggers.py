"""Bible-corrected rugger ranking: ENTITY-level discovery evidence, stats manual.

These tests seed a temporary discover DB and monkeypatch the live-RPC seams at
their SOURCE modules (``rugbot.integrations.pumpfun_creator_index``,
``rugbot.interfaces.cli.check_mint``, ``rugbot.runtime.config``) — ``ruggers``
imports them lazily inside functions, so source-module patches take effect.

Asserted per the manual-stats contract (batch scoring proved flaky — rate-
limited metadata fetches fabricated 0% winrates, so the ranker never scores):
  (a) a wallet-switching entity (burners x1 token + recurrent funder) is
      resolved as ``type2_funding_cluster`` with status ``stats_manual`` and
      the manual ``rug_check --score --entity`` command in its message;
  (b) a 2201-creation wallet is excluded as ``mass_spammer``;
  (c) the batch scorer (``_score_entity_sync``) is NEVER invoked by the ranker;
  (d) no path fabricates winrate/EV — all stat fields stay None;
  (e) ranking is by status order, then in-window launch count.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import rugbot.integrations.pumpfun_creator_index as index_mod
import rugbot.interfaces.cli.check_mint as cm_mod
import rugbot.runtime.config as config_mod
from rugbot.discover.ruggers import (
    MASS_SPAMMER_ARCHETYPE,
    STATUS_ABSTAIN,
    STATUS_MASS_SPAMMER,
    STATUS_NO_RPC,
    STATUS_STATS_MANUAL,
    TYPE1_ARCHETYPE,
    TYPE2_ARCHETYPE,
    rank_ruggers,
    recompute_launch_metrics,
)
from rugbot.discover.store import (
    ensure_discover_schema,
    upsert_launch,
    upsert_trade,
)
from rugbot.storage.database import DatabaseManager

SWITCHER_DEV = "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ"
BURNER_B = "Burnr22222222222222222222222222222222222222"
BURNER_C = "Burnr33333333333333333333333333333333333333"
CEX_FUNDER = "CexHotWa11et4444444444444444444444444444444"
SPAMMER_DEV = "Spammr5555555555555555555555555555555555555"
ENTRY_PPM = 1_000_000


def _token(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        mint=f"MINT{index:04d}",
        creator="x",
        name=f"tok{index}",
        symbol=f"T{index}",
        created_timestamp=1_700_000_000 + index,
    )


def _install_rpc_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    counts: dict[str, object],
    funding: tuple[list[dict[str, object]], str | None] | None = None,
    entity: list[str] | None = None,
    rpc_url: str | None = "https://rpc.test",
) -> dict[str, list]:
    """Patch the lazy-imported RPC seams at their source modules.

    ``_score_entity_sync`` is poisoned: the ranker must NEVER auto-score
    (stats are manual by design).
    """

    calls: dict[str, list] = {"fetch": [], "funding": []}

    def fake_fetch(creator, *, timeout_seconds=15, stop_after=None):
        calls["fetch"].append(creator)
        value = counts.get(creator, 0)
        if isinstance(value, Exception):
            raise value
        total = min(int(value), stop_after) if stop_after is not None else int(value)
        return tuple(_token(i) for i in range(total))

    def fake_funding_chain(wallets, rpc_url_, fallbacks):
        calls["funding"].append(list(wallets))
        return funding if funding is not None else ([], None)

    def fake_resolve(funding_rows, target_wallet, bundle_wallets):
        if entity is not None:
            return list(entity)
        return [target_wallet, *bundle_wallets]

    def poisoned_score(wallets, rpc_url_, fallbacks, sample_count=10):
        raise AssertionError("ranker must never auto-score")  # noqa: TRY003

    monkeypatch.setattr(index_mod, "fetch_pumpfun_created_tokens", fake_fetch)
    monkeypatch.setattr(cm_mod, "_build_funding_chain", fake_funding_chain)
    monkeypatch.setattr(cm_mod, "_resolve_entity_wallets", fake_resolve)
    monkeypatch.setattr(cm_mod, "_score_entity_sync", poisoned_score)
    monkeypatch.setattr(config_mod, "resolve_dotenv", lambda **_: None)
    monkeypatch.setattr(
        config_mod,
        "load_provider_settings",
        lambda environment=None: SimpleNamespace(
            rpc_http=rpc_url, rpc_http_fallbacks=()
        ),
    )
    return calls


def _seed_launches(
    db: DatabaseManager,
    creator: str,
    count: int,
    *,
    slot_start: int,
) -> None:
    now = dt.datetime.now(dt.UTC)
    for index in range(count):
        upsert_launch(
            db,
            mint=f"M{creator[:6]}{index}",
            creator=creator,
            created_signature=f"sig{creator[:6]}{index}",
            created_slot=slot_start + index * 100,
            created_at=(now - dt.timedelta(minutes=10 * index)).isoformat(),
        )


def _seeded_db(tmp_path: Path, seeders) -> DatabaseManager:
    db = DatabaseManager(tmp_path / "rugbot.db")
    ensure_discover_schema(db)
    for seed in seeders:
        seed(db)
    return db


# --- (a) wallet-switching entity resolves as Type 2, stats manual --------------


def test_wallet_switching_entity_is_type2_stats_manual(tmp_path, monkeypatch) -> None:
    """3 burners + recurrent CEX funder -> type2 entity, manual-stats status.

    The entity (funding cluster) is fully resolved for discovery, but no
    winrate/EV is auto-computed — the row carries the manual ``rug_check``
    command instead.
    """

    db = _seeded_db(
        tmp_path,
        [
            lambda d: _seed_launches(d, SWITCHER_DEV, 2, slot_start=100),
        ],
    )
    db.close()
    funding_rows = [
        {"from": CEX_FUNDER, "to": SWITCHER_DEV, "slot": 50, "lamports": 2_495_000_000},
        {"from": CEX_FUNDER, "to": BURNER_B, "slot": 60, "lamports": 2_495_000_000},
        {"from": CEX_FUNDER, "to": BURNER_C, "slot": 70, "lamports": 2_495_000_000},
    ]
    _install_rpc_stubs(
        monkeypatch,
        counts={SWITCHER_DEV: 4},
        funding=(funding_rows, "2.495 SOL recurrent x3"),
        entity=[SWITCHER_DEV, BURNER_B, BURNER_C],
    )

    ranked = rank_ruggers(state_dir=tmp_path, min_launches=2)

    assert len(ranked) == 1
    top = ranked[0]
    assert top.rank == 1
    assert top.operator == SWITCHER_DEV
    assert top.archetype == TYPE2_ARCHETYPE
    assert top.entity_wallets == [SWITCHER_DEV, BURNER_B, BURNER_C]
    assert top.funding_summary == "2.495 SOL recurrent x3"
    assert top.primary_funder == CEX_FUNDER
    assert top.lifetime_creation_count == 4
    # Stats are manual: no fabricated winrate/EV, the command is surfaced.
    assert top.qualification.status == STATUS_STATS_MANUAL
    assert top.qualification.winrate_pct is None
    assert top.qualification.net_ev_pct is None
    assert top.qualification.optimal_tp_pct is None
    assert f"rug_check {SWITCHER_DEV} --score --entity" in top.qualification.message
    # Read-only (§7): recommends arming the funding source, never auto-arms.
    assert CEX_FUNDER in top.next_action
    assert "observe-only" in top.next_action
    assert "auto-arm" in top.next_action


# --- (b) the 2201-creation mass spammer is excluded -----------------------------


def test_mass_spammer_excluded_and_ranked_last(tmp_path, monkeypatch) -> None:
    db = _seeded_db(
        tmp_path,
        [
            lambda d: _seed_launches(d, SPAMMER_DEV, 6, slot_start=100),
            lambda d: _seed_launches(d, SWITCHER_DEV, 2, slot_start=1000),
        ],
    )
    db.close()
    _install_rpc_stubs(
        monkeypatch,
        counts={SPAMMER_DEV: 2201, SWITCHER_DEV: 4},
    )

    ranked = rank_ruggers(state_dir=tmp_path, min_launches=2)
    by_operator = {item.operator: item for item in ranked}

    spammer = by_operator[SPAMMER_DEV]
    assert spammer.archetype == MASS_SPAMMER_ARCHETYPE
    assert spammer.qualification.status == STATUS_MASS_SPAMMER
    # The early-stop screening saw only max_creations+1 tokens, not all 2201.
    assert spammer.lifetime_creation_count == 11
    assert "spam cap" in spammer.qualification.reason
    # The manual-stats entity ranks FIRST; the spammer ranks LAST.
    assert ranked[0].operator == SWITCHER_DEV
    assert ranked[-1].operator == SPAMMER_DEV


# --- (e) ranking: status order first, then in-window activity -------------------


def test_ranking_by_status_then_in_window_count(tmp_path, monkeypatch) -> None:
    high_count = "HighCnt888888888888888888888888888888888888"
    low_count = "LowCnt999999999999999999999999999999999999"
    failed = "Failed77777777777777777777777777777777777777"
    db = _seeded_db(
        tmp_path,
        [
            lambda d: _seed_launches(d, low_count, 2, slot_start=100),
            lambda d: _seed_launches(d, high_count, 6, slot_start=1000),
            lambda d: _seed_launches(d, failed, 8, slot_start=2000),
        ],
    )
    db.close()
    _install_rpc_stubs(
        monkeypatch,
        counts={
            high_count: 6,
            low_count: 3,
            failed: RuntimeError("RPC 429 rate-limited"),
        },
    )

    ranked = rank_ruggers(state_dir=tmp_path, min_launches=2)

    # stats_manual entities sort by in-window launches DESC; the count-failed
    # abstain ranks after both despite having the most in-window launches.
    assert [item.operator for item in ranked] == [high_count, low_count, failed]
    assert ranked[0].in_window_launch_count == 6
    assert ranked[2].qualification.status == STATUS_ABSTAIN


# --- honest no-RPC fallback ----------------------------------------------------


def test_no_rpc_fallback_is_labeled_in_window_only(tmp_path, monkeypatch) -> None:
    db = _seeded_db(
        tmp_path,
        [lambda d: _seed_launches(d, SWITCHER_DEV, 3, slot_start=100)],
    )
    db.close()
    calls = _install_rpc_stubs(monkeypatch, counts={SWITCHER_DEV: 3})

    ranked = rank_ruggers(state_dir=tmp_path, min_launches=2, use_rpc=False)

    assert len(ranked) == 1
    top = ranked[0]
    assert top.qualification.status == STATUS_NO_RPC
    assert top.archetype == TYPE1_ARCHETYPE
    assert top.in_window_launch_count == 3
    assert top.lifetime_creation_count is None
    assert top.entity_wallets == [SWITCHER_DEV]
    # Zero network seams touched in the no-RPC path.
    assert calls["fetch"] == []
    assert calls["funding"] == []


def test_missing_rpc_url_degrades_to_no_rpc(tmp_path, monkeypatch) -> None:
    db = _seeded_db(
        tmp_path,
        [lambda d: _seed_launches(d, SWITCHER_DEV, 2, slot_start=100)],
    )
    db.close()
    calls = _install_rpc_stubs(monkeypatch, counts={SWITCHER_DEV: 2}, rpc_url=None)

    ranked = rank_ruggers(state_dir=tmp_path, min_launches=2)

    assert [item.qualification.status for item in ranked] == [STATUS_NO_RPC]
    assert calls["fetch"] == []


# --- fail-closed on RPC errors -------------------------------------------------


def test_lifetime_count_failure_abstains(tmp_path, monkeypatch) -> None:
    db = _seeded_db(
        tmp_path,
        [lambda d: _seed_launches(d, SWITCHER_DEV, 2, slot_start=100)],
    )
    db.close()
    _install_rpc_stubs(
        monkeypatch,
        counts={SWITCHER_DEV: RuntimeError("RPC 429 rate-limited")},
    )

    ranked = rank_ruggers(state_dir=tmp_path, min_launches=2)

    assert len(ranked) == 1
    assert ranked[0].qualification.status == STATUS_ABSTAIN
    assert "lifetime_count_unavailable" in ranked[0].qualification.reason
    assert ranked[0].lifetime_creation_count is None
    # (d) fail-closed: no fabricated track record.
    assert ranked[0].qualification.winrate_pct is None
    assert ranked[0].qualification.net_ev_pct is None
    assert ranked[0].qualification.rows == []


# --- caching -------------------------------------------------------------------


def test_enrichment_is_cached_between_runs(tmp_path, monkeypatch) -> None:
    db = _seeded_db(
        tmp_path,
        [lambda d: _seed_launches(d, SWITCHER_DEV, 2, slot_start=100)],
    )
    db.close()
    calls = _install_rpc_stubs(
        monkeypatch,
        counts={SWITCHER_DEV: 4},
        funding=([], "2.495 SOL recurrent x3"),
        entity=[SWITCHER_DEV, BURNER_B],
    )

    first = rank_ruggers(state_dir=tmp_path, min_launches=2)
    assert first[0].entity_wallets == [SWITCHER_DEV, BURNER_B]
    assert len(calls["funding"]) == 1

    # Second run: even with a poisoned funding-chain seam, the cache is reused.
    def poisoned(wallets, rpc_url_, fallbacks):
        raise AssertionError("cache miss")  # noqa: TRY003

    monkeypatch.setattr(cm_mod, "_build_funding_chain", poisoned)
    second = rank_ruggers(state_dir=tmp_path, min_launches=2)
    assert second[0].entity_wallets == [SWITCHER_DEV, BURNER_B]
    assert second[0].qualification.status == STATUS_STATS_MANUAL
    assert len(calls["funding"]) == 1  # unchanged

    # The lightweight candidate cache mirrors lifetime only; stats stay NULL.
    check = DatabaseManager(tmp_path / "rugbot.db")
    try:
        row = check.connection.execute(
            "SELECT launch_count, winrate, best_tp FROM discover_candidates "
            "WHERE wallet = ?",
            (SWITCHER_DEV,),
        ).fetchone()
        assert row is not None
        assert row["launch_count"] == 4
        assert row["winrate"] is None
        assert row["best_tp"] is None
        cache_row = check.connection.execute(
            "SELECT payload_json FROM discover_rugger_cache WHERE wallet = ?",
            (SWITCHER_DEV,),
        ).fetchone()
        payload = json.loads(cache_row["payload_json"])
        assert payload["entity_wallets"] == [SWITCHER_DEV, BURNER_B]
        assert "score" not in payload
    finally:
        check.close()


# --- bounds, since-window, empty DB, metrics recompute -------------------------


def test_rank_ruggers_rejects_invalid_bounds(tmp_path) -> None:
    with pytest.raises(ValueError, match="min_launches"):
        rank_ruggers(state_dir=tmp_path, min_launches=0)
    with pytest.raises(ValueError, match="limit"):
        rank_ruggers(state_dir=tmp_path, limit=0)
    with pytest.raises(ValueError, match="limit"):
        rank_ruggers(state_dir=tmp_path, limit=501)
    with pytest.raises(ValueError, match="max_creations"):
        rank_ruggers(state_dir=tmp_path, max_creations=0)
    with pytest.raises(ValueError, match="max_creations"):
        rank_ruggers(state_dir=tmp_path, max_creations=101)


def test_rank_ruggers_min_launches_gate(tmp_path, monkeypatch) -> None:
    solo = "SoloDev111111111111111111111111111111111111"
    db = _seeded_db(
        tmp_path,
        [
            lambda d: _seed_launches(d, SWITCHER_DEV, 2, slot_start=100),
            lambda d: _seed_launches(d, solo, 1, slot_start=1000),
        ],
    )
    db.close()
    _install_rpc_stubs(monkeypatch, counts={SWITCHER_DEV: 2, solo: 1})

    at_two = {
        item.operator for item in rank_ruggers(state_dir=tmp_path, min_launches=2)
    }
    assert solo not in at_two
    at_one = {
        item.operator for item in rank_ruggers(state_dir=tmp_path, min_launches=1)
    }
    assert solo in at_one


def test_rank_ruggers_since_window_filters_launches(tmp_path, monkeypatch) -> None:
    db = _seeded_db(
        tmp_path,
        [lambda d: _seed_launches(d, SWITCHER_DEV, 2, slot_start=100)],
    )
    db.close()
    _install_rpc_stubs(monkeypatch, counts={SWITCHER_DEV: 2})

    assert rank_ruggers(state_dir=tmp_path, since="2099-01-01", min_launches=1) == []
    wide = rank_ruggers(state_dir=tmp_path, since="2000-01-01", min_launches=2)
    assert [item.operator for item in wide] == [SWITCHER_DEV]


def test_rank_ruggers_on_empty_db_returns_empty(tmp_path) -> None:
    assert rank_ruggers(state_dir=tmp_path, min_launches=2, use_rpc=False) == []


def test_recompute_launch_metrics_populates_evidence_columns(tmp_path) -> None:
    bundler = "Bundlr1111111111111111111111111111111111111"
    taker = "Taker2222222222222222222222222222222222222"
    dev = SWITCHER_DEV

    def seed(d: DatabaseManager) -> None:
        upsert_launch(
            d,
            mint="MNTA1",
            creator=dev,
            created_signature="sigA1",
            created_slot=100,
            created_at="2026-08-26T08:00:00+00:00",
        )
        for sig, slot, side, wallet, quote, price in (
            ("tA1e", 100, "buy", bundler, 10_000, ENTRY_PPM),
            ("tA1p", 120, "buy", taker, 20_000, 2_000_000),
            ("tA1b", 130, "sell", bundler, 5_000, 1_500_000),
            ("tA1d", 150, "sell", dev, 50_000, 1_200_000),
        ):
            upsert_trade(
                d,
                mint="MNTA1",
                signature=sig,
                event_index=0,
                slot=slot,
                side=side,
                wallet=wallet,
                quote_amount_base_units=quote,
                price_ppm=price,
                signers_json="[]",
            )

    db = _seeded_db(tmp_path, [seed])
    try:
        assert recompute_launch_metrics(db) >= 1
        row = db.connection.execute(
            "SELECT volume_lamports, ath_quote_lamports, dev_sell_slot, dump_slot, "
            "bundler_sell_count FROM discover_launches WHERE mint = 'MNTA1'"
        ).fetchone()
        assert row["volume_lamports"] == 30_000
        assert row["ath_quote_lamports"] == 2_000_000
        assert row["dev_sell_slot"] == 150
        assert row["dump_slot"] == 150
        assert row["bundler_sell_count"] == 1
    finally:
        db.close()
