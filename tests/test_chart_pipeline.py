from adapters import Candle as AdapterCandle
from main import chart_data_quality, parse_pump_candles


def test_adapters_can_construct_candles():
    candle = AdapterCandle(
        ts=1,
        o=1.0,
        h=1.2,
        l=0.9,
        c=1.1,
        v=3.0,
    )
    assert candle.c == 1.1


def test_single_native_candle_is_accepted_for_initial_chart():
    candles = parse_pump_candles([
        {
            "timestamp": 1_700_000_000,
            "open": 0.000001,
            "high": 0.000001,
            "low": 0.000001,
            "close": 0.000001,
            "volume": 1,
        }
    ])
    assert len(candles) == 1
    assert chart_data_quality(candles, minimum_bars=1) > 0


def test_malformed_native_candle_is_rejected():
    candles = parse_pump_candles([
        {
            "timestamp": 1_700_000_000,
            "open": 0,
            "high": 0,
            "low": 0,
            "close": 0,
            "volume": 1,
        }
    ])
    assert candles == []
