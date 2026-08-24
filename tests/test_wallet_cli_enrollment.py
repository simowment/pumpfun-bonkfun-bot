"""Enrollment boundary tests for the wallet intelligence CLI."""

from __future__ import annotations

import json

from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.intelligence.token_resolver import ResolvedTarget
from rugbot.runtime import wallet_cli
from rugbot.storage.database import DatabaseManager
from rugbot.storage.tracker import SQLiteTrackerRepository


def test_enroll_rejects_target_without_repeat_finalized_evidence(
    monkeypatch, tmp_path, capsys
) -> None:
    """A single creator must not create any persistent watch state."""

    creator = "7SV5ocBq8EkKWsHH2ubB7yVAoTTdytt6FUVTeWw2GEbd"
    database_path = tmp_path / "tracker.db"
    monkeypatch.setenv("RUGBOT_DB_PATH", str(database_path))
    monkeypatch.setattr(
        wallet_cli,
        "resolve_token_or_wallet",
        lambda *_args, **_kwargs: ResolvedTarget(
            input_address="mint",
            target_wallet=creator,
            is_token=True,
            symbol="TEST",
            name="Test",
            default_label="Dev of Test ($TEST)",
        ),
    )

    async def abstain_scan(*_args, **_kwargs) -> AbstainResult:
        return AbstainResult(
            reason=AbstainReason.MISSING_FEATURE,
            message="No finalized repeat history",
            as_of_slot=-1,
        )

    monkeypatch.setattr(wallet_cli, "scan_wallet_intelligence", abstain_scan)

    assert wallet_cli.main(["mint", "--enroll", "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["enrolled"] is False
    assert output["enrollment_rejection_reason"] is not None

    repository = SQLiteTrackerRepository(DatabaseManager(database_path))
    assert repository.get_funder(creator) is None
    assert repository.get_target_execution_policy(creator) is None
