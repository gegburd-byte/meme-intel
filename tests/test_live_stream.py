import base64
import struct

from live_stream import (
    PUMP_AMM_BUY_DISC,
    PUMP_TRADE_DISC,
    parse_live_trade,
)


def test_parse_pump_trade_event():
    mint = "So11111111111111111111111111111111111111112"
    ts = 1_700_000_000
    payload = (
        PUMP_TRADE_DISC
        + bytes(32)
        + struct.pack("<QQB", 2_000_000_000, 100_000_000, 1)
        + bytes(32)
        + struct.pack("<q", ts)
        + struct.pack("<QQ", 90_000_000_000, 793_000_000_000_000)
    )
    log = "Program data: " + base64.b64encode(payload).decode()

    row = parse_live_trade([log], mint, signature="sig", slot=1)

    assert row is not None
    assert row["source"] == "PUMP.FUN"
    assert row["side"] == "BUY"
    assert abs(row["price"] - ((90_000_000_000 / 1e9) / (793_000_000_000_000 / 1e6))) < 1e-18
    assert row["timestamp"] == ts


def test_parse_pumpswap_buy_event():
    mint = "So11111111111111111111111111111111111111112"
    ts = 1_700_000_000
    payload = (
        PUMP_AMM_BUY_DISC
        + struct.pack(
            "<qQQQQQQQ",
            ts,
            100_000_000,
            0,
            200_000_000,
            0,
            300_000_000,
            0,
            2_000_000_000,
        )
    )
    log = "Program data: " + base64.b64encode(payload).decode()

    row = parse_live_trade([log], mint, signature="sig2", slot=2)

    assert row is not None
    assert row["source"] == "PUMPSWAP"
    assert row["side"] == "BUY"
    assert abs(row["price"] - 0.02) < 1e-12
