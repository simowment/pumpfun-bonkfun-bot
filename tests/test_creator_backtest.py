from rugbot.backtest.runners.creator_backtest_runner import (
    CreatorBacktestConfig,
    CreatorSample,
    resolve_tp_sl_matrix,
    run_creator_tp_sl_grid_search,
)


def _sample(traj, mint="m1"):
    return CreatorSample(
        mint=mint,
        creator="w",
        created_at=1,
        created_slot=1,
        trajectory=tuple(traj),
        ath_multiplier=None,
    )


def test_tp_win_sl_loss_timeout():
    # rises to +200 then dump -> win at TP 50
    s_win = CreatorSample(
        mint="win",
        creator="w",
        created_at=1,
        created_slot=1,
        trajectory=((0.0, 1.0), (10.0, 3.0), (20.0, 0.5)),
        ath_multiplier=3.0,
    )
    # drops to 0.7 -> loss at SL 20
    s_loss = CreatorSample(
        mint="loss",
        creator="w",
        created_at=2,
        created_slot=2,
        trajectory=((0.0, 1.0), (5.0, 0.7), (10.0, 0.6)),
        ath_multiplier=0.7,
    )
    # stagnates
    s_hold = CreatorSample(
        mint="hold",
        creator="w",
        created_at=3,
        created_slot=3,
        trajectory=((0.0, 1.0), (10.0, 1.01), (90.0, 0.99)),
        ath_multiplier=1.01,
    )
    config = CreatorBacktestConfig(
        quote_size_sol=0.3,
        slippage_pct=0.0,
        gas_fee_sol=0.0,
        max_hold_s=90,
        tp_grid=(50.0,),
        sl_grid=(20.0,),
    )
    report = run_creator_tp_sl_grid_search([s_win, s_loss, s_hold], config)
    assert not report.insufficient_data
    ev = report.evaluations[0]
    # s_win win, s_loss loss, s_hold timeout ~ small loss/win depending fees => expect 1 win 2 losses
    assert ev.wins == 1
    assert ev.losses == 2
    assert ev.winrate_pct == 33.33 or abs(ev.winrate_pct - 33.33) < 0.5
    # EV negative or small
    assert ev.net_ev_sol < 0.2


def test_resolve_matrix():
    s1 = _sample(((0.0, 1.0), (5.0, 2.0)), mint="a")
    s2 = _sample(((0.0, 1.0), (5.0, 0.5)), mint="b")
    # need at least 2 samples
    s1 = CreatorSample(
        mint="a",
        creator="w",
        created_at=1,
        created_slot=1,
        trajectory=((0.0, 1.0), (5.0, 2.0)),
        ath_multiplier=2.0,
    )
    s2 = CreatorSample(
        mint="b",
        creator="w",
        created_at=2,
        created_slot=2,
        trajectory=((0.0, 1.0), (5.0, 0.5)),
        ath_multiplier=0.5,
    )
    cfg = CreatorBacktestConfig(
        tp_grid=(25.0, 100.0), sl_grid=(20.0,), slippage_pct=0.0, gas_fee_sol=0.0
    )
    mat = resolve_tp_sl_matrix([s1, s2], cfg)
    assert len(mat) == 2  # tp rows
    assert len(mat[0]) == 1
    # TP 25 should win for s1, TP100 wins too (2x)
    assert mat[0][0].wins >= 1


def test_insufficient():
    cfg = CreatorBacktestConfig()
    r = run_creator_tp_sl_grid_search([], cfg)
    assert r.insufficient_data
    r2 = run_creator_tp_sl_grid_search([_sample(((0.0, 1.0),), mint="only")], cfg)
    assert r2.insufficient_data
