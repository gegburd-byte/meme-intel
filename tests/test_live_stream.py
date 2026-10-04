import base64
import struct

from live_stream import (
    LiveTradeHub,
    PUMP_AMM_BUY_DISC,
    PUMP_TRADE_DISC,
    parse_live_trade,
    parse_pumpfun_socket_trade,
)


def _base58_encode(data: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = alphabet[rem] + out
    return "1" * (len(data) - len(data.lstrip(b"\\x00"))) + (out or "")


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


def test_parse_pump_trade_from_inner_instruction_data():
    mint = "So11111111111111111111111111111111111111112"
    ts = 1_700_000_000
    payload = (
        PUMP_TRADE_DISC
        + bytes(32)
        + struct.pack("<QQB", 2_000_000_000, 100_000_000, 1)
        + bytes(32)
        + struct.pack("<q", ts)
        + struct.pack("<QQ", 90_000_000_000, 793_000_000_000_000)
        + bytes(16)
    )

    transaction = {
        "blockTime": ts,
        "slot": 99,
        "meta": {
            "logMessages": [],
            "innerInstructions": [
                {
                    "index": 0,
                    "instructions": [
                        {"programId": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "data": _base58_encode(payload)}
                    ],
                }
            ],
        },
    }

    from live_stream import parse_live_trade_from_transaction

    row = parse_live_trade_from_transaction(
        transaction,
        mint,
        signature="inner-sig",
        slot=99,
        block_time=ts,
    )

    assert row is not None
    assert row["source"] == "PUMP.FUN"
    assert row["signature"] == "inner-sig"
    assert row["timestamp"] == ts

def test_parse_pump_trade_from_top_level_instruction_data():
    mint = "So11111111111111111111111111111111111111112"
    ts = 1_700_000_000
    payload = (
        PUMP_TRADE_DISC
        + bytes(32)
        + struct.pack("<QQB", 1_000_000_000, 50_000_000, 1)
        + bytes(32)
        + struct.pack("<q", ts)
        + struct.pack("<QQ", 80_000_000_000, 800_000_000_000_000)
    )

    transaction = {
        "blockTime": ts,
        "slot": 100,
        "transaction": {
            "message": {
                "instructions": [
                    {
                        "programId": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                        "data": _base58_encode(payload),
                    }
                ]
            }
        },
        "meta": {
            "logMessages": [],
            "innerInstructions": [],
        },
    }

    from live_stream import parse_live_trade_from_transaction

    row = parse_live_trade_from_transaction(
        transaction,
        mint,
        signature="top-level-sig",
        slot=100,
        block_time=ts,
    )

    assert row is not None
    assert row["source"] == "PUMP.FUN"
    assert row["signature"] == "top-level-sig"
    assert row["timestamp"] == ts


def test_live_trade_hub_builds_current_candle_from_pumpfun_trades():
    hub = LiveTradeHub("test-key")
    mint = "So11111111111111111111111111111111111111112"
    hub.remember_trade(mint, {
        "id": "a",
        "source": "PUMP.FUN",
        "timestamp": 1_700_000_001,
        "price": 0.01,
        "volume_sol": 1.0,
    })
    hub.remember_trade(mint, {
        "id": "b",
        "source": "PUMP.FUN",
        "timestamp": 1_700_000_030,
        "price": 0.013,
        "volume_sol": 2.0,
    })
    hub.remember_trade(mint, {
        "id": "ignored",
        "source": "PUMPSWAP",
        "timestamp": 1_700_000_030,
        "price": 9.0,
        "volume_sol": 9.0,
    })

    candle = hub.current_candle(mint, timeframe=1)

    assert candle is not None
    assert candle["ts"] == 1_699_999_980
    assert candle["o"] == 0.01
    assert candle["h"] == 9.0
    assert candle["l"] == 0.01
    assert candle["c"] == 9.0
    assert candle["v"] == 12.0
    assert candle["source"] == "PUMP.FUN LIVE TRADES"


def test_live_trade_hub_accepts_pumpswap_for_current_candle():
    hub = LiveTradeHub("test-key")
    mint = "So11111111111111111111111111111111111111112"
    hub.remember_trade(mint, {
        "id": "ps-1",
        "source": "PUMPSWAP",
        "timestamp": 1_700_000_001,
        "price": 0.020,
        "volume_sol": 1.0,
    })
    hub.remember_trade(mint, {
        "id": "ps-2",
        "source": "PUMPSWAP",
        "timestamp": 1_700_000_004,
        "price": 0.021,
        "volume_sol": 2.0,
    })

    candle = hub.current_candle(mint, timeframe=1)

    assert candle is not None
    assert candle["o"] == 0.020
    assert candle["h"] == 0.021
    assert candle["l"] == 0.020
    assert candle["c"] == 0.021
    assert candle["v"] == 3.0


def test_recent_trade_snapshot_returns_bounded_clean_rows():
    hub = LiveTradeHub("test-key")
    mint = "So11111111111111111111111111111111111111112"

    hub.remember_trade(mint, {
        "id": "one",
        "signature": "sig-one",
        "source": "PUMP.FUN",
        "side": "BUY",
        "price": 0.01,
        "volume_sol": 1.5,
        "timestamp": 1_700_000_001,
    })

    rows = hub.recent_trade_snapshot(mint, limit=10)

    assert len(rows) == 1
    assert rows[0]["id"] == "one"
    assert rows[0]["signature"] == "sig-one"
    assert rows[0]["price"] == 0.01
    assert rows[0]["volume_sol"] == 1.5


def test_live_trade_hub_rpc_helper_uses_http_client(monkeypatch):
    import asyncio

    hub = LiveTradeHub("test-key")

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "jsonrpc": "2.0",
                "result": {"value": 1},
                "id": "test",
            }

    calls = []

    async def fake_post(url, json):
        calls.append((url, json))
        return FakeResponse()

    monkeypatch.setattr(hub._http, "post", fake_post)

    result, error = asyncio.run(
        hub._rpc(
            "getSignaturesForAddress",
            ["mint", {"limit": 1}],
        )
    )

    assert error is None
    assert result == {"value": 1}
    assert calls



def test_live_trade_hub_account_subscription_targets_exact_market_account():
    import asyncio

    hub = LiveTradeHub("test-key")
    hub.stream_mode = "STANDARD"
    sent = []

    async def fake_send(payload):
        sent.append(payload)

    hub._send = fake_send

    asyncio.run(
        hub._subscribe(
            "mint-address",
            "pool-address",
        )
    )

    assert sent
    assert sent[0]["method"] == "accountSubscribe"
    assert sent[0]["params"][0] == "pool-address"
    assert sent[0]["params"][1]["commitment"] == "processed"


def test_live_trade_hub_stale_current_candle_is_rejected():
    import time

    hub = LiveTradeHub("test-key")
    mint = "So11111111111111111111111111111111111111112"
    hub.remember_trade(
        mint,
        {
            "id": "old",
            "source": "PUMP.FUN",
            "timestamp": int(time.time()) - 10,
            "price": 0.01,
            "volume_sol": 1.0,
        },
    )

    assert hub.current_candle(
        mint,
        timeframe=1,
        max_age_seconds=2,
    ) is None


def test_parse_pump_trade_history_accepts_swap_quote_and_base_aliases():
    from main import parse_pump_trades

    payload = [{
        "id": "swap-trade-1",
        "createdTs": 1_700_000_000_000,
        "quoteAmountIn": 2_000_000_000,
        "baseAmountOut": 100_000_000,
        "txType": "buy",
    }]

    rows = parse_pump_trades(payload)

    assert len(rows) == 1
    assert rows[0]["id"] == "swap-trade-1"
    assert rows[0]["ts"] == 1_700_000_000
    assert abs(rows[0]["price"] - 0.02) < 1e-12
    assert rows[0]["volume"] == 2.0


def test_parse_swap_trade_prefers_executed_amounts_over_virtual_reserves():
    from main import parse_pump_trades

    payload = [{
        "id": "swap-trade-executed-price",
        "createdTs": 1_700_000_000_000,
        "quoteAmountIn": 7_000_000,
        "baseAmountOut": 1_000_000,
        # This stale bonding-curve ratio would be 4.1e-8. It must never
        # override the actual PumpSwap execution price of 0.007 SOL/token.
        "virtualSolReserves": 41_088_000,
        "virtualTokenReserves": 1_000_000_000_000_000,
        "txType": "buy",
    }]

    rows = parse_pump_trades(payload)

    assert len(rows) == 1
    assert abs(rows[0]["price"] - 0.007) < 1e-12
    assert rows[0]["volume"] == 0.007


def test_parse_native_pumpfun_socket_trade_packet():
    import json

    payload = {
        "signature": "native-sig",
        "sol_amount": 7_000_000,
        "token_amount": 1_000_000,
        "is_buy": True,
        "timestamp": 1_700_000_000,
        "mint": "So11111111111111111111111111111111111111112",
        "slot": 123,
    }
    raw = "42" + json.dumps(["tradeCreated", payload])

    trade = parse_pumpfun_socket_trade(raw)

    assert trade is not None
    assert trade["signature"] == "native-sig"
    assert trade["source"] == "PUMP.FUN"
    assert trade["price"] == 0.007
    assert trade["volume_sol"] == 0.007
    assert trade["timestamp"] == 1_700_000_000


def test_native_pumpfun_socket_heartbeat_packet_is_not_a_trade():
    assert parse_pumpfun_socket_trade("2") is None
