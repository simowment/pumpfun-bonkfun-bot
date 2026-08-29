import sqlite3
import tempfile
from pathlib import Path

from rugbot.discover.store import ensure_discover_schema, upsert_launch, upsert_trade
from rugbot.domain.market_data import build_token_market_history
from rugbot.storage.database import DatabaseManager


def _tmp_db():
    td = tempfile.mkdtemp()
    p = Path(td) / "rugbot.db"
    db = DatabaseManager(str(p))
    ensure_discover_schema(db)
    return p, db


def test_entry_peak_floor_migrated():
    p, db = _tmp_db()
    mint = "TestMint1111111111111111111111111111111111"
    upsert_launch(
        db,
        mint=mint,
        creator="Creator111111111111111111111111111111111",
        created_signature="sig0",
        created_slot=100,
    )
    # supply will be unavailable (no rpc) -> mc None
    trades = [
        (100, 10_000_000, 1_000_000_000),  # q=10M, b=1B => ppm 10000
        (101, 30_000_000, 1_000_000_000),  # ppm 30000 peak
        (102, 5_000_000, 1_000_000_000),  # ppm 5000 floor
    ]
    for i, (slot, q, b) in enumerate(trades):
        upsert_trade(
            db,
            mint=mint,
            signature=f"sig{i}",
            event_index=0,
            slot=slot,
            side="buy",
            quote_amount_base_units=q,
            base_amount=b,
            price_ppm=q * 1_000_000 // b,
        )
    db.close()
    h = build_token_market_history(mint, db_path=p)
    assert h.entry_price_ppm == 10_000
    assert h.peak_price_ppm == 30_000
    assert h.floor_price_ppm == 5_000
    assert h.entry_slot == 100
    assert h.peak_slot == 101
    assert len(h.trajectory) == 3
    assert h.migrated is False
    assert "market_cap unavailable" in ",".join(h.unavailable)


def test_fallback_unavailable_when_empty():
    p, db = _tmp_db()
    db.close()
    mint = "EmptyMint11111111111111111111111111111111"
    h = build_token_market_history(mint, db_path=p)
    # fallback off-chain will try network; but trajectory must be empty and unavailable contains marker
    assert h.trajectory == ()
    assert h.entry_price_ppm is None
    assert any(
        "trajectory unavailable" in u or "no discover_trades" in u
        for u in h.unavailable
    )


def test_recalc_price_prefers_executed():
    p, db = _tmp_db()
    mint = "RecalcMint111111111111111111111111111111111"
    upsert_launch(db, mint=mint, creator="C", created_signature="s0", created_slot=1)
    # stored price_ppm is mid (wrong 9999), but executed should be 20000
    upsert_trade(
        db,
        mint=mint,
        signature="sigA",
        event_index=0,
        slot=1,
        side="buy",
        quote_amount_base_units=20_000_000,
        base_amount=1_000_000_000,
        price_ppm=9999,
    )
    db.close()
    h = build_token_market_history(mint, db_path=p)
    assert h.entry_price_ppm == 20_000


def test_solscan_enhanced_transactions_populates_when_db_empty(monkeypatch):
    p, db = _tmp_db()
    db.close()
    mint = "SolscanMint11111111111111111111111111111111"

    # Build fake Solscan rows with token + SOL balance deltas
    def _fake_row(
        slot: int, sig: str, wallet: str, quote: int, base: int, side_log: str
    ):
        return {
            "slot": slot,
            "blockTime": 1_700_000_000 + slot,
            "transaction": {
                "signatures": [sig],
                "message": {
                    "accountKeys": [wallet, "bondingCurve111111111111111111111111111"]
                },
            },
            "meta": {
                "preTokenBalances": [{"mint": mint, "owner": wallet, "amount": "0"}],
                "postTokenBalances": [
                    {"mint": mint, "owner": wallet, "amount": str(base)}
                ],
                "preBalances": [quote + 5000, 0],
                "postBalances": [5000, 0],
                "fee": 5000,
                "logMessages": [f"Program log: Instruction: {side_log}"],
            },
            "transactionIndex": slot,
        }

    rows_page1 = [
        _fake_row(
            100,
            "sig1001111111111111111111111111111111111111111111111111111111111111",
            "Wallet11111111111111111111111111111111111",
            10_000_000,
            1_000_000_000,
            "Buy",
        ),
        _fake_row(
            101,
            "sig1011111111111111111111111111111111111111111111111111111111111111",
            "Wallet11111111111111111111111111111111111",
            30_000_000,
            1_000_000_000,
            "Buy",
        ),
        _fake_row(
            102,
            "sig1021111111111111111111111111111111111111111111111111111111111111",
            "Wallet11111111111111111111111111111111111",
            5_000_000,
            1_000_000_000,
            "Sell",
        ),
    ]

    class FakePage:
        def __init__(self, txs, cursor):
            self.transactions = tuple(txs)
            self.cursor = cursor

    # Mock SolscanClient.enhanced_transactions to return our fake pages
    monkeypatch.setenv("SOLSCAN_API_KEY", "test-key")
    # ensure derive_bonding_curve works for this mint - patch to valid PDA
    import rugbot.domain.market_data as md

    orig_derive = md._derive_bonding_curve
    monkeypatch.setattr(
        md,
        "_derive_bonding_curve",
        lambda m: "BondingCurve1111111111111111111111111111111",
    )

    from rugbot.integrations.solscan import SolscanClient

    def fake_enhanced(self, address, *, program, cursor=None, limit=10):
        assert program == "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
        # single page then done
        return FakePage(rows_page1, None)

    monkeypatch.setattr(SolscanClient, "enhanced_transactions", fake_enhanced)

    h = build_token_market_history(mint, db_path=p)
    assert h.entry_price_ppm == 10_000
    assert h.peak_price_ppm == 30_000
    assert h.floor_price_ppm == 5_000
    assert len(h.trajectory) == 3
    assert h.sources.get("trajectory") == "solscan enhanced_transactions on-chain"
    # Verify persisted to DB for reuse

    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as c FROM discover_trades WHERE mint=?", (mint,))
    assert cur.fetchone()["c"] == 3
    conn.close()
    # Restore
    monkeypatch.setattr(md, "_derive_bonding_curve", orig_derive)


def test_early_onchain_complements_solscan_peak(monkeypatch):
    """When Solscan misses early peak, early on-chain fetch completes entry=first, peak=max."""

    import rugbot.domain.market_data as md

    p, db = _tmp_db()
    db.close()
    mint = "EarlyMint111111111111111111111111111111111"

    def _fake_row(
        slot: int, sig: str, wallet: str, quote: int, base: int, side_log: str
    ):
        return {
            "slot": slot,
            "blockTime": 1_700_000_000 + slot,
            "transaction": {
                "signatures": [sig],
                "message": {
                    "accountKeys": [wallet, "bondingCurve111111111111111111111111111"]
                },
            },
            "meta": {
                "preTokenBalances": [{"mint": mint, "owner": wallet, "amount": "0"}],
                "postTokenBalances": [
                    {"mint": mint, "owner": wallet, "amount": str(base)}
                ],
                "preBalances": [quote + 5000, 0],
                "postBalances": [5000, 0],
                "fee": 5000,
                "logMessages": [f"Program log: Instruction: {side_log}"],
            },
            "transactionIndex": slot,
        }

    # Solscan only returns recent low trades (missing early high)
    rows_recent = [
        _fake_row(
            500,
            "sig5001111111111111111111111111111111111111111111111111111111111111",
            "Wallet11111111111111111111111111111111111",
            10_000_000,
            1_000_000_000,
            "Buy",
        ),
        _fake_row(
            501,
            "sig5011111111111111111111111111111111111111111111111111111111111111",
            "Wallet11111111111111111111111111111111111",
            12_000_000,
            1_000_000_000,
            "Buy",
        ),
    ]

    class FakePage:
        def __init__(self, txs, cursor):
            self.transactions = tuple(txs)
            self.cursor = cursor

    monkeypatch.setenv("SOLSCAN_API_KEY", "test-key")
    monkeypatch.setattr(
        md,
        "_derive_bonding_curve",
        lambda m: "BondingCurve1111111111111111111111111111111",
    )
    monkeypatch.setattr(md, "_resolve_rpc_url", lambda x=None: "https://fake.rpc")

    from rugbot.integrations.solscan import SolscanClient

    def fake_enhanced(self, address, *, program, cursor=None, limit=10):
        return FakePage(rows_recent, None)

    monkeypatch.setattr(SolscanClient, "enhanced_transactions", fake_enhanced)

    # Mock early on-chain: return one early trade with high peak (75M quote vs 10M)
    early_trades = [
        {
            "slot": 10,
            "tx_index": None,
            "signature": "earlyPeakSig11111111111111111111111111111111111111111111111111111",
            "wallet": None,
            "side": "buy",
            "quote_amount": 75_000_000,
            "base_amount": 1_000_000_000,
            "price_ppm": 75_000,
        },
        {
            "slot": 5,
            "tx_index": None,
            "signature": "earlyEntrySig1111111111111111111111111111111111111111111111111111",
            "wallet": None,
            "side": "buy",
            "quote_amount": 10_000_000,
            "base_amount": 1_000_000_000,
            "price_ppm": 10_000,
        },
    ]

    monkeypatch.setattr(
        md, "_fetch_early_onchain_trades", lambda mint, bc, existing, rpc: early_trades
    )
    monkeypatch.setattr(
        md,
        "_fetch_supply_via_rpc",
        lambda mint, rpc_url=None: (1_000_000_000_000, 6, 9, False),
    )

    h = build_token_market_history(mint, db_path=p)
    # entry = min slot (5) price 10k, peak = max(75k) from early
    assert h.entry_price_ppm == 10_000
    assert h.entry_slot == 5
    assert h.peak_price_ppm == 75_000
    assert h.peak_slot == 10
    assert len(h.trajectory) == 4
    assert "early-onchain" in h.sources.get("trajectory", "")
