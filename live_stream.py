from __future__ import annotations

import asyncio
import base64
import json
import os
import struct
import time
from collections import defaultdict, deque
from typing import Any

import httpx
import websockets


HELIUS_WS = "wss://mainnet.helius-rpc.com/?api-key={key}"
HELIUS_ENHANCED_WS = "wss://atlas-mainnet.helius-rpc.com/?api-key={key}"

ANCHOR_SELF_CPI_TAG = bytes([0xe4, 0x45, 0xa5, 0x2e, 0x51, 0xcb, 0x9a, 0x1d])

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"

PUMP_TRADE_DISC = bytes.fromhex("bddb7fd34ee661ee")
PUMP_AMM_BUY_DISC = bytes([103, 244, 82, 31, 44, 245, 119, 119])
PUMP_AMM_SELL_DISC = bytes([62, 47, 55, 10, 165, 3, 220, 42])


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def _i64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<q", data, offset)[0]


def _base58_decode(value: str) -> bytes | None:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if not value:
        return b""

    lookup = {char: index for index, char in enumerate(alphabet)}

    try:
        number = 0
        for char in value:
            number = number * 58 + lookup[char]
        raw = number.to_bytes(
            max(1, (number.bit_length() + 7) // 8),
            "big",
        )
        leading = 0
        for char in value:
            if char == "1":
                leading += 1
            else:
                break
        return b"\x00" * leading + (raw.lstrip(b"\x00") if number else b"")
    except (KeyError, ValueError):
        return None


def _iter_instruction_data(transaction: dict[str, Any] | None):
    """Yield raw instruction payloads from both top-level and CPI instructions."""
    tx = transaction or {}
    message = ((tx.get("transaction") or {}).get("message") or {})
    for instruction in message.get("instructions") or []:
        data = instruction.get("data")
        if isinstance(data, str):
            decoded = _base58_decode(data)
            if decoded:
                yield decoded

    meta = tx.get("meta") or {}
    for group in meta.get("innerInstructions") or []:
        for instruction in group.get("instructions") or []:
            data = instruction.get("data")
            if isinstance(data, str):
                decoded = _base58_decode(data)
                if decoded:
                    yield decoded


def parse_live_trade_from_transaction(
    transaction: dict[str, Any] | None,
    mint: str,
    signature: str = "",
    slot: int | None = None,
    block_time: int | None = None,
) -> dict[str, Any] | None:
    tx = transaction or {}
    meta = tx.get("meta") or {}

    trade = parse_live_trade(
        meta.get("logMessages") or [],
        mint,
        signature=signature,
        slot=slot,
        block_time=block_time if block_time is not None else tx.get("blockTime"),
    )

    if trade:
        return trade

    # Pump.fun TradeEvents are also emitted through CPI inner-instruction data.
    # Scan every inner instruction payload for the event discriminator rather
    # than requiring a specific wrapper/tag, because current transactions can
    # encode the event directly in instruction data.
    for payload in _iter_instruction_data(tx):
        if PUMP_TRADE_DISC in payload or PUMP_AMM_BUY_DISC in payload or PUMP_AMM_SELL_DISC in payload:
            pseudo_log = "Program data: " + base64.b64encode(payload).decode()
            trade = parse_live_trade(
                [pseudo_log],
                mint,
                signature=signature,
                slot=slot,
                block_time=block_time if block_time is not None else tx.get("blockTime"),
            )
            if trade:
                return trade

    return None

def _event_bytes(log: str) -> bytes | None:
    prefix = "Program data: "
    if not log.startswith(prefix):
        return None
    try:
        return base64.b64decode(log[len(prefix):], validate=False)
    except Exception:
        return None


def parse_live_trade(logs: list[str] | None, mint: str, signature: str = "", slot: int | None = None, block_time: int | None = None) -> dict[str, Any] | None:
    """Decode Pump.fun/PumpSwap trade events emitted in Solana logs."""
    target_mint = _base58_decode(mint)

    for index, log in enumerate(logs or []):
        payload = _event_bytes(log)
        if not payload:
            continue

        search_from = 0
        while True:
            pos = payload.find(PUMP_TRADE_DISC, search_from)
            if pos < 0:
                break

            start = pos + 8
            minimum = start + 32 + 8 + 8 + 1
            if len(payload) < minimum:
                break

            try:
                event_mint = payload[start:start + 32]

                # A transaction can contain multiple Pump.fun trades (for
                # example through an aggregator). Only accept the event whose
                # mint actually matches the chart token.
                if (
                    target_mint is not None and
                    len(target_mint) == 32 and
                    event_mint != target_mint and
                    event_mint != bytes(32)
                ):
                    search_from = pos + 8
                    continue

                sol_amount = _u64(payload, start + 32)
                token_amount = _u64(payload, start + 40)
                is_buy = bool(payload[start + 48])

                if sol_amount <= 0 or token_amount <= 0:
                    search_from = pos + 8
                    continue

                if len(event_mint) != 32:
                    search_from = pos + 8
                    continue

                price = (sol_amount / 1_000_000_000) / (token_amount / 1_000_000)
                if not price or price <= 0:
                    continue

                # Pump.fun's bonding-curve spot price is the virtual-reserve
                # ratio. Keep that canonical price for the live display while
                # native Pump.fun OHLC remains the candle authority.
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
                    event_ts = int(block_time or 0)
                if not event_ts or event_ts < 1_500_000_000:
                    continue

                return {
                    "id": f"{signature}:{index}:{pos}",
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
                search_from = pos + 8
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
                    event_ts = int(block_time or 0)
                if not event_ts or event_ts < 1_500_000_000:
                    continue

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
        self.enhanced_state: bool | None = None
        self.stream_mode = "STANDARD"
        self.recent_trades: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=500)
        )
        self._resolve_semaphore = asyncio.Semaphore(8)
        self._pending_resolutions: set[tuple[str, str]] = set()
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(2.5, connect=1.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            headers={
                "Accept": "application/json",
                "User-Agent": "Meme-Intel/live-stream",
            },
        )

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
                self.recent_trades.pop(mint, None)
                await self._unsubscribe(mint)

    async def _subscribe_when_ready(self, mint: str) -> None:
        if self.ws is None:
            return
        subscribed = mint in self.subscription_to_mint.values()
        pending = mint in self.pending.values()
        if not subscribed and not pending:
            await self._subscribe(mint)

    def remember_trade(self, mint: str, trade: dict[str, Any]) -> None:
        """Keep a bounded in-memory window of decoded Pump.fun trades for the active candle."""
        if not mint or not isinstance(trade, dict):
            return
        if trade.get("source") != "PUMP.FUN":
            return

        rows = self.recent_trades[mint]
        trade_id = str(trade.get("id") or trade.get("signature") or "")
        if trade_id and any(
            str(item.get("id") or item.get("signature") or "") == trade_id
            for item in reversed(rows)
        ):
            return
        rows.append(dict(trade))

    def current_candle(self, mint: str, timeframe: int = 1) -> dict[str, Any] | None:
        """Build a bounded current Pump.fun candle from the already-decoded live feed.

        This is a degraded-mode fallback only. Native Pump.fun OHLC always wins
        when it is available; this method prevents the chart from freezing when
        the native HTTP candle endpoint is unavailable/auth-protected.
        """
        try:
            span = max(60, int(timeframe or 1) * 60)
        except (TypeError, ValueError):
            span = 60

        rows = list(self.recent_trades.get(mint, ()))
        rows = [
            row for row in rows
            if row.get("source") == "PUMP.FUN"
            and isinstance(row.get("timestamp"), (int, float))
            and isinstance(row.get("price"), (int, float))
            and float(row.get("price") or 0) > 0
        ]
        if not rows:
            return None

        rows.sort(key=lambda row: (int(row["timestamp"]), str(row.get("id") or "")))
        latest_ts = int(rows[-1]["timestamp"])
        bucket = (latest_ts // span) * span
        rows = [
            row for row in rows
            if (int(row["timestamp"]) // span) * span == bucket
        ]
        if not rows:
            return None

        prices = [float(row["price"]) for row in rows]
        volume = sum(max(0.0, float(row.get("volume_sol") or 0.0)) for row in rows)

        return {
            "ts": bucket,
            "o": prices[0],
            "h": max(prices),
            "l": min(prices),
            "c": prices[-1],
            "v": volume,
            "source": "PUMP.FUN LIVE TRADES",
        }

    async def _resolve_standard_transaction(
        self,
        mint: str,
        signature: str,
        slot: int | None = None,
    ) -> None:
        """Resolve a standard logsSubscribe notification into full transaction data.

        Standard logs notifications do not include inner/top-level instruction
        bytes. Pump.fun TradeEvents can be emitted there, so fetch the single
        transaction and run the broader decoder when direct log decoding misses.
        """
        key = (mint, signature)
        if key in self._pending_resolutions:
            return

        self._pending_resolutions.add(key)

        try:
            async with self._resolve_semaphore:
                response = await self._http.post(
                    HELIUS_HTTP_RPC.format(key=self.api_key),
                    json={
                        "jsonrpc": "2.0",
                        "id": f"meme-intel-live-{signature[:12]}",
                        "method": "getTransaction",
                        "params": [
                            signature,
                            {
                                "encoding": "jsonParsed",
                                "commitment": "processed",
                                "maxSupportedTransactionVersion": 1,
                            },
                        ],
                    },
                )

            if response.status_code >= 400:
                return

            payload = response.json()
            transaction = payload.get("result")
            if not isinstance(transaction, dict):
                return

            trade = parse_live_trade_from_transaction(
                transaction,
                mint,
                signature=signature,
                slot=slot,
                block_time=transaction.get("blockTime"),
            )

            if trade:
                self.remember_trade(mint, trade)
                await self._broadcast(mint, {
                    "type": "trade",
                    "trade": trade,
                })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)[:300]
        finally:
            self._pending_resolutions.discard(key)

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.ws is None:
            return
        async with self._send_lock:
            await self.ws.send(json.dumps(payload))

    async def _subscribe(self, mint: str) -> None:
        request_id = self.request_id
        self.request_id += 1
        self.pending[request_id] = mint

        if self.stream_mode == "ENHANCED":
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "transactionSubscribe",
                "params": [
                    {
                        "accountInclude": [mint],
                        "vote": False,
                        "failed": False,
                    },
                    {
                        "commitment": "processed",
                        "encoding": "jsonParsed",
                        "transactionDetails": "full",
                        "maxSupportedTransactionVersion": 1,
                    },
                ],
            }
        else:
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "logsSubscribe",
                "params": [
                    {"mentions": [mint]},
                    {"commitment": "processed"},
                ],
            }

        await self._send(payload)

    async def _unsubscribe(self, mint: str) -> None:
        ids = [sub_id for sub_id, sub_mint in self.subscription_to_mint.items() if sub_mint == mint]
        for sub_id in ids:
            try:
                await self._send({
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "method": (
                        "transactionUnsubscribe"
                        if self.stream_mode == "ENHANCED"
                        else "logsUnsubscribe"
                    ),
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

                # Standard Solana WSS works on free Helius plans and is all
                # the chart needs because we subscribe to logs mentioning
                # exactly one mint. Enhanced transactionSubscribe is optional.
                use_enhanced = (
                    os.getenv("HELIUS_USE_ENHANCED_WS", "").strip().lower()
                    in {"1", "true", "yes"}
                    and self.enhanced_state is not False
                )
                if use_enhanced:
                    self.stream_mode = "ENHANCED"
                    url = HELIUS_ENHANCED_WS.format(key=self.api_key)
                else:
                    self.stream_mode = "STANDARD"
                    url = HELIUS_WS.format(key=self.api_key)

                async with websockets.connect(
                    url,
                    ping_interval=15,
                    ping_timeout=8,
                    close_timeout=2,
                    max_queue=4096,
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

                        if "id" in message and "error" in message:
                            failed_request = self.pending.pop(
                                int(message["id"]),
                                None
                            )
                            if (
                                failed_request and
                                self.stream_mode == "ENHANCED" and
                                self.enhanced_state is not False
                            ):
                                self.enhanced_state = False
                                self.stream_mode = "STANDARD"
                                await self._status_all(
                                    "RECONNECTING",
                                    "Enhanced WSS unavailable; using standard WSS."
                                )
                                await ws.close()
                            continue

                        if "id" in message and "result" in message:
                            mint = self.pending.pop(int(message["id"]), None)
                            if mint:
                                try:
                                    self.subscription_to_mint[int(message["result"])] = mint
                                    if self.stream_mode == "ENHANCED":
                                        self.enhanced_state = True
                                except (TypeError, ValueError):
                                    pass
                            continue

                        params = message.get("params") or {}
                        subscription = params.get("subscription")
                        mint = self.subscription_to_mint.get(subscription)
                        if not mint:
                            continue

                        if message.get("method") == "transactionNotification":
                            result = params.get("result") or {}
                            tx = result.get("transaction") or {}
                            meta = tx.get("meta") or {}

                            if meta.get("err") is not None:
                                continue

                            trade = parse_live_trade_from_transaction(
                                tx,
                                mint,
                                signature=str(result.get("signature") or ""),
                                slot=result.get("slot"),
                                block_time=result.get("blockTime"),
                            )
                        elif message.get("method") == "logsNotification":
                            result = ((params.get("result") or {}).get("value") or {})

                            if result.get("err") is not None:
                                continue

                            signature = str(result.get("signature") or "")
                            slot = (params.get("result") or {}).get("context", {}).get("slot")
                            logs = result.get("logs") or []

                            trade = parse_live_trade(
                                logs,
                                mint,
                                signature=signature,
                                slot=slot,
                            )

                            if trade:
                                self.remember_trade(mint, trade)
                                await self._broadcast(mint, {
                                    "type": "trade",
                                    "trade": trade,
                                })
                            elif signature:
                                # Standard logs notifications do not contain
                                # inner instruction bytes. Resolve the full
                                # transaction asynchronously so the websocket
                                # reader never stalls on HTTP.
                                task = asyncio.create_task(
                                    self._resolve_standard_transaction(
                                        mint,
                                        signature,
                                        slot=slot,
                                    )
                                )
                                self._pending_tasks.add(task)
                                task.add_done_callback(self._pending_tasks.discard)
                            continue
                        else:
                            continue

                        if trade:
                            self.remember_trade(mint, trade)
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
        self.stream_mode = "STANDARD"


trade_hub = LiveTradeHub()
