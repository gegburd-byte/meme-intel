from engine import Candle, Setup, decision_engine, market_metrics, volume_profile, risk_flags


def candles(values, volume=1000):
    out = []
    for i, close in enumerate(values):
        out.append(
            Candle(
                ts=i * 60,
                o=close * 0.99,
                h=close * 1.01,
                l=close * 0.98,
                c=close,
                v=volume,
            )
        )
    return out


def test_volume_profile_returns_70_percent_area():
    cs = candles([10, 10.2, 10.1, 10.3, 10.2, 10.4, 10.3, 10.25] * 5)
    profile = volume_profile(cs)

    assert profile["poc"] is not None
    assert profile["vah"] is not None
    assert profile["val"] is not None
    assert profile["val"] <= profile["poc"] <= profile["vah"]
    assert profile["value_area_pct"] == 0.70


def test_market_metrics_has_core_indicators():
    cs = candles([1 + i * 0.01 for i in range(60)])
    market = market_metrics(cs, [], {
        "liquidity": 50000,
        "v5mUSD": 10000,
        "txns": {"m5": {"buys": 70, "sells": 30}},
    })

    assert market["ema9"] is not None
    assert market["ema21"] is not None
    assert market["rsi14"] is not None
    assert market["profile"]["poc"] is not None
    assert market["buy_ratio_5m"] == 0.7


def test_critical_risk_blocks_long_signal():
    cs = candles([1 + i * 0.01 for i in range(60)])
    market = market_metrics(cs, [], {
        "liquidity": 50000,
        "v5mUSD": 10000,
        "txns": {"m5": {"buys": 70, "sells": 30}},
    })

    setup = Setup(
        state="BREAKOUT_CONFIRMED",
        prev_high=1.5,
        higher_low=1.2,
        stop=1.18,
    )

    risk = risk_flags(liquidity_usd=1000, market_cap=50000)
    social = {
        "mention_velocity": 4,
        "sentiment": 85,
        "author_quality": 80,
        "coordination_risk": 10,
        "unique_author_count": 12,
    }

    decision = decision_engine(
        setup=setup,
        market=market,
        social=social,
        risk=risk,
        overview={
            "liquidity": 1000,
            "v5mUSD": 5000,
        },
    )

    assert risk["overall"] == "CRITICAL"
    assert decision["action"] == "NO TRADE"
    assert decision["invalidation"] == 1.18
