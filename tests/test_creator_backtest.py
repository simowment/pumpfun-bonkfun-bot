from rugbot.backtest.runners.creator_backtest_runner import (
    CreatorBacktestConfig,
    CreatorSample,
    _simulate_observed_exit,
    _simulate_one,
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


def test_no_usable_price_history_abstains():
    """Fail closed when no sample has trajectory or ATH."""
    cfg = CreatorBacktestConfig()
    empty = [
        CreatorSample(
            mint="e1",
            creator="w",
            created_at=1,
            created_slot=1,
            trajectory=(),
            ath_multiplier=None,
        ),
        CreatorSample(
            mint="e2",
            creator="w",
            created_at=2,
            created_slot=2,
            trajectory=(),
            ath_multiplier=None,
        ),
    ]
    report = run_creator_tp_sl_grid_search(empty, cfg)
    assert report.insufficient_data is True
    assert report.evaluations == ()
    assert report.optimal_tp is None
    assert "no usable price history" in report.message
    # Contrast: one sample with ATH set still runs (guard not over-broad).
    usable = [
        empty[0],
        CreatorSample(
            mint="u1",
            creator="w",
            created_at=3,
            created_slot=3,
            trajectory=(),
            ath_multiplier=3.0,
        ),
    ]
    contrast = run_creator_tp_sl_grid_search(usable, cfg)
    assert contrast.insufficient_data is False


def test_observed_exit_differs_from_fixed_sl_on_rug():
    """Rug flooring at 0.2x: SL row exits at SL price, tp_only at ~0.2x."""
    cfg = CreatorBacktestConfig(
        quote_size_sol=0.3,
        slippage_pct=0.0,
        gas_fee_sol=0.0,
        max_hold_s=90,
        tp_grid=(100.0,),
        sl_grid=(10.0,),
    )
    rug = CreatorSample(
        mint="rug",
        creator="w",
        created_at=1,
        created_slot=1,
        trajectory=((0.0, 1.0), (10.0, 0.5), (20.0, 0.2), (30.0, 0.25)),
        ath_multiplier=1.0,
    )
    other = CreatorSample(
        mint="rug2",
        creator="w",
        created_at=2,
        created_slot=2,
        trajectory=((0.0, 1.0), (10.0, 0.4), (20.0, 0.2)),
        ath_multiplier=1.0,
    )
    _, sl_net, _ = _simulate_one(rug, 100.0, 10.0, cfg)
    _, obs_net, _ = _simulate_observed_exit(rug, 100.0, cfg)
    assert obs_net < sl_net  # 0.2x loss is worse than 0.9x SL fill
    report = run_creator_tp_sl_grid_search([rug, other], cfg)
    assert len(report.tp_only_evaluations) == 1
    tp_only = report.tp_only_evaluations[0]
    assert tp_only.sl_pct is None
    assert tp_only.net_pnl_sol < report.evaluations[0].net_pnl_sol


def test_observed_and_fixed_agree_on_tp_hit():
    """TP-hitting sample: both models exit at TP."""
    cfg = CreatorBacktestConfig(
        quote_size_sol=0.3,
        slippage_pct=0.0,
        gas_fee_sol=0.0,
        max_hold_s=90,
        tp_grid=(50.0,),
        sl_grid=(20.0,),
    )
    hit = CreatorSample(
        mint="hit",
        creator="w",
        created_at=1,
        created_slot=1,
        trajectory=((0.0, 1.0), (10.0, 3.0), (20.0, 0.5)),
        ath_multiplier=3.0,
    )
    other = CreatorSample(
        mint="hit2",
        creator="w",
        created_at=2,
        created_slot=2,
        trajectory=((0.0, 1.0), (5.0, 2.0)),
        ath_multiplier=2.0,
    )
    o1, n1, _ = _simulate_one(hit, 50.0, 20.0, cfg)
    o2, n2, _ = _simulate_observed_exit(hit, 50.0, cfg)
    assert o1 == "win" and o2 == "win"
    assert n1 == n2
    assert other.mint == "hit2"


def test_observed_empty_trajectory_fallback():
    """Empty trajectory falls back to ath_multiplier behaviour."""
    cfg = CreatorBacktestConfig(
        quote_size_sol=0.3,
        slippage_pct=0.0,
        gas_fee_sol=0.0,
        tp_grid=(50.0,),
        sl_grid=(20.0,),
    )
    win_empty = CreatorSample(
        mint="e1",
        creator="w",
        created_at=1,
        created_slot=1,
        trajectory=(),
        ath_multiplier=3.0,
    )
    o, _, _ = _simulate_observed_exit(win_empty, 50.0, cfg)
    assert o == "win"
    loss_empty = CreatorSample(
        mint="e2",
        creator="w",
        created_at=2,
        created_slot=2,
        trajectory=(),
        ath_multiplier=0.4,
    )
    o2, _, _ = _simulate_observed_exit(loss_empty, 50.0, cfg)
    assert o2 == "loss"


def test_optimal_observed_present_and_consistent():
    """optimal_tp/ev_observed present and match argmax of tp_only rows."""
    cfg = CreatorBacktestConfig(
        quote_size_sol=0.3,
        slippage_pct=0.0,
        gas_fee_sol=0.0,
        tp_grid=(25.0, 100.0),
        sl_grid=(20.0,),
    )
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
    report = run_creator_tp_sl_grid_search([s1, s2], cfg)
    assert len(report.tp_only_evaluations) == 2
    assert report.optimal_tp_observed is not None
    best = max(report.tp_only_evaluations, key=lambda e: e.net_ev_sol)
    assert report.optimal_tp_observed == best.tp_pct
    assert report.optimal_ev_observed == best.net_ev_sol
    assert report.exit_models == ("observed_no_stop", "scenario_fixed_stop")
    assert any("scenario-only" in w for w in report.warnings)
