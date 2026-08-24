"""Recorded finalized account coverage for fast creator resolution."""

import base64
import json
from pathlib import Path

from rugbot.ingest.pump.bonding_curve_account import (
    PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
    PumpBondingCurveAccountState,
    decode_pump_bonding_curve_creator,
)


def test_decode_creator_from_recorded_finalized_curve() -> None:
    fixture_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "account_states"
        / "pump_bonding_curve"
        / "finalized_current_layout_ffzxakv.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    account = fixture["account"]
    decoded = decode_pump_bonding_curve_creator(
        PumpBondingCurveAccountState(
            as_of_slot=fixture["rpc"]["context_slot"],
            account_pubkey=account["pubkey"],
            owner_program_id=account["owner"],
            raw_account_data=base64.b64decode(account["data_base64"]),
            source_artifact_version=fixture["artifact_version"],
            layout_artifact_version=PUMP_BONDING_CURVE_LAYOUT_ARTIFACT_VERSION,
        )
    )

    assert isinstance(decoded, bytes)
    assert decoded.hex() == fixture["expected"]["creator_hex"]
