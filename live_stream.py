from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
from collections import defaultdict
from typing import Any

import websockets


HELIUS_WS = "wss://mainnet.helius-rpc.com/?api-key={key}"

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

PUMP_TRADE_DISC = bytes.fromhex("bddb7fd34ee661ee")
PUMP_AMM_BUY_DISC = bytes([103, 244, 82, 31, 44, 245, 119, 119])
PUMP_AMM_SELL_DISC = bytes([62, 47, 55, 10, 165, 3, 220, 42])


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def _i64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<q", data, offset)[0]


def _event_bytes(log: str) -> bytes | None:
    prefix = "Program data: "
    if not log.startswith(prefix):
        return None
    try:
        return base64.b64decode(log[len(prefix):], validate=False)
    except Exception:
        return None


def parse_live_trade(logs: list[str] | None, mint: str, signature: str = "", slot: int | None = None) -> dict[str, Any] | None:
    """Decode Pump.fun/PumpSwap trade events emitted in Solana logs.

    Prices are execution prices from the on-chain event amounts, not a delayed
    third-party quote. Pump.fun tokens use 6 decimal base units and SOL uses
    9 decimal lamports.
    """
    for index, log in enumerate(logs or []):
        payload = _event_bytes(log)
        if not payload:
            continue

        pos = payload.find(PUMP_TRADE_DISC)
        if pos >= 0:
            start = pos + 8
            minimum = start + 32 + 8 + 8 + 1
            if len(payload) < minimum:
                continue

            try:
                event_mint = payload[start:start + 32]
                sol_amount = _u64(payload, start + 32)
                token_amount = _u64(payload, start + 40)
                is_buy = bool(payload[start + 48])

                if sol_amount <= 0 or token_amount <= 0:
                    continue

                # The event contains the mint as the first field. We trust the
                # logsSubscribe mint filter, but keep this length check to avoid
                # malformed/CPI data accidentally becoming a chart point.
                if len(event_mint) != 32:
                    continue

                price = (sol_amount / 1_000_000_000) / (token_amount / 1_000_000)
                if not price or price <= 0:
                    continue

                # Updated bonding-curve reserves are part of the event and
                # are what the Pump.fun UI uses to represent the live curve price.
                # Fall back to execution price only if a legacy event omits them.
                virtual_sol = 0
                virtual_token = 0
                if len(payload) >= start + 32 + 8 + 8 + 1 + 32 + 8 + 8 + 8:
                    virtual_sol = _u64(payload, start + 89)
                    virtual_token = _u64(payload, start + 97)

                if virtual_sol > 0 and virtual_token > 0:
                    price = (
                        virtual_sol / 1_000_000_000
                    ) / (
                        virtual_token / 1_000_000
                    )

                event_ts = None
                if len(payload) >= start + 89:
                    try:
                        event_ts = _i64(payload, start + 81)
                    except Exception:
                        event_ts = None

                if not event_ts or event_ts < 1_500_000_000 or event_ts > int(time.time()) + 3600:
                    event_ts = int(time.time())

                return {
                    "id": f"{signature}:{index}",
                    "signature": signature,
                    "slot": slot,
                    "mint": mint,
                    "source": "PUMP.FUN",
                    "venue": PUMP_PROGRAM,
                    "side": "BUY" if is_buy else "SELL",
                    "price": price,
                    "volume_sol": sol_amount / 1_000_000_000,
                    "token_amount": token_amount / 1_000_000,
                    "timestamp": int(event_ts),
                }
            except (TypeError, ValueError, struct.error):
                continue

        pos = payload.find(PUMP_AMM_BUY_DISC)
        side = "BUY"
        if pos < 0:
            pos = payload.find(PUMP_AMM_SELL_DISC)
            side = "SELL"

        if pos >= 0:
            start = pos + 8
            # Both current PumpSwap trade events put:
            # timestamp, base amount, ..., quote amount at start + 56.
            if len(payload) < start + 64:
                continue

            try:
                event_ts = _i64(payload, start)
                base_amount = _u64(payload, start + 8)
                quote_amount = _u64(payload, start + 56)
                if base_amount <= 0 or quote_amount <= 0:
                    continue

                price = (quote_amount / 1_000_000_000) / (base_amount / 1_000_000)
                if not price or price <= 0:
                    continue

                if not event_ts or event_ts < 1_500_000_000 or event_ts > int(time.time()) + 3600:
                    event_ts = int(time.time())

                return {
                    "id": f"{signature}:{index}",
                    "signature": signature,
                    "slot": slot,
                    "mint": mint,
                    "source": "PUMPSWAP",
                    "venue": PUMP_AMM_PROGRAM,
                    "side": side,
                    "price": price,
                    "volume_sol": quote_amount / 1_000_000_000,
                    "token_amount": base_amount / 1_000_000,
                    "timestamp": int(event_ts),
                }
            except (TypeError, ValueError, struct.error):
                continue

    return None


class LiveTradeHub:
    """One Helius WebSocket multiplexed across all selected-token clients."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("HELIUS_API_KEY")
        self.ws = None
        self.task: asyncio.Task | None = None
        self.clients: dict[str, set[Any]] = defaultdict(set)
        self.subscription_to_mint: dict[int, str] = {}
        self.pending: dict[int, str] = {}
        self.request_id = 1
        self.state = "NOT_CONFIGURED" if not self.api_key else "IDLE"
        self.last_error = ""
        self._send_lock = asyncio.Lock()

    def active(self) -> bool:
        return bool(self.api_key)

    async def add_client(self, mint: str, websocket: Any) -> None:
        self.clients[mint].add(websocket)
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self._run())

        await self._subscribe_when_ready(mint)

    async def remove_client(self, mint: str, websocket: Any) -> None:
        rows = self.clients.get(mint)
        if rows:
            rows.discard(websocket)
            if not rows:
                self.clients.pop(mint, None)
                await self._unsubscribe(mint)

    async def _subscribe_when_ready(self, mint: str) -> None:
        if self.ws is None:
            return
        subscribed = mint in self.subscription_to_mint.values()
        pending = mint in self.pending.values()
        if not subscribed and not pending:
            await self._subscribe(mint)

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.ws is None:
            return
        async with self._send_lock:
            await self.ws.send(json.dumps(payload))

    async def _subscribe(self, mint: str) -> None:
        request_id = self.request_id
        self.request_id += 1
        self.pending[request_id] = mint
        await self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "logsSubscribe",
            "params": [
                {"mentions": [mint]},
                {"commitment": "processed"},
            ],
        })

    async def _unsubscribe(self, mint: str) -> None:
        ids = [sub_id for sub_id, sub_mint in self.subscription_to_mint.items() if sub_mint == mint]
        for sub_id in ids:
            try:
                await self._send({
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "method": "logsUnsubscribe",
                    "params": [sub_id],
                })
                self.request_id += 1
            except Exception:
                pass
            self.subscription_to_mint.pop(sub_id, None)

    async def _broadcast(self, mint: str, payload: dict[str, Any]) -> None:
        dead = []
        for client in list(self.clients.get(mint, ())):
            try:
                await client.send_json(payload)
            except Exception:
                dead.append(client)

        for client in dead:
            self.clients[mint].discard(client)

    async def _status_all(self, state: str, detail: str = "") -> None:
        for mint in list(self.clients):
            await self._broadcast(mint, {
                "type": "status",
                "state": state,
                "detail": detail,
            })

    async def _run(self) -> None:
        if not self.api_key:
            await self._status_all("UNAVAILABLE", "HELIUS_API_KEY missing")
            return

        backoff = 0.5
        while self.clients:
            try:
                self.state = "CONNECTING"
                url = HELIUS_WS.format(key=self.api_key)

                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=2,
                    max_queue=2048,
                ) as ws:
                    self.ws = ws
                    self.state = "LIVE"
                    self.last_error = ""
                    self.subscription_to_mint.clear()
                    self.pending.clear()

                    await self._status_all("LIVE")

                    for mint in list(self.clients):
                        if mint not in self.subscription_to_mint.values() and mint not in self.pending.values():
                            await self._subscribe(mint)

                    async for raw in ws:
                        try:
                            message = json.loads(raw)
                        except Exception:
                            continue

                        if "id" in message and "result" in message:
                            mint = self.pending.pop(int(message["id"]), None)
                            if mint:
                                try:
                                    self.subscription_to_mint[int(message["result"])] = mint
                                except (TypeError, ValueError):
                                    pass
                            continue

                        if message.get("method") != "logsNotification":
                            continue

                        params = message.get("params") or {}
                        subscription = params.get("subscription")
                        mint = self.subscription_to_mint.get(subscription)
                        if not mint:
                            continue

                        result = ((params.get("result") or {}).get("value") or {})
                        if result.get("err") is not None:
                            continue

                        trade = parse_live_trade(
                            result.get("logs") or [],
                            mint,
                            signature=str(result.get("signature") or ""),
                            slot=(params.get("result") or {}).get("context", {}).get("slot"),
                        )
                        if trade:
                            await self._broadcast(mint, {
                                "type": "trade",
                                "trade": trade,
                            })

                self.ws = None
                self.subscription_to_mint.clear()
                self.pending.clear()

                if self.clients:
                    self.state = "RECONNECTING"
                    await self._status_all("RECONNECTING")
                    await asyncio.sleep(backoff)
                    backoff = min(5.0, backoff * 2)

            except asyncio.CancelledError:
                self.ws = None
                raise
            except Exception as exc:
                self.ws = None
                self.subscription_to_mint.clear()
                self.pending.clear()
                self.last_error = str(exc)[:300]
                self.state = "RECONNECTING"

                if self.clients:
                    await self._status_all("RECONNECTING", self.last_error)
                    await asyncio.sleep(backoff)
                    backoff = min(5.0, backoff * 2)

        self.state = "IDLE"


trade_hub = LiveTradeHub()
