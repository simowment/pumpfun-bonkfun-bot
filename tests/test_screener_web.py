"""Integration tests for Real-Time Screener Service and Web Interface Dashboard."""

from pathlib import Path

import base58
import pytest
from aiohttp.test_utils import TestClient, TestServer

from rugbot.ingest.pump.pump_stream import PumpPortalLaunchNotification
from rugbot.interfaces.web.adapter import create_web_app
from rugbot.runtime.app import build_ui_runtime
from rugbot.tracker.screener import ScreenerCandidateStatus


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def core_instance(tmp_path: Path):
    return build_ui_runtime(state_dir=tmp_path)


@pytest.mark.anyio
async def test_screener_service_direct(core_instance):
    """Test ScreenerService evaluation, candidate queue, accept and reject flows."""
    screener = core_instance.screener
    sample_mint = "E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4bN8pump"

    # 1. Scan and evaluate
    candidate = screener.scan_and_evaluate(sample_mint)
    assert candidate.creator_wallet != ""
    assert candidate.root_funder != ""
    assert candidate.optimal_tp_label != ""
    assert candidate.status in {
        ScreenerCandidateStatus.QUALIFIED,
        ScreenerCandidateStatus.PENDING_REVIEW,
    }

    # Verify candidate is queued
    all_candidates = screener.get_candidates()
    assert len(all_candidates) >= 1
    assert all_candidates[0].creator_wallet == candidate.creator_wallet

    # 2. Accept candidate -> should enroll into tracker repository and create policy
    accepted = screener.accept_candidate(
        candidate.creator_wallet, core_instance.service
    )
    assert accepted is not None
    assert accepted.status == ScreenerCandidateStatus.ACCEPTED

    # Verify funder in repository
    funder = core_instance.repository.get_funder(candidate.root_funder)
    assert funder is not None
    assert funder.enabled is True

    # Verify policy in repository
    policy = core_instance.repository.get_target_execution_policy(candidate.root_funder)
    assert policy is not None
    assert policy.monitoring_enabled is True
    assert policy.take_profit_pnl_ppm >= 0

    # 3. Reject candidate
    rejected = screener.reject_candidate(candidate.creator_wallet)
    assert rejected is not None
    assert rejected.status == ScreenerCandidateStatus.REJECTED


@pytest.mark.anyio
async def test_web_routes_and_dashboard(core_instance):
    """Test web dashboard rendering and REST API routes."""
    app = create_web_app(core_instance)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    try:
        # 1. Health endpoint
        res = await client.get("/api/health")
        assert res.status == 200
        data = await res.json()
        assert data["status"] == "ok"

        # 2. Web UI HTML endpoint
        res = await client.get("/")
        assert res.status == 200
        assert "text/html" in res.headers["Content-Type"]
        html = await res.text()
        assert "RUGBOT // LIVE SCREENER & REVIEW" in html
        assert "CANDIDATE DEVELOPER REVIEW QUEUE" in html

        # 3. Scan candidate via Web API
        sample_mint = "E9CqsGL5uXPASB853f87ox8nZVgW7ucoeYMC4bN8pump"
        res = await client.post("/api/screener/scan", json={"query": sample_mint})
        assert res.status == 200
        data = await res.json()
        assert data["ok"] is True
        assert "candidate" in data
        dev_wallet = data["candidate"]["creator_wallet"]

        # 4. State API reflects screener candidate
        res = await client.get("/api/state")
        assert res.status == 200
        state = await res.json()
        assert "screener" in state
        assert len(state["screener"]) >= 1

        # 5. Screener API
        res = await client.get("/api/screener")
        assert res.status == 200
        screener_list = await res.json()
        assert len(screener_list) >= 1

        # 6. Accept candidate via Web API
        res = await client.post("/api/screener/accept", json={"address": dev_wallet})
        assert res.status == 200
        accept_res = await res.json()
        assert accept_res["ok"] is True

        # Verify enrolled target in state
        res = await client.get("/api/state")
        state = await res.json()
        assert len(state["targets"]) >= 1

        # 7. Reject candidate via Web API
        res = await client.post("/api/screener/reject", json={"address": dev_wallet})
        assert res.status == 200
        reject_res = await res.json()
        assert reject_res["ok"] is True

    finally:
        await client.close()


@pytest.mark.anyio
async def test_screener_live_nomination_is_fail_closed(core_instance):
    """A provider trigger nominates a creator without becoming launch evidence."""
    screener = core_instance.screener
    sample_mint = base58.b58encode(bytes(range(32))).decode("ascii")
    creator = base58.b58encode(bytes(range(1, 33))).decode("ascii")
    signature = base58.b58encode(bytes(range(64))).decode("ascii")

    candidate = screener.nominate_live_launch(
        PumpPortalLaunchNotification(
            mint_pubkey=sample_mint,
            creator_pubkey=creator,
            signature=signature,
        )
    )

    assert candidate.samples == ()
    assert candidate.cluster_token_count == 0
    assert candidate.is_bible_qualified is False
    assert candidate.status == ScreenerCandidateStatus.PENDING_REVIEW
    assert "finalized completed outcomes" in candidate.qualification_reason

    # A third-party trigger is not canonical finalized launch history.
    persisted_launch = core_instance.repository.get_launch(sample_mint)
    assert persisted_launch is None
