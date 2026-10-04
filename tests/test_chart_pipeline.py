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


def test_parse_pump_trade_history_uses_virtual_reserve_price():
    from main import parse_pump_trades, aggregate_pump_trade_candles

    rows = parse_pump_trades([
        {
            "timestamp": 1_700_000_000,
            "sol_amount": 1_000_000_000,
            "token_amount": 1_000_000_000,
            "virtual_sol_reserves": 30_000_000_000,
            "virtual_token_reserves": 1_000_000_000_000_000,
        }
    ])

    assert len(rows) == 1
    assert rows[0]["price"] > 0

    candles = aggregate_pump_trade_candles(
        [[
            {
                "timestamp": 1_700_000_000,
                "sol_amount": 1_000_000_000,
                "token_amount": 1_000_000_000,
                "virtual_sol_reserves": 30_000_000_000,
                "virtual_token_reserves": 1_000_000_000_000_000,
            }
        ]],
        timeframe=1,
        limit=10,
    )

    assert len(candles) == 1
    assert candles[0].h == candles[0].c


def test_frontend_timeframes_and_live_cache_are_wired():
    from pathlib import Path

    html = Path("static/index.html").read_text()
    js = Path("static/app.js").read_text()

    for value in ("1s", "1m", "5m", "15m", "1h"):
        assert f'data-tf="{value}"' in html

    assert "async function syncLiveTradeCache()" in js
    assert "/api/chart/live-trades?mint=" in js
    assert "startLiveTradeCachePoll();" in js


def test_chart_history_has_no_gecko_source():
    from pathlib import Path

    source = Path("main.py").read_text()
    history_start = source.index('@app.get("/api/chart/history")')
    history_end = source.index('@app.get("/api/chart/current")', history_start)
    history = source[history_start:history_end]

    assert "GECKOTERMINAL" not in history
    assert "PUMP.FUN TRADE HISTORY" in history
    assert "HELIUS_ONCHAIN_TRADES" in history


def test_live_trade_endpoint_uses_pumpfun_semantic_units():
    from pathlib import Path

    source = Path("main.py").read_text()
    start = source.index('@app.get("/api/chart/live-trades")')
    end = source.index('@app.get("/api/chart")', start)
    endpoint = source[start:end]

    assert "pf.trades(" in endpoint
    assert "parse_pump_trades" in endpoint
    assert "GECKO" not in endpoint


def test_parse_pump_trade_history_preserves_side():
    from main import parse_pump_trades

    rows = parse_pump_trades([
        {
            "timestamp": 1_700_000_000,
            "sol_amount": 1_000_000_000,
            "token_amount": 1_000_000,
            "virtual_sol_reserves": 30_000_000_000,
            "virtual_token_reserves": 1_000_000_000_000_000,
            "is_buy": False,
        }
    ])

    assert rows
    assert rows[0]["side"] == "SELL"
