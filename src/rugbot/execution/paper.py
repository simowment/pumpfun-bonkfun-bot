"""Paper execution port."""

from rugbot.domain.amounts import Lamports
from rugbot.execution.ports import (
    ExecutionIntent,
    ExecutionMode,
    ExecutionReceipt,
    PaperTradeSimulator,
    non_submitting_receipt,
    validate_execution_intent,
)


class PaperExecutionPort:
    """Execution port backed by a deterministic local simulator."""

    def __init__(self, simulator: PaperTradeSimulator | None = None) -> None:
        """Initialize the paper execution port."""

        self._simulator = simulator

    async def submit(self, intent: ExecutionIntent) -> ExecutionReceipt:
        """Simulate an intent without submitting a transaction."""

        intent_error = validate_execution_intent(intent)
        if intent_error is not None:
            return non_submitting_receipt(
                mode=ExecutionMode.PAPER,
                intent=(intent if isinstance(intent, ExecutionIntent) else None),
                message=intent_error,
                estimated_fee_lamports=Lamports(0),
            )
        if self._simulator is None:
            return non_submitting_receipt(
                mode=ExecutionMode.PAPER,
                intent=intent,
                estimated_fee_lamports=Lamports(0),
                message="paper simulator is not configured",
            )

        simulator = self._simulator
        receipt = await self._simulate_safely(intent, simulator)
        if not isinstance(receipt, ExecutionReceipt):
            return non_submitting_receipt(
                mode=ExecutionMode.PAPER,
                intent=intent,
                estimated_fee_lamports=Lamports(0),
                message="paper simulator returned malformed receipt",
            )
        simulated_output = _validated_simulated_output(receipt)
        estimated_fee = _validated_estimated_fee(receipt)
        accepted = _validated_acceptance(
            intent=intent,
            receipt=receipt,
            simulated_output=simulated_output,
            estimated_fee=estimated_fee,
        )
        return ExecutionReceipt(
            mode=ExecutionMode.PAPER,
            intent_id=intent.intent_id,
            as_of_slot=intent.as_of_slot,
            accepted=accepted,
            would_submit_transaction=False,
            signature=None,
            simulated_output_base_units=simulated_output,
            estimated_fee_lamports=estimated_fee,
            message=_validated_message(receipt),
        )

    async def _simulate_safely(
        self,
        intent: ExecutionIntent,
        simulator: PaperTradeSimulator,
    ) -> object:
        try:
            return await simulator.simulate(intent)
        except Exception:  # noqa: BLE001
            return non_submitting_receipt(
                mode=ExecutionMode.PAPER,
                intent=intent,
                estimated_fee_lamports=Lamports(0),
                message="paper simulator raised during simulation",
            )


def _validated_simulated_output(receipt: ExecutionReceipt) -> int | None:
    output = receipt.simulated_output_base_units
    if output is None:
        return None
    if type(output) is int and output >= 0:
        return output
    return None


def _validated_estimated_fee(receipt: ExecutionReceipt) -> Lamports | None:
    fee = receipt.estimated_fee_lamports
    if fee is None:
        return None
    if type(fee) is int and fee >= 0:
        return fee
    return None


def _validated_acceptance(
    *,
    intent: ExecutionIntent,
    receipt: ExecutionReceipt,
    simulated_output: int | None,
    estimated_fee: Lamports | None,
) -> bool:
    return (
        receipt.accepted is True
        and _safe_paper_receipt_controls(intent=intent, receipt=receipt)
        and simulated_output is not None
        and estimated_fee is not None
    )


def _safe_paper_receipt_controls(
    *,
    intent: ExecutionIntent,
    receipt: ExecutionReceipt,
) -> bool:
    return (
        receipt.mode is ExecutionMode.PAPER
        and receipt.intent_id == intent.intent_id
        and type(receipt.as_of_slot) is int
        and receipt.as_of_slot == intent.as_of_slot
        and receipt.would_submit_transaction is False
        and receipt.signature is None
    )


def _validated_message(receipt: ExecutionReceipt) -> str:
    if type(receipt.message) is str and receipt.message:
        return receipt.message
    return "paper simulator returned sanitized receipt"
