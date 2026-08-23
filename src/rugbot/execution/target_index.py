"""Fast in-memory index for target policy matching on incoming launch events."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from rugbot.tracker.models import TargetExecutionMode, TargetExecutionPolicy

if TYPE_CHECKING:
    from collections.abc import Iterable


class TargetIndex:
    """Thread-safe O(1) in-memory target match index."""

    def __init__(self, policies: Iterable[TargetExecutionPolicy] = ()) -> None:
        self._policies: dict[str, TargetExecutionPolicy] = {
            p.funder_address: p for p in policies
        }
        self._lock = threading.Lock()

    def update_policy(self, policy: TargetExecutionPolicy) -> None:
        """Register or update an execution policy."""
        with self._lock:
            self._policies[policy.funder_address] = policy

    def remove_policy(self, funder_address: str) -> None:
        """Remove a policy from the active index."""
        with self._lock:
            self._policies.pop(funder_address, None)

    def match(self, address: str) -> TargetExecutionPolicy | None:
        """O(1) match of a developer wallet or root funder address."""
        with self._lock:
            policy = self._policies.get(address)
            if (
                policy is not None
                and policy.monitoring_enabled
                and policy.execution_mode != TargetExecutionMode.OFF
            ):
                return policy
            return None

    def get_all_active(self) -> tuple[TargetExecutionPolicy, ...]:
        """Return all active execution policies."""
        with self._lock:
            return tuple(
                p
                for p in self._policies.values()
                if p.monitoring_enabled and p.execution_mode != TargetExecutionMode.OFF
            )


__all__ = ["TargetIndex"]
