"""Fees-always-on coverage for the backtest net-EV arithmetic.

The net positive EV claim in AGENTS.md section 14.4 is only honest if every
cost leg is applied. These tests pin the three legs that were previously
undefended: the recorded AMM LP fee, the execution cost carried by a filled
launch, and the unmodeled Jito tip in the offline stress arm. Amounts come from
the recorded finalized PumpSwap event and the canonical demo artifact.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import base58

from rugbot.backtest.cases.rpc_case_builder import _swap_event_as_trade_event
from rugbot.backtest.cli import (
    FULL_EXIT_OUTPUT_HAIRCUT_PPM,
    UNMODELED_JITO_TIP_QUOTE_BASE_UNITS,
    run_backtest_file,
)
from rugbot.backtest.evaluation import BacktestSplit, build_backtest_report
from rugbot.backtest.io import load_backtest_document
from rugbot.domain.amounts import (
    PROBABILITY_PPM_DENOMINATOR,
    QuoteBaseUnits,
    Slot,
    TokenBaseUnits,
)
from rugbot.domain.decisions import AbstainReason, AbstainResult
from rugbot.domain.fees import FeeConfig
from rugbot.domain.migration import PINNED_PUMP_SWAP_IDL_SHA256
from rugbot.domain.quote_engine import (
    CANONICAL_PUMPSWAP_PROGRAM_CONFIG_VERSION,
    PUMP_SWAP_POOL_DECODER_VERSION,
    PoolReserves,
    QuotePath,
    executable_sell_quote,
)
from rugbot.ingest.pump.swap_event_decoder import decode_pump_swap_trade_event

DEMO_ARTIFACT = Path("fixtures/backtest/demo.json")
SWAP_EVENT = next(
    Path("fixtures/finalized_transactions/pump_swap_event").glob("*.json")
)
BASE_DECIMALS = 6
QUOTE_DECIMALS = 9
# The recorded AMM event payload carries no mint; normalization takes it from
# the matched launch. It does not participate in any fee leg asserted here.
MINT_LABEL = "MintNotCarriedByTheRecordedEventPayload1111111"


def _recorded_swap_event():
    """Decode the recorded finalized PumpSwap BuyEvent."""

    document = json.loads(SWAP_EVENT.read_text(encoding="utf-8"))
    assert document["commitment"] == "finalized"
    event = decode_pump_swap_trade_event(
        base64.b64decode(document["data_base64"]),
        as_of_slot=document["as_of_slot"],
        signature=base58.b58decode(document["signature"]),
        event_index=document["event_index"],
    )
    assert not isinstance(event, AbstainResult)
    return event


def _amm_exit_quote(event, *, lp_fee_bps: int):
    """Quote a full AMM exit exactly as the trajectory builder does."""

    reserves = PoolReserves(
        virtual_base_reserves=TokenBaseUnits(event.pool_base_reserves_base_units),
        virtual_quote_reserves=QuoteBaseUnits(event.pool_quote_reserves_base_units),
        real_base_reserves=TokenBaseUnits(event.pool_base_reserves_base_units),
        real_quote_reserves=QuoteBaseUnits(event.pool_quote_reserves_base_units),
        is_complete=False,
        as_of_slot=Slot(event.as_of_slot),
        base_decimals=BASE_DECIMALS,
        quote_decimals=QUOTE_DECIMALS,
        decoder_version=PUMP_SWAP_POOL_DECODER_VERSION,
        idl_hash=PINNED_PUMP_SWAP_IDL_SHA256,
        program_config_version=CANONICAL_PUMPSWAP_PROGRAM_CONFIG_VERSION,
    )
    fee = FeeConfig(
        version="pump-swap-event-fees",
        protocol_fee_bps=event.protocol_fee_basis_points,
        creator_fee_bps=event.creator_fee_basis_points,
        lp_fee_bps=lp_fee_bps,
        is_known=True,
        program_config_version=CANONICAL_PUMPSWAP_PROGRAM_CONFIG_VERSION,
        valid_from_slot=Slot(event.as_of_slot),
        source_artifact_version=f"finalized-trade-event:{SWAP_EVENT.name}",
    )
    quote = executable_sell_quote(
        path=QuotePath.CANONICAL_PUMPSWAP,
        reserves=reserves,
        base_input_amount=TokenBaseUnits(event.base_amount_base_units),
        fee_config=fee,
    )
    assert not isinstance(quote, AbstainResult)
    return fee, quote


def _evaluate(document: dict, tmp_path: Path):
    """Hydrate one mutated artifact and run the shared evaluator over it."""

    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    parsed = load_backtest_document(path)
    assert not isinstance(parsed, AbstainResult)
    return build_backtest_report(launches=parsed.launches, config=parsed.config)


def _metrics(report, split: BacktestSplit):
    return next(item for item in report.split_metrics if item.split is split)


def test_recorded_amm_event_normalization_preserves_the_lp_fee_leg() -> None:
    """The LP leg must survive normalization into the bonding-curve proof.

    ``_fee_config`` reads ``lp_fee_basis_points`` off this proof, so losing it
    here silently drops 20 bps from every AMM exit quote downstream.
    """

    event = _recorded_swap_event()

    proof = _swap_event_as_trade_event(event, MINT_LABEL)

    assert proof.lp_fee_basis_points == 20
    assert proof.lp_fee_base_units == 399
    assert proof.protocol_fee_basis_points == event.protocol_fee_basis_points
    assert proof.creator_fee_basis_points == event.creator_fee_basis_points


def test_amm_exit_quote_charges_every_recorded_fee_leg() -> None:
    """All three legs must be charged; omitting the LP leg overstates net EV."""

    event = _recorded_swap_event()

    honest_fee, honest_quote = _amm_exit_quote(event, lp_fee_bps=20)
    dropped_fee, dropped_quote = _amm_exit_quote(event, lp_fee_bps=0)

    assert honest_fee.swap_total_fee_bps == 30
    assert honest_quote.fee_amount_base_units == 594
    assert honest_quote.output_amount_base_units == 197392

    assert dropped_fee.swap_total_fee_bps == 10
    assert dropped_quote.fee_amount_base_units == 198

    overstated = (
        dropped_quote.output_amount_base_units - honest_quote.output_amount_base_units
    )
    assert overstated == 396


def test_canonical_demo_artifact_reports_net_pnl_after_execution_cost() -> None:
    """The canonical fixture is net of cost in every split."""

    report = run_backtest_file(DEMO_ARTIFACT)

    assert not isinstance(report, AbstainResult)
    assert report.source_launch_count == 3
    assert _metrics(report, BacktestSplit.TRAIN).net_pnl_filled_quote_base_units == 100
    assert _metrics(report, BacktestSplit.TEST).net_pnl_filled_quote_base_units == 200
    assert (
        _metrics(report, BacktestSplit.STRESS).net_pnl_filled_quote_base_units == -150
    )
    assert _metrics(report, BacktestSplit.STRESS).cost_to_gross_profit_ppm == 6000000


def test_fee_free_artifact_is_rejected(tmp_path: Path) -> None:
    """Zeroing execution cost must not be able to inflate net EV.

    Before this gate the same mutation flipped the stress split from a 150 base
    unit loss to a 30 base unit profit and still produced a report.
    """

    document = json.loads(DEMO_ARTIFACT.read_text(encoding="utf-8"))
    for launch in document["launches"]:
        launch["execution_cost_quote_base_units"] = 0
        launch["net_pnl_quote_base_units"] = launch["gross_profit_quote_base_units"]

    report = _evaluate(document, tmp_path)

    assert isinstance(report, AbstainResult)
    assert report.reason is AbstainReason.UNSUPPORTED_PROTOCOL_STATE
    assert report.message == (
        "filled launch execution cost must be positive because every executable "
        "Pump quote charges a fee"
    )


def test_unfilled_launch_may_still_report_zero_execution_cost(tmp_path: Path) -> None:
    """The gate is scoped to fills; an unfilled entry never pays an exit fee."""

    document = json.loads(DEMO_ARTIFACT.read_text(encoding="utf-8"))
    launch = document["launches"][0]
    launch["fill_status"] = "unfilled"
    launch["net_pnl_quote_base_units"] = 0
    launch["gross_profit_quote_base_units"] = 0
    launch["execution_cost_quote_base_units"] = 0

    report = _evaluate(document, tmp_path)

    assert not isinstance(report, AbstainResult)


def test_offline_stress_arm_cannot_claim_positive_net_ev_without_a_tip() -> None:
    """Section 14.4 requires the Jito tip in any positive net EV claim.

    The offline RPC arm models no tip, so it must keep zeroing every executable
    exit output. Lowering the haircut without supplying a tip cost fails here.
    """

    available_output_ppm = PROBABILITY_PPM_DENOMINATOR - FULL_EXIT_OUTPUT_HAIRCUT_PPM

    assert available_output_ppm == 0 or UNMODELED_JITO_TIP_QUOTE_BASE_UNITS > 0
    assert UNMODELED_JITO_TIP_QUOTE_BASE_UNITS == 0
