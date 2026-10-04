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
import socketio


HELIUS_WS = "wss://mainnet.helius-rpc.com/?api-key={key}"
HELIUS_HTTP_RPC = "https://mainnet.helius-rpc.com/?api-key={key}"
HELIUS_ENHANCED_WS = "wss://atlas-mainnet.helius-rpc.com/?api-key={key}"
PUMP_FUN_SOCKET_IO = "wss://frontend-api.pump.fun/socket.io/?EIO=4&transport=websocket"

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

                # Use the executed SOL/token ratio as the trade price.
                # Virtual-reserve state is useful for curve metadata but can
                # lag the exact execution price shown by the native trade feed.
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


def parse_pumpfun_socket_trade(raw: str) -> dict[str, Any] | None:
    """Parse Pump.fun's native Engine.IO/Socket.IO tradeCreated packet."""
    if not isinstance(raw, str) or not raw.startswith("42"):
        return None

    try:
        packet = json.loads(raw[2:])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None

    if (
        not isinstance(packet, list)
        or len(packet) < 2
        or packet[0] != "tradeCreated"
        or not isinstance(packet[1], dict)
    ):
        return None

    payload = packet[1]
    mint = str(payload.get("mint") or "").strip()
    signature = str(payload.get("signature") or "").strip()

    try:
        sol_amount = float(payload.get("sol_amount") or 0)
        token_amount = float(payload.get("token_amount") or 0)
    except (TypeError, ValueError):
        return None

    if not mint or sol_amount <= 0 or token_amount <= 0:
        return None

    price = (
        (sol_amount / 1_000_000_000)
        / (token_amount / 1_000_000)
    )
    if price <= 0:
        return None

    try:
        timestamp = int(float(payload.get("timestamp") or 0))
    except (TypeError, ValueError):
        timestamp = 0

    if timestamp > 10_000_000_000:
        timestamp //= 1000
    if timestamp < 1_500_000_000:
        timestamp = int(time.time())

    try:
        slot = int(payload.get("slot") or 0)
    except (TypeError, ValueError):
        slot = 0

    return {
        "id": signature or (
            f"pumpfun:{mint}:{timestamp}:"
            f"{payload.get('tx_index') or payload.get('txIndex') or ''}"
        ),
        "signature": signature,
        "slot": slot or None,
        "mint": mint,
        "source": "PUMP.FUN",
        "venue": PUMP_PROGRAM,
        "side": "BUY" if bool(payload.get("is_buy")) else "SELL",
        "price": price,
        "volume_sol": sol_amount / 1_000_000_000,
        "token_amount": token_amount / 1_000_000,
        "timestamp": timestamp,
        "native_pumpfun_ws": True,
    }


class LiveTradeHub:
    """One Helius WebSocket multiplexed across all selected-token clients."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("HELIUS_API_KEY")
        self.ws = None
        self.task: asyncio.Task | None = None
        self.clients: dict[str, set[Any]] = defaultdict(set)
        # A migrated Pump.fun token can trade against a PumpSwap pool without
        # the token mint being the address mentioned by the live transaction.
        # Keep every live watch address mapped back to the selected mint.
        self.watch_addresses: dict[str, set[str]] = defaultdict(set)
        self.subscription_to_mint: dict[int, str] = {}
        self._subscription_address: dict[int, str] = {}
        self.pending: dict[int, tuple[str, str]] = {}
        self.request_id = 1
        self.state = "NOT_CONFIGURED" if not self.api_key else "IDLE"
        self.last_error = ""
        self._send_lock = asyncio.Lock()
        self.enhanced_state: bool | None = None
        self.stream_mode = "STANDARD"
        self.recent_trades: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=500)
        )
        self._recovery_tasks: dict[str, asyncio.Task[Any]] = {}
        self._pumpfun_task: asyncio.Task | None = None
        self.pumpfun_live = False
        self._seen_signatures: dict[str, deque[str]] = defaultdict(
            lambda: deque(maxlen=250)
        )
        self._resolve_semaphore = asyncio.Semaphore(8)
        self._pending_resolutions: set[tuple[str, str]] = set()
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        self._subscribed_addresses: dict[str, set[str]] = defaultdict(set)
        self._pending_addresses: dict[str, set[str]] = defaultdict(set)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(2.5, connect=1.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            headers={
                "Accept": "application/json",
                "User-Agent": "Meme-Intel/live-stream",
            },
        )

    def active(self) -> bool:
        return bool(self.api_key or self.pumpfun_live)

    async def add_client(
        self,
        mint: str,
        websocket: Any,
        extra_addresses: list[str] | None = None,
    ) -> None:
        self.clients[mint].add(websocket)
        self.watch_addresses[mint].add(mint)

        for address in extra_addresses or []:
            address = str(address or "").strip()
            if address:
                self.watch_addresses[mint].add(address)

        if mint not in self._recovery_tasks or self._recovery_tasks[mint].done():
            self._recovery_tasks[mint] = asyncio.create_task(
                self._recovery_loop(mint)
            )

        if not self.task or self.task.done():
            self.task = asyncio.create_task(self._run())

        if not self._pumpfun_task or self._pumpfun_task.done():
            self._pumpfun_task = asyncio.create_task(
                self._run_pumpfun_socket()
            )

        await self._subscribe_when_ready(mint)

    async def remove_client(self, mint: str, websocket: Any) -> None:
        rows = self.clients.get(mint)
        if rows:
            rows.discard(websocket)
            if not rows:
                self.clients.pop(mint, None)
                self.watch_addresses.pop(mint, None)
                self.recent_trades.pop(mint, None)
                self._seen_signatures.pop(mint, None)

                recovery = self._recovery_tasks.pop(mint, None)
                if recovery and not recovery.done():
                    recovery.cancel()

                if not self.clients:
                    pump_task = self._pumpfun_task
                    self._pumpfun_task = None
                    if pump_task and not pump_task.done():
                        pump_task.cancel()
                    self.pumpfun_live = False

                await self._unsubscribe(mint)

    async def add_watch_address(self, mint: str, address: str) -> None:
        """Add a live market address without disturbing the existing stream."""
        address = str(address or "").strip()
        if not mint or not address or mint not in self.clients:
            return

        self.watch_addresses[mint].add(address)

        if self.ws is not None and mint in self.clients:
            subscribed_addresses = getattr(self, "_subscribed_addresses", {})
            current = subscribed_addresses.setdefault(mint, set())
            pending_addresses = getattr(self, "_pending_addresses", {})
            pending = pending_addresses.setdefault(mint, set())

            if address not in current and address not in pending:
                pending.add(address)
                await self._subscribe(mint, address)

    async def _subscribe_when_ready(self, mint: str) -> None:
        if self.ws is None:
            return

        addresses = self.watch_addresses.get(mint) or {mint}
        subscribed = getattr(self, "_subscribed_addresses", {}).setdefault(mint, set())
        pending = getattr(self, "_pending_addresses", {}).setdefault(mint, set())

        for address in addresses:
            if address in subscribed or address in pending:
                continue
            pending.add(address)
            await self._subscribe(mint, address)

    def remember_trade(self, mint: str, trade: dict[str, Any]) -> bool:
        """Remember one real trade while deduplicating identical event fingerprints."""
        if not mint or not isinstance(trade, dict):
            return False
        if trade.get("source") not in {"PUMP.FUN", "PUMPSWAP"}:
            return False

        rows = self.recent_trades[mint]

        def fingerprint(item: dict[str, Any]) -> str:
            signature = str(item.get("signature") or "").strip()
            timestamp = int(item.get("timestamp") or 0)
            price = float(item.get("price") or 0)
            side = str(item.get("side") or "")
            token_amount = float(item.get("token_amount") or 0)
            event_id = str(item.get("id") or "").strip()

            if signature:
                return (
                    f"{signature}|{timestamp}|{price:.18g}|"
                    f"{side}|{token_amount:.18g}"
                )
            return event_id or (
                f"{timestamp}|{price:.18g}|{side}|{token_amount:.18g}"
            )

        key = fingerprint(trade)
        for item in reversed(rows):
            if fingerprint(item) == key:
                return False

        rows.append(dict(trade))

        # Only Helius-recovered signatures enter the signature safety ring.
        # Native Pump.fun events are left eligible for on-chain recovery if
        # the native socket ever misses a single event.
        if (
            str(trade.get("signature") or "").strip()
            and not trade.get("native_pumpfun_ws")
        ):
            self._seen_signatures[mint].append(
                str(trade.get("signature"))
            )

        return True

    def current_candle(
        self,
        mint: str,
        timeframe: int = 1,
        max_age_seconds: int | float | None = None,
    ) -> dict[str, Any] | None:
        """Build a bounded current Pump.fun/PumpSwap candle from decoded live trades.

        This is a degraded-mode fallback only. Native Pump.fun OHLC wins whenever
        it exists; the 1s mode is intentionally trade-driven because Pump.fun's
        native OHLC endpoint is minute-based rather than a historical 1-second feed.
        """
        raw_timeframe = str(timeframe or "1").strip().lower()
        if raw_timeframe in {"1s", "1sec", "1second"}:
            span = 1
        else:
            try:
                span = max(60, int(float(raw_timeframe)) * 60)
            except (TypeError, ValueError):
                span = 60

        rows = list(self.recent_trades.get(mint, ()))
        rows = [
            row for row in rows
            if row.get("source") in {"PUMP.FUN", "PUMPSWAP"}
            and isinstance(row.get("timestamp"), (int, float))
            and isinstance(row.get("price"), (int, float))
            and float(row.get("price") or 0) > 0
        ]
        if not rows:
            return None

        rows.sort(key=lambda row: (int(row["timestamp"]), str(row.get("id") or "")))
        latest_ts = int(rows[-1]["timestamp"])

        if (
            max_age_seconds is not None
            and (
                latest_ts <= 0
                or time.time() - latest_ts > float(max_age_seconds)
            )
        ):
            return None
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

    async def publish_external_trade(
        self,
        mint: str,
        trade: dict[str, Any],
    ) -> None:
        """Publish a decoded exact-venue trade to the same live chart channel."""
        if not mint or not isinstance(trade, dict):
            return

        source = str(trade.get("source") or "").upper()
        if source not in {"PUMP.FUN", "PUMPSWAP"}:
            return

        try:
            price = float(trade.get("price") or 0)
            timestamp = int(trade.get("timestamp") or 0)
        except (TypeError, ValueError):
            return

        if price <= 0 or timestamp <= 0:
            return

        row = {
            **trade,
            "price": price,
            "timestamp": timestamp,
            "source": "PUMPSWAP" if source == "PUMPSWAP" else "PUMP.FUN",
        }
        if not self.remember_trade(mint, row):
            return
        await self._broadcast(mint, {
            "type": "trade",
            "trade": row,
        })

    def recent_trade_snapshot(
        self,
        mint: str,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        rows = list(self.recent_trades.get(mint, ()))
        rows.sort(
            key=lambda row: (
                int(row.get("timestamp") or 0),
                str(row.get("id") or ""),
            )
        )

        return [
            {
                "id": str(row.get("id") or ""),
                "signature": str(row.get("signature") or ""),
                "source": str(row.get("source") or ""),
                "side": str(row.get("side") or "BUY"),
                "price": float(row.get("price") or 0),
                "volume_sol": float(row.get("volume_sol") or 0),
                "timestamp": int(row.get("timestamp") or 0),
            }
            for row in rows[-max(1, min(int(limit or 30), 250)):]
        ]

    async def _run_pumpfun_socket(self) -> None:
        """Primary Pump.fun feed using the same Socket.IO subscription as the site."""
        backoff = 0.25

        while self.clients:
            sio = socketio.AsyncClient(
                reconnection=False,
                logger=False,
                engineio_logger=False,
                websocket_extra_options={
                    "origin": "https://pump.fun",
                },
            )

            try:
                @sio.event
                async def connect():
                    self.pumpfun_live = True
                    self.state = "LIVE"
                    self.last_error = ""
                    await self._status_all(
                        "LIVE",
                        "PUMP.FUN native tradeCreated stream",
                    )

                    # This is the native Pump.fun subscription used by current
                    # Socket.IO clients: subscribe to tradeCreated after the
                    # Socket.IO connection is established.
                    await sio.emit(
                        "subscribe",
                        "tradeCreated",
                    )

                @sio.event
                async def disconnect():
                    self.pumpfun_live = False

                @sio.on("tradeCreated")
                async def on_trade(data):
                    if not isinstance(data, dict):
                        return

                    trade = parse_pumpfun_socket_trade(
                        "42" + json.dumps(["tradeCreated", data])
                    )
                    if not trade:
                        return

                    mint = str(trade.get("mint") or "")
                    if mint not in self.clients:
                        return

                    await self.publish_external_trade(
                        mint,
                        trade,
                    )

                await sio.connect(
                    "https://frontend-api.pump.fun",
                    headers={
                        "User-Agent": "Meme-Intel/1.0",
                    },
                    transports=["websocket"],
                    socketio_path="socket.io",
                    wait_timeout=8,
                )

                backoff = 0.25
                await sio.wait()

            except asyncio.CancelledError:
                self.pumpfun_live = False
                try:
                    await sio.disconnect()
                except Exception:
                    pass
                raise
            except Exception as exc:
                self.pumpfun_live = False
                self.last_error = (
                    "PUMP_FUN_SOCKETIO:" + str(exc)[:260]
                )
                if self.clients:
                    await asyncio.sleep(backoff)
                    backoff = min(5.0, backoff * 2)
            finally:
                self.pumpfun_live = False
                try:
                    await sio.disconnect()
                except Exception:
                    pass

        self.pumpfun_live = False

    async def _recovery_loop(self, mint: str) -> None:
        """Low-rate Helius safety net for a silent/malformed websocket path."""
        initialized = False

        while mint in self.clients:
            try:
                addresses = list(self.watch_addresses.get(mint) or {mint})
                cutoff = int(time.time()) - 12
                pending = []

                # Query the mint and its PumpSwap pool. The pool is essential
                # after migration because many AMM transactions mention the
                # pool/program accounts rather than the token mint directly.
                for address in addresses:
                    signatures, err = await self._rpc(
                        "getSignaturesForAddress",
                        [
                            address,
                            {
                                "limit": 12,
                                "commitment": "processed",
                            },
                        ],
                    )

                    if err or not signatures:
                        continue

                    for item in signatures:
                        if not isinstance(item, dict):
                            continue

                        signature = str(item.get("signature") or "")
                        if not signature:
                            continue

                        if signature in self._seen_signatures[mint]:
                            continue

                        block_time = item.get("blockTime")
                        if block_time is not None and int(block_time) < cutoff:
                            continue

                        pending.append(item)

                if not pending:
                    await asyncio.sleep(0.25)
                    continue

                # On first pass, only resolve the very recent tail.
                if not initialized:
                    initialized = True
                    pending = [
                        item for item in pending
                        if item.get("blockTime") is None
                        or int(item.get("blockTime")) >= cutoff
                    ]

                sem = asyncio.Semaphore(6)

                async def resolve(item):
                    async with sem:
                        signature = str(item.get("signature") or "")
                        result, tx_err = await self._rpc(
                            "getTransaction",
                            [
                                signature,
                                {
                                    "encoding": "jsonParsed",
                                    "commitment": "processed",
                                    "maxSupportedTransactionVersion": 1,
                                },
                            ],
                        )

                        if not isinstance(result, dict):
                            result, tx_err = await self._rpc(
                                "getTransaction",
                                [
                                    signature,
                                    {
                                        "encoding": "jsonParsed",
                                        "commitment": "confirmed",
                                        "maxSupportedTransactionVersion": 1,
                                    },
                                ],
                            )

                        if tx_err or not isinstance(result, dict):
                            return None

                        return parse_live_trade_from_transaction(
                            result,
                            mint,
                            signature=signature,
                            slot=result.get("slot") or item.get("slot"),
                            block_time=result.get("blockTime") or item.get("blockTime"),
                        )

                resolved = await asyncio.gather(
                    *(resolve(item) for item in pending),
                    return_exceptions=True,
                )

                resolved_items = []
                for item, result in zip(pending, resolved):
                    signature = str(item.get("signature") or "")

                    if isinstance(result, dict):
                        # Mark a signature as seen only after it actually
                        # decoded. A transient getTransaction failure must be
                        # retried instead of permanently losing the live trade.
                        if signature:
                            self._seen_signatures[mint].append(signature)
                        resolved_items.append(result)
                    elif signature:
                        try:
                            self._seen_signatures[mint].remove(signature)
                        except ValueError:
                            pass

                for trade in sorted(
                    resolved_items,
                    key=lambda x: (
                        int(x.get("timestamp") or 0),
                        str(x.get("id") or ""),
                    ),
                ):
                    if self.remember_trade(mint, trade):
                        await self._broadcast(mint, {
                            "type": "trade",
                            "trade": trade,
                        })

                await asyncio.sleep(0.18)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)[:300]
                await asyncio.sleep(0.9)

    async def _rpc(
        self,
        method: str,
        params: list[Any],
    ) -> tuple[Any | None, str | None]:
        """Small JSON-RPC helper for the live recovery lane."""
        if not self.api_key:
            return None, "HELIUS_API_KEY_MISSING"

        try:
            response = await self._http.post(
                HELIUS_HTTP_RPC.format(key=self.api_key),
                json={
                    "jsonrpc": "2.0",
                    "id": f"meme-intel-recovery-{method}",
                    "method": method,
                    "params": params,
                },
            )

            if response.status_code >= 400:
                return None, f"HTTP_{response.status_code}"

            payload = response.json()

            if payload.get("error"):
                return None, str(payload["error"])[:240]

            return payload.get("result"), None

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return None, str(exc)[:240]

    async def _resolve_latest_address_trade(
        self,
        mint: str,
        address: str,
    ) -> None:
        """Resolve the newest transaction after an exact market-account change."""
        try:
            signatures = None

            # A processed account notification can arrive a few milliseconds
            # before the signature index catches up. Retry the tiny lookup a few
            # times instead of missing the exact trade that caused the update.
            for lookup_attempt in range(3):
                signatures, err = await self._rpc(
                    "getSignaturesForAddress",
                    [
                        address,
                        {
                            "limit": 6,
                            "commitment": "processed",
                        },
                    ],
                )

                if signatures:
                    break

                if lookup_attempt < 2:
                    await asyncio.sleep(0.045)

            if not signatures:
                return

            for item in signatures:
                if not isinstance(item, dict):
                    continue

                signature = str(item.get("signature") or "")
                if not signature:
                    continue

                if signature in self._seen_signatures[mint]:
                    continue

                result = None

                for tx_attempt, commitment in enumerate(
                    ("processed", "confirmed")
                ):
                    result, tx_err = await self._rpc(
                        "getTransaction",
                        [
                            signature,
                            {
                                "encoding": "jsonParsed",
                                "commitment": commitment,
                                "maxSupportedTransactionVersion": 1,
                            },
                        ],
                    )

                    if isinstance(result, dict):
                        break

                    if tx_attempt == 0:
                        await asyncio.sleep(0.045)

                if not isinstance(result, dict):
                    continue

                trade = parse_live_trade_from_transaction(
                    result,
                    mint,
                    signature=signature,
                    slot=result.get("slot") or item.get("slot"),
                    block_time=result.get("blockTime") or item.get("blockTime"),
                )

                if not trade:
                    # The transaction may be migration/admin noise on the
                    # watched account. Keep looking at the next newest tx.
                    continue

                self._seen_signatures[mint].append(signature)
                if self.remember_trade(mint, trade):
                    await self._broadcast(mint, {
                        "type": "trade",
                        "trade": trade,
                    })
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)[:300]

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
            transaction = None

            async with self._resolve_semaphore:
                for commitment, delay in (
                    ("processed", 0.0),
                    ("confirmed", 0.15),
                ):
                    if delay:
                        await asyncio.sleep(delay)

                    response = await self._http.post(
                        HELIUS_HTTP_RPC.format(key=self.api_key),
                        json={
                            "jsonrpc": "2.0",
                            "id": f"meme-intel-live-{signature[:12]}-{commitment}",
                            "method": "getTransaction",
                            "params": [
                                signature,
                                {
                                    "encoding": "jsonParsed",
                                    "commitment": commitment,
                                    "maxSupportedTransactionVersion": 1,
                                },
                            ],
                        },
                    )

                    if response.status_code >= 400:
                        continue

                    payload = response.json()
                    candidate = payload.get("result")
                    if isinstance(candidate, dict):
                        transaction = candidate
                        break

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

    async def _subscribe(self, mint: str, address: str | None = None) -> None:
        address = str(address or mint).strip()
        request_id = self.request_id
        self.request_id += 1
        self.pending[request_id] = (mint, address)
        self._pending_addresses.setdefault(mint, set()).add(address)

        if self.stream_mode == "ENHANCED":
            # Enhanced Helius subscriptions can include the token mint or the
            # PumpSwap pool. Both are routed back to the selected mint below.
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "transactionSubscribe",
                "params": [
                    {
                        "accountInclude": [address],
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
            # Standard Solana WebSockets do not reliably expose Pump.fun token
            # mints/pools in log text, so a mentions-filtered logs subscription
            # can sit OPEN while returning zero trades. Subscribing directly to
            # the bonding-curve / PumpSwap pool account gives us an event every
            # time the exact market state changes, without a global firehose.
            payload = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "accountSubscribe",
                "params": [
                    address,
                    {
                        "commitment": "processed",
                        "encoding": "base64",
                    },
                ],
            }

        await self._send(payload)

    async def _unsubscribe(self, mint: str) -> None:
        ids = [sub_id for sub_id, sub_mint in self.subscription_to_mint.items() if sub_mint == mint]
        self._subscribed_addresses.pop(mint, None)
        self._pending_addresses.pop(mint, None)
        for sub_id in ids:
            try:
                await self._send({
                    "jsonrpc": "2.0",
                    "id": self.request_id,
                    "method": (
                        "transactionUnsubscribe"
                        if self.stream_mode == "ENHANCED"
                        else "accountUnsubscribe"
                    ),
                    "params": [sub_id],
                })
                self.request_id += 1
            except Exception:
                pass
            self.subscription_to_mint.pop(sub_id, None)
            self._subscription_address.pop(sub_id, None)

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

                # Standard Solana WSS works on free Helius plans. In standard
                # mode we watch the exact bonding-curve / PumpSwap market
                # accounts so every state-changing trade can wake the resolver.
                # Enhanced transactionSubscribe remains the lower-latency path
                # when the configured Helius plan supports it.
                enhanced_mode = os.getenv(
                    "HELIUS_USE_ENHANCED_WS",
                    "auto",
                ).strip().lower()

                use_enhanced = (
                    enhanced_mode in {"1", "true", "yes", "auto"}
                    and enhanced_mode not in {"0", "false", "no"}
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
                    self._subscription_address.clear()
                    self.pending.clear()

                    await self._status_all("LIVE")

                    self._subscribed_addresses.clear()
                    self._pending_addresses.clear()

                    for mint in list(self.clients):
                        await self._subscribe_when_ready(mint)

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
                            if failed_request:
                                failed_mint, failed_address = failed_request
                                pending_addresses = self._pending_addresses.setdefault(
                                    failed_mint,
                                    set(),
                                )
                                pending_addresses.discard(failed_address)
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
                            pending_subscription = self.pending.pop(
                                int(message["id"]),
                                None,
                            )
                            if pending_subscription:
                                try:
                                    mint, address = pending_subscription
                                    subscription_id = int(message["result"])
                                    self.subscription_to_mint[subscription_id] = mint
                                    self._subscription_address[subscription_id] = address
                                    pending_addresses = self._pending_addresses.setdefault(
                                        mint,
                                        set(),
                                    )
                                    subscribed_addresses = self._subscribed_addresses.setdefault(
                                        mint,
                                        set(),
                                    )
                                    pending_addresses.discard(address)
                                    subscribed_addresses.add(address)
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

                        if message.get("method") == "accountNotification":
                            result = params.get("result") or {}
                            # Account notifications contain the changed account
                            # but no transaction signature. Resolve the newest
                            # transaction for the exact watched market address.
                            task = asyncio.create_task(
                                self._resolve_latest_address_trade(
                                    mint,
                                    self._subscription_address.get(subscription, mint),
                                )
                            )
                            self._pending_tasks.add(task)
                            task.add_done_callback(self._pending_tasks.discard)
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
                            if self.remember_trade(mint, trade):
                                await self._broadcast(mint, {
                                    "type": "trade",
                                    "trade": trade,
                                })

                self.ws = None
                self.subscription_to_mint.clear()
                self._subscription_address.clear()
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
