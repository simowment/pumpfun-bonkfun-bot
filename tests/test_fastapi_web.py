"""Integration coverage for the FastAPI tracker surface."""

from pathlib import Path
from types import SimpleNamespace

from starlette.testclient import TestClient

import rugbot.integrations.pumpfun_creator_index as index_mod
import rugbot.interfaces.cli.check_mint as cm_mod
import rugbot.runtime.config as config_mod
from rugbot.application.commands import CommandResult
from rugbot.discover.store import (
    ensure_discover_schema,
    upsert_launch,
    upsert_trade,
)
from rugbot.interfaces.web.fastapi_app import create_fastapi_app
from rugbot.runtime.app import build_ui_runtime
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository
from rugbot.tracker.models import (
    EntityBackfillRecord,
    EntityBackfillStatus,
    TargetScanRecord,
)


def test_entity_backfill_checkpoint_survives_database_reopen(tmp_path: Path) -> None:
    """Persist the cursor, cached report, and resumable status in real SQLite."""

    database_path = tmp_path / "rugbot.db"
    first_database = DatabaseManager(database_path)
    first_repository = SQLiteTrackerRepository(first_database)
    saved = EntityBackfillRecord(
        query="9BnKqsHE5WxUSo6XJxzpTgH4Nj7UH3UrbshymMYsBjL8",
        wallet="9BnKqsHE5WxUSo6XJxzpTgH4Nj7UH3UrbshymMYsBjL8",
        requested_transactions=100,
        cached_transactions=17,
        before_signature="5" * 88,
        status=EntityBackfillStatus.RATE_LIMITED,
        message="getTransaction was rate-limited",
        report_json='{"status":"ok"}',
        created_at="2026-08-24T05:00:00+00:00",
        updated_at="2026-08-24T05:01:00+00:00",
    )
    first_repository.save_entity_backfill(saved)
    first_database.close()

    reopened_database = DatabaseManager(database_path)
    reopened_repository = SQLiteTrackerRepository(reopened_database)
    restored = reopened_repository.get_entity_backfill(saved.query)
    resumable = reopened_repository.get_incomplete_entity_backfills()
    reopened_database.close()

    assert restored == saved
    assert resumable == (saved,)


def test_exhausted_cached_history_is_reported_as_finished(tmp_path: Path) -> None:
    """Distinguish an exhausted six-row history from a stalled 6/20 backfill."""

    core = build_ui_runtime(state_dir=tmp_path)
    backfill = EntityBackfillRecord(
        query="8KiXkQXRYcFKVYuyioFqdxs6cK6k1qPigPTaEHRmpump",
        wallet="D8V1T12tYwtwn3yxihzGnAN3ar3Jr9pBrbByj6qmT6Rw",
        requested_transactions=20,
        cached_transactions=6,
        before_signature="5" * 88,
        status=EntityBackfillStatus.COMPLETE,
        message="finalized history cached: 6/20",
        report_json='{"status":"ok"}',
        created_at="2026-08-24T05:00:00+00:00",
        updated_at="2026-08-24T05:01:00+00:00",
    )
    core.repository.save_entity_backfill(backfill)

    result = core.cached_entity_report(backfill.query)
    data = result.data

    assert result.ok is True
    assert result.message == (
        "finalized history exhausted: all 6 available transactions cached"
    )
    assert isinstance(data, dict)
    assert data["backfill"] == {
        "status": "complete",
        "cached_transactions": 6,
        "history_transactions": 6,
        "requested_transactions": 20,
        "progress_percent": 100,
        "history_exhausted": True,
        "resumable": False,
        "message": "finalized history exhausted: all 6 available transactions cached",
        "updated_at": "2026-08-24T05:01:00+00:00",
    }


def test_target_scan_history_survives_database_reopen(tmp_path: Path) -> None:
    database_path = tmp_path / "rugbot.db"
    first_database = DatabaseManager(database_path)
    first_repository = SQLiteTrackerRepository(first_database)
    first_repository.save_target_scan(
        TargetScanRecord(
            query="9BnKqsHE5WxUSo6XJxzpTgH4Nj7UH3UrbshymMYsBjL8",
            tracking_address="9BnKqsHE5WxUSo6XJxzpTgH4Nj7UH3UrbshymMYsBjL8",
            token_symbol=None,
            token_name=None,
            scan_ok=True,
            launch_count=0,
            linked_launch_count=0,
            repeat_bundler_mint_count=0,
            message="finalized scan complete",
            first_scanned_at="2026-08-24T05:00:00+00:00",
            last_scanned_at="2026-08-24T05:00:00+00:00",
        )
    )
    first_database.close()

    reopened_database = DatabaseManager(database_path)
    reopened_repository = SQLiteTrackerRepository(reopened_database)
    history = reopened_repository.get_target_scans()
    reopened_database.close()

    assert len(history) == 1
    assert history[0].query == "9BnKqsHE5WxUSo6XJxzpTgH4Nj7UH3UrbshymMYsBjL8"
    assert history[0].scan_count == 1


def test_target_scan_history_appends_and_filters_by_entity(tmp_path: Path) -> None:
    """Each scan remains queryable after later scans for the same entity."""

    database = DatabaseManager(tmp_path / "rugbot.db")
    repository = SQLiteTrackerRepository(database)
    entity = "9BnKqsHE5WxUSo6XJxzpTgH4Nj7UH3UrbshymMYsBjL8"
    other_entity = "8KiXkQXRYcFKVYuyioFqdxs6cK6k1qPigPTaEHRmpump"
    for query, address, message in (
        (entity, entity, "first scan"),
        (entity, entity, "second scan"),
        (other_entity, other_entity, "other entity"),
    ):
        repository.save_target_scan(
            TargetScanRecord(
                query=query,
                tracking_address=address,
                token_symbol=None,
                token_name=None,
                scan_ok=True,
                launch_count=1,
                linked_launch_count=0,
                repeat_bundler_mint_count=0,
                message=message,
                first_scanned_at=f"2026-08-24T05:0{len(message)}:00+00:00",
                last_scanned_at=f"2026-08-24T05:0{len(message)}:00+00:00",
            )
        )

    history = repository.get_target_scans_for_entity(entity)
    database.close()

    assert [scan.message for scan in history] == ["second scan", "first scan"]
    assert [scan.scan_count for scan in history] == [2, 1]
    assert all(scan.tracking_address == entity for scan in history)


def test_target_scan_schema_removes_verdict_columns_without_losing_history(
    tmp_path: Path,
) -> None:
    """Drop obsolete qualification columns while preserving factual scan history."""

    database = DatabaseManager(tmp_path / "rugbot.db")
    database.connection.executescript(
        """
        CREATE TABLE tracker_target_scans (
            query TEXT PRIMARY KEY,
            tracking_address TEXT,
            token_symbol TEXT,
            token_name TEXT,
            scan_ok INTEGER NOT NULL,
            tracking_eligible INTEGER NOT NULL,
            assessment TEXT NOT NULL,
            launch_count INTEGER NOT NULL,
            linked_launch_count INTEGER NOT NULL,
            repeat_bundler_mint_count INTEGER NOT NULL,
            message TEXT NOT NULL,
            first_scanned_at TEXT NOT NULL,
            last_scanned_at TEXT NOT NULL,
            scan_count INTEGER NOT NULL
        );
        INSERT INTO tracker_target_scans VALUES (
            'target', 'wallet', NULL, NULL, 1, 0,
            'insufficient_repeat_operator_evidence', 3, 2, 1,
            'complete', '2026-08-24T05:00:00+00:00',
            '2026-08-24T05:01:00+00:00', 4
        );
        """
    )

    repository = SQLiteTrackerRepository(database)
    columns = {
        str(row["name"])
        for row in database.connection.execute(
            "PRAGMA table_info(tracker_target_scans)"
        )
    }
    history = repository.get_target_scans()
    database.close()

    assert "tracking_eligible" not in columns
    assert "assessment" not in columns
    assert len(history) == 1
    assert history[0].query == "target"
    assert history[0].launch_count == 3
    assert history[0].scan_count == 4


def test_fastapi_exposes_entity_scan_history(tmp_path: Path) -> None:
    """Expose persisted per-entity scan events through the web boundary."""

    entity = "9BnKqsHE5WxUSo6XJxzpTgH4Nj7UH3UrbshymMYsBjL8"
    core = build_ui_runtime(state_dir=tmp_path)
    core.repository.save_target_scan(
        TargetScanRecord(
            query=entity,
            tracking_address=entity,
            token_symbol="TEST",  # noqa: S106 - fixture label, not a secret.
            token_name="Test token",  # noqa: S106 - fixture label, not a secret.
            scan_ok=False,
            launch_count=0,
            linked_launch_count=0,
            repeat_bundler_mint_count=0,
            message="pending finalized evidence",
            first_scanned_at="2026-08-24T05:00:00+00:00",
            last_scanned_at="2026-08-24T05:00:00+00:00",
        )
    )
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        response = client.get(f"/api/entity/{entity}/scans")

    assert response.status_code == 200
    assert response.json()["entity_address"] == entity
    assert response.json()["scans"][0]["message"] == "pending finalized evidence"
    assert response.json()["scans"][0]["scan_ok"] is False


def test_fastapi_exposes_persisted_state_without_seed_data(tmp_path: Path) -> None:
    core = build_ui_runtime(state_dir=tmp_path)
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        state = client.get("/api/state")
        assert state.status_code == 200
        payload = state.json()
        assert payload["observation"]["status"] in {
            "connecting",
            "disconnected",
            "pumpportal_live",
            "wss_live",
        }
        payload["observation"]["status"] = "configured"
        assert payload == {
            "target_history": [],
            "launches": [],
            "observation": {"status": "configured", "addresses": []},
        }


def test_fastapi_state_excludes_cached_report_bodies(tmp_path: Path) -> None:
    """Initial UI state carries progress metadata, not full cached analyses."""

    core = build_ui_runtime(state_dir=tmp_path)
    core.repository.save_entity_backfill(
        EntityBackfillRecord(
            query="8KiXkQXRYcFKVYuyioFqdxs6cK6k1qPigPTaEHRmpump",
            wallet="D8V1T12tYwtwn3yxihzGnAN3ar3Jr9pBrbByj6qmT6Rw",
            requested_transactions=100,
            cached_transactions=61,
            before_signature=None,
            status=EntityBackfillStatus.COMPLETE,
            message="finalized history cached",
            report_json='{"graph":"' + ("x" * 300_000) + '"}',
            created_at="2026-08-24T05:00:00+00:00",
            updated_at="2026-08-24T05:01:00+00:00",
        )
    )
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        response = client.get("/api/state")

    assert response.status_code == 200
    assert len(response.content) < 5_000
    assert "entity_backfills" not in response.json()


def test_cached_entity_report_adds_real_pump_bonding_curve(tmp_path: Path) -> None:
    """Older cached mint rows receive their deterministic Axiom market identity."""

    mint = "7V1XwAbQntvcKLgAH8aNWSBf6PnFCjEH6w7Bzic4pump"
    core = build_ui_runtime(state_dir=tmp_path)
    core.repository.save_entity_backfill(
        EntityBackfillRecord(
            query=mint,
            wallet="4jPMW7KgFyJwNbE7sb2hJHPy7XBYB48FEhd3vZkCk61i",
            requested_transactions=20,
            cached_transactions=20,
            before_signature=None,
            status=EntityBackfillStatus.COMPLETE,
            message="finalized history cached",
            report_json=(
                '{"entity_mints":[{"mint":"'
                + mint
                + '"}],"identity":{"input":"'
                + mint
                + '","is_token":true}}'
            ),
            created_at="2026-08-24T05:00:00+00:00",
            updated_at="2026-08-24T05:01:00+00:00",
        )
    )
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        response = client.get(f"/api/entity/cache?query={mint}")

    assert response.status_code == 200
    expected = "Aj6ptkQH1rzZvc1EnA9t6vB7gZNoPNXhfm2pRTbGgybk"
    assert response.json()["data"]["entity_mints"][0]["bonding_curve"] == expected
    assert response.json()["data"]["identity"]["bonding_curve"] == expected


def test_cached_entity_report_resolves_via_tracking_address(tmp_path: Path) -> None:
    """A report cached under a mint query is retrievable by its resolved entity."""

    mint = "7V1XwAbQntvcKLgAH8aNWSBf6PnFCjEH6w7Bzic4pump"
    creator = "4jPMW7KgFyJwNbE7sb2hJHPy7XBYB48FEhd3vZkCk61i"
    core = build_ui_runtime(state_dir=tmp_path)
    core.repository.save_entity_backfill(
        EntityBackfillRecord(
            query=mint,
            wallet=creator,
            requested_transactions=10,
            cached_transactions=10,
            before_signature=None,
            status=EntityBackfillStatus.COMPLETE,
            message="finalized history cached: 10/10",
            report_json=(
                '{"identity":{"input":"'
                + mint
                + '","is_token":true,"resolved_creator":"'
                + creator
                + '"},"tracking_address":"'
                + creator
                + '"}'
            ),
            created_at="2026-08-24T05:00:00+00:00",
            updated_at="2026-08-24T05:01:00+00:00",
        )
    )
    core.repository.save_target_scan(
        TargetScanRecord(
            query=mint,
            tracking_address=creator,
            token_symbol="Unfazed",  # noqa: S106 - test fixture label
            token_name="The Unfazed Hawk",  # noqa: S106 - test fixture label
            scan_ok=True,
            launch_count=0,
            linked_launch_count=0,
            repeat_bundler_mint_count=0,
            message="finalized history cached: 10/10",
            first_scanned_at="2026-08-24T05:01:00+00:00",
            last_scanned_at="2026-08-24T05:01:00+00:00",
        )
    )
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        response = client.get(f"/api/entity/cache?query={creator}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["tracking_address"] == creator
    assert data["identity"]["resolved_creator"] == creator


def test_fastapi_has_no_seeded_cluster_or_token_routes(tmp_path: Path) -> None:
    core = build_ui_runtime(state_dir=tmp_path)
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        assert client.get("/api/cluster/target").status_code == 404
        assert client.get("/api/tokens/recent").status_code == 404
        response = client.post("/api/entity/backtest", json={})

    assert response.status_code == 409
    assert "would be fabricated" in response.json()["detail"]


def test_fastapi_rejects_unresolved_tracking(tmp_path: Path) -> None:
    core = build_ui_runtime(state_dir=tmp_path)
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        response = client.post(
            "/api/entity/track",
            json={
                "address": "5SW7p56x22LKj8gYcE8DVVd1S59UJUGR9jKq2PFdKiKg",
                "label": "unresolved",
            },
        )

    assert response.status_code == 409
    assert "must be resolved" in response.json()["detail"]


def test_fastapi_returns_finalized_wallet_balance(tmp_path: Path, monkeypatch) -> None:
    """Expose the selected wallet's validated finalized SOL balance."""

    address = "5SW7p56x22LKj8gYcE8DVVd1S59UJUGR9jKq2PFdKiKg"
    core = build_ui_runtime(state_dir=tmp_path)

    async def wallet_balance(requested_address: str) -> CommandResult:
        assert requested_address == address
        return CommandResult(
            ok=True,
            message="finalized SOL balance loaded",
            data={"address": address, "balance_lamports": 1_250_000_000, "slot": 42},
        )

    monkeypatch.setattr(core, "wallet_balance", wallet_balance)
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        response = client.get(f"/api/wallet/balance?address={address}")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "address": address,
        "balance_lamports": 1_250_000_000,
        "slot": 42,
    }


def test_fastapi_tracks_resolved_address_without_qualification(
    tmp_path: Path, monkeypatch
) -> None:
    """Tracking requires resolved identity, not a behavioral verdict."""

    address = "5SW7p56x22LKj8gYcE8DVVd1S59UJUGR9jKq2PFdKiKg"
    core = build_ui_runtime(state_dir=tmp_path)

    async def analyze_wallet(*_args, **_kwargs) -> CommandResult:
        return CommandResult(
            ok=True,
            message="resolved",
            data={"tracking_address": address},
        )

    async def refresh_observation() -> None:
        return None

    monkeypatch.setattr(core, "analyze_wallet", analyze_wallet)
    monkeypatch.setattr(core, "refresh_observation", refresh_observation)
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        scan = client.post(
            "/api/entity/scan",
            json={"query": address, "max_transactions": 20},
        )
        track = client.post(
            "/api/entity/track",
            json={"address": address, "label": "resolved target"},
        )

    assert scan.status_code == 200
    assert track.status_code == 200
    assert "observe-only" in track.json()["message"]


def test_fastapi_websocket_starts_with_real_empty_state(tmp_path: Path) -> None:
    core = build_ui_runtime(state_dir=tmp_path)
    app = create_fastapi_app(core)

    with TestClient(app) as client, client.websocket_connect("/api/events") as socket:
        message = socket.receive_json()

    assert message["type"] == "state"
    assert message["data"]["launches"] == []
    assert message["data"]["observation"]["status"] in {
        "connecting",
        "disconnected",
        "pumpportal_live",
        "wss_live",
    }


# --- Autonomous rugger discovery endpoint (GET /api/discover/ruggers) ---------
# The discover DB lives at the CWD-relative ``.state/discover/rugbot.db`` (see
# ``get_discover_state_dir``), independent of the tracker ``state_dir``; the web
# handler delegates to ``RugbotApp.discover_ruggers`` -> ``rank_ruggers()`` with
# no state_dir, so these tests chdir into ``tmp_path`` and seed that location.

_RUGGER_SERIAL_DEV = "CvoPbuS2AghzVBYJx7HfQGhALiqif4YwWgHvXmhehuJZ"
_RUGGER_OTHER_DEV = "FJz6SLz8CQBmm692kfp6e8s9FZPuqnKX5ZNcr7k5Kadd"
_RUGGER_TAKER = "Taker2222222222222222222222222222222222222"


def _seed_discover_ruggers(db: DatabaseManager) -> None:
    """Seed a 6-launch serial deployer (3 dev dumps) + a 2-launch dev."""

    ensure_discover_schema(db)
    for index, slot in enumerate([100, 200, 300, 400, 500, 600], start=1):
        upsert_launch(
            db,
            mint=f"MNTA{index}",
            creator=_RUGGER_SERIAL_DEV,
            created_signature=f"sigA{index}",
            created_slot=slot,
            created_at=f"2026-08-26T08:{(index - 1) * 10:02d}:00+00:00",
        )
    # Peak prices give 2x/3x/4x/2x/5x multipliers; MNTA4 carries no trades.
    for mint, slot, peak_ppm, dump_slot in (
        ("MNTA1", 100, 2_000_000, 150),
        ("MNTA2", 200, 3_000_000, None),
        ("MNTA3", 300, 4_000_000, 350),
        ("MNTA5", 500, 2_000_000, 550),
        ("MNTA6", 600, 5_000_000, None),
    ):
        upsert_trade(
            db,
            mint=mint,
            signature=f"{mint}e",
            event_index=0,
            slot=slot,
            side="buy",
            wallet=_RUGGER_TAKER,
            quote_amount_base_units=10_000,
            price_ppm=1_000_000,
            signers_json="[]",
        )
        upsert_trade(
            db,
            mint=mint,
            signature=f"{mint}p",
            event_index=1,
            slot=slot + 20,
            side="buy",
            wallet=_RUGGER_TAKER,
            quote_amount_base_units=20_000,
            price_ppm=peak_ppm,
            signers_json="[]",
        )
        if dump_slot is not None:
            upsert_trade(
                db,
                mint=mint,
                signature=f"{mint}d",
                event_index=2,
                slot=dump_slot,
                side="sell",
                wallet=_RUGGER_SERIAL_DEV,
                quote_amount_base_units=30_000,
                price_ppm=1_500_000,
                signers_json="[]",
            )
    for index, (mint, slot, minute) in enumerate(
        [("MNTB1", 1000, "09:00"), ("MNTB2", 1100, "09:30")], start=1
    ):
        upsert_launch(
            db,
            mint=mint,
            creator=_RUGGER_OTHER_DEV,
            created_signature=f"sigB{index}",
            created_slot=slot,
            created_at=f"2026-08-26T{minute}:00+00:00",
        )


def _stub_ruggers_rpc(monkeypatch, counts: dict) -> None:
    """Patch the lazy-imported RPC seams at their source modules (no network)."""

    def fake_fetch(creator, *, timeout_seconds=15, stop_after=None):
        value = counts.get(creator, 0)
        if isinstance(value, Exception):
            raise value
        total = min(int(value), stop_after) if stop_after is not None else int(value)
        return tuple(
            SimpleNamespace(
                mint=f"MINT{i:04d}",
                creator=creator,
                name=f"tok{i}",
                symbol=f"T{i}",
                created_timestamp=1_700_000_000 + i,
            )
            for i in range(total)
        )

    def poisoned_score(wallets, rpc_url, fallbacks, sample_count=10):
        raise AssertionError("ranker must never auto-score")  # noqa: TRY003

    monkeypatch.setattr(index_mod, "fetch_pumpfun_created_tokens", fake_fetch)
    monkeypatch.setattr(cm_mod, "_build_funding_chain", lambda w, r, f: ([], None))
    monkeypatch.setattr(
        cm_mod,
        "_resolve_entity_wallets",
        lambda rows, target, bundle: [target, *bundle],
    )
    monkeypatch.setattr(cm_mod, "_score_entity_sync", poisoned_score)
    monkeypatch.setattr(config_mod, "resolve_dotenv", lambda **_: None)
    monkeypatch.setattr(
        config_mod,
        "load_provider_settings",
        lambda environment=None: SimpleNamespace(
            rpc_http="https://rpc.test", rpc_http_fallbacks=()
        ),
    )


def test_api_discover_ruggers_ranks_serial_deployers(
    tmp_path: Path, monkeypatch
) -> None:
    """GET /api/discover/ruggers returns the bible evidence contract (mocked RPC)."""

    monkeypatch.chdir(tmp_path)
    discover_dir = tmp_path / ".state" / "discover"
    discover_dir.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(discover_dir / "rugbot.db")
    _seed_discover_ruggers(db)
    db.close()
    # The serial dev is a legitimate operator (6 lifetime creations, under the
    # cap); the other dev's lifetime count fails -> fail-closed abstain.
    _stub_ruggers_rpc(
        monkeypatch,
        {
            _RUGGER_SERIAL_DEV: 6,
            _RUGGER_OTHER_DEV: RuntimeError("RPC 429 rate-limited"),
        },
    )

    core = build_ui_runtime(state_dir=tmp_path)
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        response = client.get("/api/discover/ruggers?min_launches=2&limit=50")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["type2_note"]  # entity model explained, never fabricated

    ruggers = body["ruggers"]
    assert [row["operator"] for row in ruggers] == [
        _RUGGER_SERIAL_DEV,
        _RUGGER_OTHER_DEV,
    ]

    top = ruggers[0]
    assert top["rank"] == 1
    # The seed carries block-0 (same-slot) taker buys, so the entity resolves
    # to {dev, bundler} -> Type 2 funding cluster (bible §3.2 wallet-switching).
    assert top["archetype"] == "type2_funding_cluster"
    # Bible-corrected contract: honest lifetime vs in-window counts.
    assert top["lifetime_creation_count"] == 6
    assert top["in_window_launch_count"] == 6
    assert top["entity_wallets"][0] == _RUGGER_SERIAL_DEV
    assert _RUGGER_TAKER in top["entity_wallets"]
    assert top["launch_cadence"]["median_interval_minutes"] == 10.0
    assert top["ath_exit_profile"]["dev_dump_count"] == 3
    assert top["ath_exit_profile"]["peak_ath_multiplier_ppm"] == 5_000_000
    # Stats are manual by design: no fabricated winrate/EV, the row carries
    # the manual rug_check command instead.
    assert top["qualification"]["status"] == "stats_manual"
    assert top["qualification"]["winrate_pct"] is None
    assert top["qualification"]["net_ev_pct"] is None
    assert top["qualification"]["optimal_tp_pct"] is None
    assert (
        f"rug_check {_RUGGER_SERIAL_DEV} --score --entity"
        in top["qualification"]["message"]
    )
    # Read-only recommendation, never an auto-arm (§7).
    assert "observe-only" in top["next_action"]

    # The count-failed dev abstains rather than fabricating a track record.
    second = ruggers[1]
    assert second["qualification"]["status"] == "abstain"
    assert "lifetime_count_unavailable" in second["qualification"]["reason"]


def test_api_discover_ruggers_no_rpc_fallback(tmp_path: Path, monkeypatch) -> None:
    """no_rpc=true degrades to the honest in-window-only view (zero network)."""

    monkeypatch.chdir(tmp_path)
    discover_dir = tmp_path / ".state" / "discover"
    discover_dir.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(discover_dir / "rugbot.db")
    _seed_discover_ruggers(db)
    db.close()

    core = build_ui_runtime(state_dir=tmp_path)
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        response = client.get("/api/discover/ruggers?min_launches=2&no_rpc=true")

    assert response.status_code == 200
    ruggers = response.json()["ruggers"]
    assert [row["operator"] for row in ruggers] == [
        _RUGGER_SERIAL_DEV,
        _RUGGER_OTHER_DEV,
    ]
    top = ruggers[0]
    assert top["qualification"]["status"] == "no_rpc_in_window_only"
    assert top["lifetime_creation_count"] is None
    assert top["in_window_launch_count"] == 6


def test_api_discover_ruggers_validates_bounds(tmp_path: Path, monkeypatch) -> None:
    """Out-of-range params are rejected 422 before any DB access."""

    monkeypatch.chdir(tmp_path)
    core = build_ui_runtime(state_dir=tmp_path)
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        assert client.get("/api/discover/ruggers?min_launches=0").status_code == 422
        assert client.get("/api/discover/ruggers?limit=0").status_code == 422
        assert client.get("/api/discover/ruggers?limit=501").status_code == 422
        assert client.get("/api/discover/ruggers?max_creations=0").status_code == 422
        assert client.get("/api/discover/ruggers?max_creations=101").status_code == 422
