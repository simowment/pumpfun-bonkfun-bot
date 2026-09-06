"""Mocked RPC budget and trade-poll default tests (no network)."""

from __future__ import annotations

from rugbot.discover import collector, ruggers
from rugbot.discover.store import ensure_discover_schema, upsert_launch
from rugbot.runtime.config import (
    DEFAULT_MAX_RPC_CALLS_PER_COMMAND,
    resolve_max_rpc_calls_per_command,
)
from rugbot.storage.database import DatabaseManager


def test_resolve_max_rpc_calls_defaults() -> None:
    """Missing env returns the named default budget."""
    assert resolve_max_rpc_calls_per_command({}) == 25
    assert DEFAULT_MAX_RPC_CALLS_PER_COMMAND == 25


def test_resolve_max_rpc_calls_override() -> None:
    """A valid env integer overrides the default budget."""
    assert resolve_max_rpc_calls_per_command({"RUGBOT_MAX_RPC_CALLS": "10"}) == 10


def test_resolve_max_rpc_calls_garbage_fail_closed() -> None:
    """Garbage, empty, or non-positive values fail closed to the default."""
    for raw in ("garbage", "", "   ", "0", "-5", "1.5"):
        assert (
            resolve_max_rpc_calls_per_command({"RUGBOT_MAX_RPC_CALLS": raw})
            == DEFAULT_MAX_RPC_CALLS_PER_COMMAND
        )


def test_trade_poll_default_off(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Trade polling is off unless the env opt-in is set."""
    monkeypatch.delenv("RUGBOT_DISCOVER_TRADE_POLL_ENABLED", raising=False)
    assert collector._discover_trade_poll_enabled() is False


def test_trade_poll_opt_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Setting RUGBOT_DISCOVER_TRADE_POLL_ENABLED=1 re-enables polling."""
    monkeypatch.setenv("RUGBOT_DISCOVER_TRADE_POLL_ENABLED", "1")
    assert collector._discover_trade_poll_enabled() is True


def test_budget_exceeded_returns_partial_with_note(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Over-limit ranking degrades to a partial list with an honest note.

    Uses ``use_rpc=False`` (no network): rows beyond ``limit`` are cut and
    every row carries the in-window-only abstain note instead of RPC evidence.
    """
    db = DatabaseManager(tmp_path / "rugbot.db")
    try:
        ensure_discover_schema(db)
        for index in range(5):
            upsert_launch(
                db,
                mint=f"mint{index:02d}",
                creator=f"creator{index:02d}",
                created_signature=f"sig{index:02d}",
                created_slot=index,
            )
    finally:
        db.close()

    evidence = ruggers.rank_ruggers(tmp_path, min_launches=1, limit=2, use_rpc=False)
    assert len(evidence) == 2
    assert all(item.qualification.status == ruggers.STATUS_NO_RPC for item in evidence)
    assert all("in_window_only" in item.qualification.reason for item in evidence)
