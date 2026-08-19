"""Single-route transaction dispatch for one signed economic intent."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rugbot.execution.sender.base import RoutingPolicy, SubmissionResult

if TYPE_CHECKING:
    from rugbot.execution.sender.jito import JitoSender
    from rugbot.execution.sender.rpc import RpcSender
    from rugbot.execution.telemetry import ExecutionMetrics


class TransactionRouter:
    """Dispatch signed bytes through exactly one configured sender."""

    def __init__(
        self,
        rpc_sender: RpcSender,
        jito_sender: JitoSender | None = None,
    ) -> None:
        self.rpc_sender = rpc_sender
        self.jito_sender = jito_sender

    async def route(
        self,
        raw_tx_bytes: bytes,
        policy: RoutingPolicy = RoutingPolicy.RPC_ONLY,
        telemetry: ExecutionMetrics | None = None,
    ) -> SubmissionResult:
        """Send exact signed bytes through the one selected route."""

        if policy is RoutingPolicy.RPC_ONLY:
            result = await self.rpc_sender.send_transaction(raw_tx_bytes)
        elif self.jito_sender is not None:
            result = await self.jito_sender.send_transaction(raw_tx_bytes)
        else:
            return SubmissionResult(
                sender_name="jito",
                signature="",
                ack_ms=0.0,
                acknowledged=False,
                error_message="Jito route selected without a configured sender",
            )
        self._record_telemetry(result, telemetry)
        return result

    @staticmethod
    def _record_telemetry(
        result: SubmissionResult,
        telemetry: ExecutionMetrics | None,
    ) -> None:
        if telemetry is None:
            return
        if result.sender_name == "jito":
            telemetry.jito_ack_ms = result.ack_ms
        elif result.sender_name == "rpc":
            telemetry.rpc_ack_ms = result.ack_ms
        if result.acknowledged:
            telemetry.first_ack_sender = result.sender_name
