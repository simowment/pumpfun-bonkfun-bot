"""Integration tests for FastAPI /api/trade/* endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from rugbot.interfaces.web.fastapi_app import create_fastapi_app
from rugbot.runtime.app import build_ui_runtime

VALID_MINT = "279mMFSUjS2kg4S3yQwwv3zZBqCtZ1Quvmg8FUHYpump"


def test_fastapi_trade_lifecycle(tmp_path: Path) -> None:
    """Test full web trading workflow: buy -> check positions -> sell -> delete."""
    core = build_ui_runtime(state_dir=tmp_path)
    app = create_fastapi_app(core)

    with TestClient(app) as client:
        # 1. Buy Token
        buy_res = client.post(
            "/api/trade/buy",
            json={
                "mint": VALID_MINT,
                "amount_sol": 0.2,
                "slippage_pct": 5.0,
                "priority_fee_sol": 0.0005,
                "jito_tip_sol": 0.001,
                "take_profit_pct": 50.0,
                "stop_loss_pct": 20.0,
                "mode": "paper",
            },
        )
        assert buy_res.status_code == 200
        buy_data = buy_res.json()
        assert buy_data["ok"] is True
        assert buy_data["side"] == "buy"
        assert buy_data["mint"] == VALID_MINT
        assert buy_data["token_amount"] > 0
        assert buy_data["take_profit_pct"] == 50.0

        # 2. Get Open Positions
        pos_res = client.get("/api/trade/positions")
        assert pos_res.status_code == 200
        pos_data = pos_res.json()
        assert pos_data["ok"] is True
        assert pos_data["total_open"] == 1
        assert pos_data["positions"][0]["mint"] == VALID_MINT

        # 3. Sell 50% of Position
        sell_res = client.post(
            "/api/trade/sell",
            json={
                "mint": VALID_MINT,
                "percent": 50.0,
                "slippage_pct": 10.0,
                "mode": "paper",
            },
        )
        assert sell_res.status_code == 200
        sell_data = sell_res.json()
        assert sell_data["ok"] is True
        assert sell_data["side"] == "sell"

        # 4. Close remaining position via DELETE
        del_res = client.delete(f"/api/trade/positions/{VALID_MINT}")
        assert del_res.status_code == 200
        assert del_res.json()["ok"] is True

        # 5. Check no open positions remain
        pos_after = client.get("/api/trade/positions").json()
        assert pos_after["total_open"] == 0
