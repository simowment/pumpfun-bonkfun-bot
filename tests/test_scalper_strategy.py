"""Pure decision tests for scalper strategy."""

from rugbot.domain.scalper_strategy import (
    ScalperConfig,
    decide_scalper_exit,
    next_filled,
)


def test_tp_tranches_in_order():
    cfg = ScalperConfig(
        tp_levels_pct=(25.0, 35.0, 45.0), sell_fractions=(0.2, 0.3, 0.5), sl_pct=12.0
    )
    entry = 1_000_000
    filled = (False, False, False)
    # 30% => first TP triggers
    sig = decide_scalper_exit(
        config=cfg,
        entry_price_ppm=entry,
        current_price_ppm=int(entry * 1.30),
        current_slot=10,
        entry_slot=0,
        filled=filled,
    )
    assert sig.action == "take_profit"
    assert sig.tranche_index == 0
    assert sig.fraction == 0.2

    filled2 = next_filled(filled, 0)
    sig2 = decide_scalper_exit(
        config=cfg,
        entry_price_ppm=entry,
        current_price_ppm=int(entry * 1.40),
        current_slot=11,
        entry_slot=0,
        filled=filled2,
    )
    assert sig2.tranche_index == 1
    assert sig2.fraction == 0.3


def test_stop_loss_triggered():
    cfg = ScalperConfig(sl_pct=12.0)
    entry = 1_000_000
    sig = decide_scalper_exit(
        config=cfg,
        entry_price_ppm=entry,
        current_price_ppm=int(entry * 0.85),
        current_slot=5,
        entry_slot=0,
        filled=(False, False, False),
    )
    assert sig.action == "stop_loss"
    assert sig.reason == "stop_loss"


def test_circuit_breaker_holds():
    cfg = ScalperConfig(daily_loss_stop=5)
    entry = 1_000_000
    sig = decide_scalper_exit(
        config=cfg,
        entry_price_ppm=entry,
        current_price_ppm=int(entry * 1.50),
        current_slot=5,
        entry_slot=0,
        filled=(False, False, False),
        consecutive_losses=5,
    )
    assert sig.action == "hold"
    assert sig.reason == "circuit_breaker"


def test_timeout_closes():
    cfg = ScalperConfig(max_hold_slots=5)
    entry = 1_000_000
    sig = decide_scalper_exit(
        config=cfg,
        entry_price_ppm=entry,
        current_price_ppm=int(entry * 1.05),
        current_slot=10,
        entry_slot=0,
        filled=(False, False, False),
    )
    assert sig.action in ("take_profit", "stop_loss")
    assert "timeout" in sig.reason


def test_next_filled_marks_correctly():
    assert next_filled((False, False), 1) == (False, True)
    assert next_filled((True, False, False), 2) == (True, False, True)
