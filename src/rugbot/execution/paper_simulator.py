"""Pure paper round-trip simulation for validated market state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from rugbot.decision.sizing import EntryLatencySnapshot
from rugbot.domain.amounts import Lamports, QuoteBaseUnits, Slot, TokenBaseUnits
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.quotes import ExecutableQuote, QuotePath
from rugbot.execution.ports import (
    ExecutionIntent,
    ExecutionMode,
    ExecutionReceipt,
    non_submitting_receipt,
    validate_execution_intent,
)
from rugbot.protocol.pump.quote_engine import (
    PoolReserves,
    executable_buy_quote,
    executable_sell_quote,
)

if TYPE_CHECKING:
    from rugbot.domain.fees import FeeConfig

BASIS_POINTS_DENOMINATOR = 10_000
PROBABILITY_PPM_DENOMINATOR = 1_000_000


@dataclass(frozen=True, slots=True)
class PaperStress:
    """Deterministic adverse execution assumptions for one paper round trip.

    ``failure_probe_ppm`` is a deterministic percentile supplied by replay. It
    makes failure simulation reproducible without random state or a live call:
    a probe below a modeled failure probability fails the leg.
    """

    latency_snapshot: EntryLatencySnapshot | None
    max_entry_latency_ms: int
    max_exit_latency_ms: int
    entry_slippage_bps: int = 0
    entry_impact_bps: int = 0
    exit_slippage_bps: int = 0
    exit_impact_bps: int = 0
    entry_failure_ppm: int = 0
    full_exit_failure_ppm: int = 0
    failure_probe_ppm: int = PROBABILITY_PPM_DENOMINATOR - 1
    max_full_exit_failure_ppm: int = PROBABILITY_PPM_DENOMINATOR


@dataclass(frozen=True, slots=True)
class PaperRoundTripInputs:
    """Explicit point-in-time inputs for one paper entry and full exit."""

    as_of_slot: Slot
    path: QuotePath
    reserves: PoolReserves
    fee_config: FeeConfig | None
    entry_intent: ExecutionIntent
    stress: PaperStress


@dataclass(frozen=True, slots=True)
class PaperRoundTripResult:
    """Result of an integer-only entry plus stressed full-position exit."""

    as_of_slot: Slot
    accepted: bool
    entry_quote: ExecutableQuote
    full_exit_quote: ExecutableQuote | None
    entry_receipt: ExecutionReceipt
    exit_receipt: ExecutionReceipt | None
    entry_output_base_units: TokenBaseUnits
    stressed_entry_output_base_units: TokenBaseUnits
    stressed_full_exit_output_quote_base_units: QuoteBaseUnits | None
    entry_fee_quote_base_units: QuoteBaseUnits
    exit_fee_quote_base_units: QuoteBaseUnits | None
    total_fee_quote_base_units: QuoteBaseUnits
    net_pnl_quote_base_units: int | None
    entry_price_impact_bps: int
    exit_price_impact_bps: int | None
    entry_latency_ms: int
    exit_latency_ms: int
    reason_codes: tuple[str, ...]


PaperRoundTripOutcome = PaperRoundTripResult | AbstainResult


def simulate_paper_round_trip(*, inputs: PaperRoundTripInputs) -> PaperRoundTripOutcome:
    """Simulate a paper buy and a complete stressed sell without I/O.

    The buy quote is generated from the canonical quote engine. The sell uses
    the counterfactual reserves after that buy and the full received position.
    Additional slippage and impact are applied as conservative integer output
    reductions. Missing, stale, malformed, or untrusted state abstains.
    """

    validation_error = _validate_inputs(inputs)
    if validation_error is not None:
        return validation_error

    entry_quote = executable_buy_quote(
        path=inputs.path,
        reserves=inputs.reserves,
        quote_input_amount=QuoteBaseUnits(
            int(inputs.entry_intent.quote_amount_base_units)
        ),
        fee_config=inputs.fee_config,
    )
    if isinstance(entry_quote, AbstainResult):
        return entry_quote

    stress = inputs.stress
    latency = stress.latency_snapshot
    entry_latency_ms = int(latency.p99_entry_latency_ms) + int(latency.safety_margin_ms)
    exit_latency_ms = int(latency.p99_exit_latency_ms) + int(latency.safety_margin_ms)
    entry_output = TokenBaseUnits(entry_quote.output_amount_base_units)
    stressed_entry_output = TokenBaseUnits(
        _apply_adverse_bps(
            int(entry_output),
            stress.entry_slippage_bps + stress.entry_impact_bps,
        )
    )
    entry_impact_bps = _quote_price_impact_bps(
        quote=entry_quote,
        input_reserves=int(inputs.reserves.virtual_quote_reserves),
        output_reserves=int(inputs.reserves.virtual_base_reserves),
        pool_input=_pool_input_after_fee(entry_quote, inputs.path),
    )
    entry_failure = _entry_failure_reason(
        intent=inputs.entry_intent,
        stress=stress,
        stressed_output=int(stressed_entry_output),
        entry_latency_ms=entry_latency_ms,
    )
    if entry_failure is not None:
        entry_receipt = _receipt(
            intent=inputs.entry_intent,
            accepted=False,
            simulated_output=None,
            estimated_fee=entry_quote.fee_amount_base_units,
            message=entry_failure,
        )
        return _result_without_exit(
            inputs=inputs,
            entry_quote=entry_quote,
            entry_receipt=entry_receipt,
            entry_output=entry_output,
            stressed_entry_output=stressed_entry_output,
            entry_impact_bps=entry_impact_bps,
            entry_latency_ms=entry_latency_ms,
            exit_latency_ms=exit_latency_ms,
            reason_code=entry_failure,
        )

    post_entry_reserves = _reserves_after_buy(
        reserves=inputs.reserves,
        path=inputs.path,
        quote=entry_quote,
    )
    if isinstance(post_entry_reserves, AbstainResult):
        return post_entry_reserves

    full_exit_quote = executable_sell_quote(
        path=inputs.path,
        reserves=post_entry_reserves,
        base_input_amount=TokenBaseUnits(int(stressed_entry_output)),
        fee_config=inputs.fee_config,
    )
    if isinstance(full_exit_quote, AbstainResult):
        return full_exit_quote

    stressed_exit_output = QuoteBaseUnits(
        _apply_adverse_bps(
            full_exit_quote.output_amount_base_units,
            stress.exit_slippage_bps + stress.exit_impact_bps,
        )
    )
    exit_impact_bps = _quote_price_impact_bps(
        quote=full_exit_quote,
        input_reserves=int(post_entry_reserves.virtual_base_reserves),
        output_reserves=int(post_entry_reserves.virtual_quote_reserves),
        pool_input=full_exit_quote.input_amount_base_units,
    )
    exit_intent = _build_exit_intent(
        intent=inputs.entry_intent,
        base_amount=int(stressed_entry_output),
    )
    exit_failure = _exit_failure_reason(
        intent=exit_intent,
        stress=stress,
        stressed_output=int(stressed_exit_output),
        exit_latency_ms=exit_latency_ms,
    )
    exit_receipt = _receipt(
        intent=exit_intent,
        accepted=exit_failure is None,
        simulated_output=(int(stressed_exit_output) if exit_failure is None else None),
        estimated_fee=full_exit_quote.fee_amount_base_units,
        message=(
            "full stressed exit executed" if exit_failure is None else exit_failure
        ),
    )
    accepted = exit_failure is None
    entry_receipt = _receipt(
        intent=inputs.entry_intent,
        accepted=accepted,
        simulated_output=(int(stressed_entry_output) if accepted else None),
        estimated_fee=entry_quote.fee_amount_base_units,
        message=(
            "paper round trip accepted"
            if accepted
            else f"paper round trip rejected: {exit_failure}"
        ),
    )
    total_fee = QuoteBaseUnits(
        entry_quote.fee_amount_base_units + full_exit_quote.fee_amount_base_units
    )
    return PaperRoundTripResult(
        as_of_slot=inputs.as_of_slot,
        accepted=accepted,
        entry_quote=entry_quote,
        full_exit_quote=full_exit_quote,
        entry_receipt=entry_receipt,
        exit_receipt=exit_receipt,
        entry_output_base_units=entry_output,
        stressed_entry_output_base_units=stressed_entry_output,
        stressed_full_exit_output_quote_base_units=stressed_exit_output,
        entry_fee_quote_base_units=QuoteBaseUnits(entry_quote.fee_amount_base_units),
        exit_fee_quote_base_units=QuoteBaseUnits(full_exit_quote.fee_amount_base_units),
        total_fee_quote_base_units=total_fee,
        net_pnl_quote_base_units=(
            int(stressed_exit_output) - int(inputs.entry_intent.quote_amount_base_units)
            if accepted
            else None
        ),
        entry_price_impact_bps=entry_impact_bps,
        exit_price_impact_bps=exit_impact_bps,
        entry_latency_ms=entry_latency_ms,
        exit_latency_ms=exit_latency_ms,
        reason_codes=(
            ("paper_round_trip_accepted",)
            if accepted
            else (exit_failure or "full_exit_execution_failed",)
        ),
    )


class PaperRoundTripSimulator:
    """Adapt the round-trip simulator to the existing paper port contract."""

    def __init__(
        self,
        *,
        as_of_slot: Slot,
        path: QuotePath,
        reserves: PoolReserves,
        fee_config: FeeConfig | None,
        stress: PaperStress,
    ) -> None:
        """Initialize a simulator with immutable point-in-time state."""

        self._inputs = PaperRoundTripInputs(
            as_of_slot=as_of_slot,
            path=path,
            reserves=reserves,
            fee_config=fee_config,
            entry_intent=_placeholder_intent(as_of_slot),
            stress=stress,
        )

    def simulate_round_trip(self, intent: ExecutionIntent) -> PaperRoundTripOutcome:
        """Run the pure round-trip simulation for ``intent``."""

        return simulate_paper_round_trip(
            inputs=replace(self._inputs, entry_intent=intent)
        )

    async def simulate(self, intent: ExecutionIntent) -> ExecutionReceipt:
        """Return the non-submitting receipt expected by ``PaperExecutionPort``."""

        result = self.simulate_round_trip(intent)
        if isinstance(result, AbstainResult):
            return non_submitting_receipt(
                mode=ExecutionMode.PAPER,
                intent=intent,
                estimated_fee_lamports=Lamports(0),
                message=f"paper round trip abstained: {result.message}",
            )
        return result.entry_receipt


def _validate_inputs(inputs: object) -> AbstainResult | None:  # noqa: PLR0911
    if not isinstance(inputs, PaperRoundTripInputs):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE, "paper inputs malformed", -1
        )
    if not _non_negative_int(inputs.as_of_slot):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "paper as_of_slot must be a non-negative integer",
            int(inputs.as_of_slot) if type(inputs.as_of_slot) is int else -1,
        )
    if not isinstance(inputs.reserves, PoolReserves):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "pool reserves are required",
            inputs.as_of_slot,
        )
    if inputs.reserves.as_of_slot != inputs.as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "reserves slot does not match paper slot",
            inputs.as_of_slot,
        )
    if not isinstance(inputs.entry_intent, ExecutionIntent):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "entry intent is malformed",
            inputs.as_of_slot,
        )
    intent_error = validate_execution_intent(inputs.entry_intent)
    if intent_error is not None:
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE, intent_error, inputs.as_of_slot
        )
    if inputs.entry_intent.side != "buy":
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "paper round trip requires a buy intent",
            inputs.as_of_slot,
        )
    if inputs.entry_intent.as_of_slot != inputs.as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "entry intent slot does not match paper slot",
            inputs.as_of_slot,
        )
    if not isinstance(inputs.stress, PaperStress):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "paper stress inputs are required",
            inputs.as_of_slot,
        )
    return _validate_stress(inputs.stress, inputs.as_of_slot)


def _validate_stress(  # noqa: C901, PLR0911
    stress: PaperStress, as_of_slot: Slot
) -> AbstainResult | None:
    latency = stress.latency_snapshot
    if not isinstance(latency, EntryLatencySnapshot):
        return _abstain(
            AbstainReason.MISSING_FEATURE, "latency snapshot is required", as_of_slot
        )
    if latency.as_of_slot != as_of_slot:
        return _abstain(
            AbstainReason.STALE_STATE,
            "latency snapshot slot does not match paper slot",
            as_of_slot,
        )
    if (
        type(latency.latency_snapshot_version) is not str
        or not latency.latency_snapshot_version
    ):
        return _abstain(
            AbstainReason.MISSING_FEATURE,
            "latency snapshot version is required",
            as_of_slot,
        )
    if type(latency.evidence_ids) is not tuple or not latency.evidence_ids:
        return _abstain(
            AbstainReason.MISSING_FEATURE, "latency evidence is required", as_of_slot
        )
    if any(
        type(evidence_id) is not str or not evidence_id
        for evidence_id in latency.evidence_ids
    ):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "latency evidence is malformed",
            as_of_slot,
        )
    integer_fields = (
        latency.as_of_slot,
        stress.max_entry_latency_ms,
        stress.max_exit_latency_ms,
        stress.entry_slippage_bps,
        stress.entry_impact_bps,
        stress.exit_slippage_bps,
        stress.exit_impact_bps,
        stress.entry_failure_ppm,
        stress.full_exit_failure_ppm,
        stress.failure_probe_ppm,
        stress.max_full_exit_failure_ppm,
        latency.p99_entry_latency_ms,
        latency.p99_exit_latency_ms,
        latency.safety_margin_ms,
    )
    if any(type(value) is not int for value in integer_fields):
        return _abstain(
            AbstainReason.UNKNOWN_PROTOCOL_STATE,
            "paper stress fields must be integers",
            as_of_slot,
        )
    if any(value < 0 for value in integer_fields):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "paper stress fields must be non-negative",
            as_of_slot,
        )
    bps_fields = (
        stress.entry_slippage_bps,
        stress.entry_impact_bps,
        stress.exit_slippage_bps,
        stress.exit_impact_bps,
    )
    if any(value > BASIS_POINTS_DENOMINATOR for value in bps_fields):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "paper stress bps exceed 10000",
            as_of_slot,
        )
    if (
        stress.entry_slippage_bps + stress.entry_impact_bps > BASIS_POINTS_DENOMINATOR
        or stress.exit_slippage_bps + stress.exit_impact_bps > BASIS_POINTS_DENOMINATOR
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "paper stress bps exceed total output",
            as_of_slot,
        )
    ppm_fields = (
        stress.entry_failure_ppm,
        stress.full_exit_failure_ppm,
        stress.failure_probe_ppm,
        stress.max_full_exit_failure_ppm,
    )
    if (
        any(value > PROBABILITY_PPM_DENOMINATOR for value in ppm_fields)
        or stress.failure_probe_ppm == PROBABILITY_PPM_DENOMINATOR
    ):
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "paper failure ppm is invalid",
            as_of_slot,
        )
    return None


def _entry_failure_reason(
    *,
    intent: ExecutionIntent,
    stress: PaperStress,
    stressed_output: int,
    entry_latency_ms: int,
) -> str | None:
    if entry_latency_ms > stress.max_entry_latency_ms:
        return "entry_latency_budget_exceeded"
    if stress.entry_slippage_bps + stress.entry_impact_bps > intent.max_slippage_bps:
        return "entry_slippage_above_tolerance"
    if stressed_output <= 0:
        return "entry_fill_would_be_zero"
    if stress.failure_probe_ppm < stress.entry_failure_ppm:
        return "entry_execution_failed"
    return None


def _exit_failure_reason(
    *,
    intent: ExecutionIntent,
    stress: PaperStress,
    stressed_output: int,
    exit_latency_ms: int,
) -> str | None:
    if exit_latency_ms > stress.max_exit_latency_ms:
        return "exit_latency_budget_exceeded"
    if stress.exit_slippage_bps + stress.exit_impact_bps > intent.max_slippage_bps:
        return "exit_slippage_above_tolerance"
    if stress.full_exit_failure_ppm > stress.max_full_exit_failure_ppm:
        return "full_exit_failure_above_cap"
    if stressed_output <= 0:
        return "full_exit_fill_would_be_zero"
    if stress.failure_probe_ppm < stress.full_exit_failure_ppm:
        return "full_exit_execution_failed"
    return None


def _reserves_after_buy(
    *, reserves: PoolReserves, path: QuotePath, quote: ExecutableQuote
) -> PoolReserves | AbstainResult:
    pool_input = _pool_input_after_fee(quote, path)
    virtual_base = int(reserves.virtual_base_reserves) - quote.output_amount_base_units
    virtual_quote = int(reserves.virtual_quote_reserves) + pool_input
    real_base = int(reserves.real_base_reserves) - quote.output_amount_base_units
    real_quote = int(reserves.real_quote_reserves) + pool_input
    if min(virtual_base, virtual_quote, real_base, real_quote) < 0:
        return _abstain(
            AbstainReason.UNSUPPORTED_PROTOCOL_STATE,
            "counterfactual post-entry reserves are invalid",
            reserves.as_of_slot,
        )
    return replace(
        reserves,
        virtual_base_reserves=TokenBaseUnits(virtual_base),
        virtual_quote_reserves=QuoteBaseUnits(virtual_quote),
        real_base_reserves=TokenBaseUnits(real_base),
        real_quote_reserves=QuoteBaseUnits(real_quote),
    )


def _pool_input_after_fee(quote: ExecutableQuote, path: QuotePath) -> int:
    effective_input = quote.input_amount_base_units - quote.fee_amount_base_units
    if path is QuotePath.PUMP_BONDING_CURVE:
        return max(0, effective_input - 1)
    return effective_input


def _quote_price_impact_bps(
    *,
    quote: ExecutableQuote,
    input_reserves: int,
    output_reserves: int,
    pool_input: int,
) -> int:
    if input_reserves <= 0 or output_reserves <= 0 or pool_input <= 0:
        return BASIS_POINTS_DENOMINATOR
    ideal_output = pool_input * output_reserves // input_reserves
    if ideal_output <= 0 or quote.output_amount_base_units >= ideal_output:
        return 0
    return (
        (ideal_output - quote.output_amount_base_units)
        * BASIS_POINTS_DENOMINATOR
        // ideal_output
    )


def _apply_adverse_bps(amount: int, bps: int) -> int:
    return amount * (BASIS_POINTS_DENOMINATOR - bps) // BASIS_POINTS_DENOMINATOR


def _build_exit_intent(*, intent: ExecutionIntent, base_amount: int) -> ExecutionIntent:
    return ExecutionIntent(
        intent_id=f"{intent.intent_id}:full-exit",
        as_of_slot=intent.as_of_slot,
        market_id=intent.market_id,
        side="sell",
        quote_amount_base_units=None,
        base_amount_base_units=base_amount,
        max_slippage_bps=intent.max_slippage_bps,
        reason_codes=("paper_full_exit",),
    )


def _result_without_exit(  # noqa: PLR0913
    *,
    inputs: PaperRoundTripInputs,
    entry_quote: ExecutableQuote,
    entry_receipt: ExecutionReceipt,
    entry_output: TokenBaseUnits,
    stressed_entry_output: TokenBaseUnits,
    entry_impact_bps: int,
    entry_latency_ms: int,
    exit_latency_ms: int,
    reason_code: str,
) -> PaperRoundTripResult:
    return PaperRoundTripResult(
        as_of_slot=inputs.as_of_slot,
        accepted=False,
        entry_quote=entry_quote,
        full_exit_quote=None,
        entry_receipt=entry_receipt,
        exit_receipt=None,
        entry_output_base_units=entry_output,
        stressed_entry_output_base_units=stressed_entry_output,
        stressed_full_exit_output_quote_base_units=None,
        entry_fee_quote_base_units=QuoteBaseUnits(entry_quote.fee_amount_base_units),
        exit_fee_quote_base_units=None,
        total_fee_quote_base_units=QuoteBaseUnits(entry_quote.fee_amount_base_units),
        net_pnl_quote_base_units=None,
        entry_price_impact_bps=entry_impact_bps,
        exit_price_impact_bps=None,
        entry_latency_ms=entry_latency_ms,
        exit_latency_ms=exit_latency_ms,
        reason_codes=(reason_code,),
    )


def _receipt(
    *,
    intent: ExecutionIntent,
    accepted: bool,
    simulated_output: int | None,
    estimated_fee: int,
    message: str,
) -> ExecutionReceipt:
    return ExecutionReceipt(
        mode=ExecutionMode.PAPER,
        intent_id=intent.intent_id,
        as_of_slot=intent.as_of_slot,
        accepted=accepted,
        would_submit_transaction=False,
        signature=None,
        simulated_output_base_units=simulated_output,
        estimated_fee_lamports=Lamports(estimated_fee),
        message=message,
    )


def _placeholder_intent(as_of_slot: Slot) -> ExecutionIntent:
    return ExecutionIntent(
        intent_id="paper-placeholder",
        as_of_slot=as_of_slot,
        market_id="paper-placeholder",
        side="buy",
        quote_amount_base_units=1,
        base_amount_base_units=None,
        max_slippage_bps=0,
        reason_codes=("paper_placeholder",),
    )


def _abstain(reason: AbstainReason, message: str, as_of_slot: int) -> AbstainResult:
    return AbstainResult(reason=reason, message=message, as_of_slot=as_of_slot)


def _non_negative_int(value: object) -> bool:
    return type(value) is int and value >= 0
