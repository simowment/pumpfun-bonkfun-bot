"""Integration tests for the real-time screener service."""

from pathlib import Path

import base58
import pytest

from rugbot.ingest.pump.pump_stream import PumpPortalLaunchNotification
from rugbot.runtime.app import build_ui_runtime
from rugbot.tracker.screener import ScreenerCandidateStatus


@pytest.fixture
def core_instance(tmp_path: Path):
    return build_ui_runtime(state_dir=tmp_path)


def test_screener_service_direct(core_instance) -> None:
    """Pending provider evidence must never enroll a tracker target."""

    sample_mint = "Anq6scgnxpMZvQN19XMSEUYQiDYuqNeh6cMZnN3Cpump"
    candidate = core_instance.screener.scan_and_evaluate(sample_mint)

    assert candidate.creator_wallet
    assert candidate.root_funder
    assert candidate.status in {
        ScreenerCandidateStatus.QUALIFIED,
        ScreenerCandidateStatus.PENDING_REVIEW,
    }
    history = core_instance.target_scan_history(candidate.creator_wallet)
    assert len(history) == 1
    assert history[0].tracking_address == candidate.creator_wallet
    assert history[0].query == sample_mint
    assert history[0].scan_ok is False
    accepted = core_instance.screener.accept_candidate(
        candidate.creator_wallet, core_instance.service
    )
    assert accepted is None
    assert core_instance.repository.get_funder(candidate.root_funder) is None
    assert (
        core_instance.repository.get_target_execution_policy(candidate.root_funder)
        is None
    )

    rejected = core_instance.screener.reject_candidate(candidate.creator_wallet)
    assert rejected is not None
    assert rejected.status == ScreenerCandidateStatus.REJECTED


def test_screener_live_nomination_is_fail_closed(core_instance) -> None:
    """A provider trigger is not canonical finalized launch history."""

    sample_mint = base58.b58encode(bytes(range(32))).decode("ascii")
    creator = base58.b58encode(bytes(range(1, 33))).decode("ascii")
    signature = base58.b58encode(bytes(range(64))).decode("ascii")

    candidate = core_instance.screener.nominate_live_launch(
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
    assert core_instance.repository.get_launch(sample_mint) is None
