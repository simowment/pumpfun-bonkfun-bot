"""Observe-only execution port."""

from rugbot.execution.ports import (
    ExecutionIntent,
    ExecutionMode,
    ExecutionReceipt,
    non_submitting_receipt,
    validate_execution_intent,
)


class ObserveExecutionPort:
    """Execution port that records decisions but cannot submit transactions."""

    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        """Return a non-submitting receipt for an observed intent."""

        intent_error = validate_execution_intent(intent)
        if intent_error is not None:
            return non_submitting_receipt(
                mode=ExecutionMode.OBSERVE,
                intent=(intent if isinstance(intent, ExecutionIntent) else None),
                message=intent_error,
            )
        return non_submitting_receipt(
            mode=ExecutionMode.OBSERVE,
            intent=intent,
            message="observe mode records intent only",
        )
