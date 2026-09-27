from rugbot.domain.ohlc import TradeTick, build_ohlc_candles


def _tick(timestamp: int, price: float) -> TradeTick:
    return TradeTick(
        timestamp=timestamp, price=price, volume=1.0, is_buy=True, signature=""
    )


def test_multi_second_candles_align_to_their_buckets() -> None:
    candles = build_ohlc_candles(
        [_tick(7, 1.0), _tick(8, 3.0), _tick(18, 2.0)], timeframe_seconds=5
    )

    assert [(c.timestamp, c.open, c.high, c.close, c.volume) for c in candles] == [
        (5, 1.0, 3.0, 3.0, 2.0),
        (10, 3.0, 3.0, 3.0, 0.0),
        (15, 2.0, 2.0, 2.0, 1.0),
    ]
