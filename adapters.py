from datetime import datetime, timezone
import asyncio
import os
import re
import time
from typing import Any

import httpx

from engine import Candle

from live_stream import parse_live_trade_from_transaction

DEXSCREENER = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
X_API = "https://api.x.com/2"
HELIUS_RPC = "https://mainnet.helius-rpc.com"
PUBLIC_SOLANA_RPCS = (
    "https://api.mainnet.solana.com",
    "https://solana-rpc.publicnode.com",
)

CA_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$([A-Za-z][A-Za-z0-9_]{1,14})\b")


class Source:
    def __init__(self, name, configured, reason=""):
        self.name = name
        self.configured = configured
        self.reason = reason


class DexScreenerAdapter:
    def __init__(self):
        self.source = Source("DexScreener", True)

    async def _get(self, path, params=None):
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(DEXSCREENER + path, params=params)
                if r.status_code >= 400:
                    return None, f"HTTP_{r.status_code}"
                return r.json(), None
        except Exception as exc:
            return None, f"DEXSCREENER_UNREACHABLE:{exc}"

    async def pairs(self, mint):
        return await self._get(
            f"/token-pairs/v1/solana/{mint}"
        )

    async def best_pair(self, mint):
        payload, err = await self.pairs(mint)

        if err:
            return None, err

        pairs = payload if isinstance(payload, list) else []

        if not pairs:
            return None, "NO_PAIRS"

        def liquidity(pair):
            try:
                return float(
                    (pair.get("liquidity") or {}).get("usd") or 0
                )
            except Exception:
                return 0.0

        pairs.sort(
            key=lambda p: (
                liquidity(p),
                1 if p.get("dexId") == "pumpswap" else 0
            ),
            reverse=True
        )

        return pairs[0], None

    async def overview(self, mint):
        pair, err = await self.best_pair(mint)

        if err:
            return None, err

        liquidity = (pair.get("liquidity") or {}).get("usd")
        volume = pair.get("volume") or {}

        data = {
            "symbol": (pair.get("baseToken") or {}).get("symbol"),
            "name": (pair.get("baseToken") or {}).get("name"),
            "price": _num(pair.get("priceUsd")),
            "liquidity": _num(liquidity),
            "marketCap": _num(pair.get("marketCap")),
            "fdv": _num(pair.get("fdv")),
            "v5mUSD": _num(volume.get("m5")),
            "v1hUSD": _num(volume.get("h1")),
            "v24hUSD": _num(volume.get("h24")),
            "dexId": pair.get("dexId"),
            "pairAddress": pair.get("pairAddress"),
            "pairCreatedAt": pair.get("pairCreatedAt"),
            "priceChange": pair.get("priceChange") or {},
            "txns": pair.get("txns") or {},
        }

        return {"data": data}, None

    async def creation(self, mint):
        pair, err = await self.best_pair(mint)

        if err:
            return None, err

        return {
            "data": {
                "pairCreatedAt": pair.get("pairCreatedAt"),
                "pairAddress": pair.get("pairAddress"),
                "dexId": pair.get("dexId"),
            }
        }, None

    async def security(self, mint):
        return None, "NOT_AVAILABLE"


class GeckoTerminalAdapter:
    def __init__(self):
        self.source = Source("GeckoTerminal", True)
        self._pool_cache = {}
        self._response_cache = {}

    async def _get(self, path, params=None):
        params = params or {}

        cache_key = (
            path,
            tuple(sorted((str(k), str(v)) for k, v in params.items())),
        )

        cached = self._response_cache.get(cache_key)
        if cached:
            cached_at, payload = cached
            if time.time() - cached_at < 15:
                return payload, None

        last_error = None

        for attempt in range(4):
            try:
                async with httpx.AsyncClient(
                    timeout=15,
                    headers={
                        "User-Agent": "Meme-Intel/1.0"
                    },
                ) as c:
                    r = await c.get(
                        GECKO + path,
                        params=params,
                    )

                if r.status_code == 200:
                    payload = r.json()
                    self._response_cache[cache_key] = (
                        time.time(),
                        payload,
                    )
                    return payload, None

                last_error = f"HTTP_{r.status_code}"

                if r.status_code == 429 or r.status_code >= 500:
                    retry_after = r.headers.get("Retry-After")

                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        delay = 2 ** attempt

                    await asyncio.sleep(
                        min(max(delay, 1.0), 10.0)
                    )
                    continue

                return None, last_error

            except Exception as exc:
                last_error = str(exc)

                if attempt < 3:
                    await asyncio.sleep(2 ** attempt)
                    continue

                return None, last_error

        return None, last_error or "GECKO_REQUEST_FAILED"

    async def best_pool(self, mint):
        cached = self._pool_cache.get(mint)
        if cached and time.time() - cached["time"] < 60:
            return cached["pool"], None

        payload, err = await self._get(
            f"/networks/solana/tokens/{mint}/pools",
            {"page": 1}
        )

        if err:
            return None, err

        pools = (payload or {}).get("data", [])

        if not pools:
            return None, "NO_POOLS"

        def reserve(pool):
            try:
                return float(
                    (pool.get("attributes") or {}).get(
                        "reserve_in_usd"
                    ) or 0
                )
            except Exception:
                return 0.0

        def volume(pool):
            try:
                return float(
                    ((pool.get("attributes") or {}).get("volume_usd") or {}).get("h24")
                    or 0
                )
            except Exception:
                return 0.0

        def is_pumpswap(pool):
            attrs = pool.get("attributes") or {}
            relationships = pool.get("relationships") or {}
            exchange = relationships.get("exchange") or {}
            exchange_data = exchange.get("data") or {}
            exchange_id = str(
                exchange_data.get("id")
                or attrs.get("dex_id")
                or attrs.get("exchange_id")
                or attrs.get("name")
                or ""
            ).lower().replace("-", "_").replace(" ", "_")
            return (
                "pumpswap" in exchange_id
                or exchange_id in {"pump_swap", "pump.fun", "pumpfun"}
            )

        # For Pump.fun tokens that migrated, prefer the PumpSwap pool even if
        # another DEX has slightly more liquidity. This keeps the fallback chart
        # on the token's native Pump.fun trading venue.
        pools.sort(
            key=lambda p: (
                1 if is_pumpswap(p) else 0,
                reserve(p),
                volume(p),
            ),
            reverse=True,
        )

        pool = (pools[0].get("attributes") or {}).get("address")

        if not pool:
            return None, "NO_POOL_ADDRESS"

        self._pool_cache[mint] = {
            "pool": pool,
            "time": time.time()
        }

        return pool, None

    async def candles(self, mint, interval="5m"):
        pool, err = await self.best_pool(mint)

        if err:
            return None, err

        aggregate = {
            "1m": 1,
            "5m": 5
        }.get(interval)

        if aggregate is None:
            return None, "UNSUPPORTED_INTERVAL"

        limit = 1000 if aggregate == 1 else 500

        payload, err = await self._get(
            f"/networks/solana/pools/{pool}/ohlcv/minute",
            {
                "aggregate": aggregate,
                "limit": limit
            }
        )

        if err:
            return None, err

        return payload, None


class PumpFunAdapter:
    """Direct read-only Pump.fun candle feed used for the chart.

    Pump.fun's frontend chart endpoint returns 1-minute OHLC candles for a mint.
    We keep a very short cache so repeated chart refreshes do not hammer the
    upstream while still keeping the currently-forming candle fresh.
    """

    def __init__(self):
        self.source = Source("Pump.fun", True)
        self._cache = {}
        self._cache_ttl = 0.50
        self.base_urls = (
            "https://frontend-api-v3.pump.fun",
            "https://frontend-api.pump.fun",
        )
        self._client = httpx.AsyncClient(
            timeout=3.5,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; MemeIntel/2.3)",
                "Accept": "application/json",
                "Referer": "https://pump.fun/",
                "Origin": "https://pump.fun",
                **(
                    {"Authorization": f"Bearer {os.getenv('PUMP_FUN_JWT')}"}
                    if os.getenv("PUMP_FUN_JWT")
                    else {}
                ),
            },
        )

    async def coin(self, mint, fresh=False):
        """Fetch Pump.fun's current coin record for exact market-cap metadata."""
        mint = (mint or "").strip()
        if not mint:
            return None, "INVALID_MINT"

        cache_key = ("coin", mint)
        now = time.time()
        cached = self._cache.get(cache_key)

        if (
            not fresh and
            cached and
            now - cached["time"] < 0.75
        ):
            return cached["payload"], None

        last_error = None

        # Use the currently documented v3 coin endpoint first, then the
        # legacy frontend hostname as a compatibility fallback.
        urls = (
            f"{self.base_urls[0]}/coins-v2/{mint}",
            f"{self.base_urls[0]}/coins-v2/{mint}?sync=true",
            f"{self.base_urls[0]}/coins/{mint}?sync=true",
            f"{self.base_urls[0]}/coins/{mint}",
            f"{self.base_urls[1]}/coins/{mint}?sync=true",
            f"{self.base_urls[1]}/coins/{mint}",
        )

        for url in urls:
            try:
                r = await self._client.get(url)

                if r.status_code >= 400:
                    last_error = f"HTTP_{r.status_code}"
                    continue

                payload = r.json()

                if isinstance(payload, dict):
                    self._cache[cache_key] = {
                        "time": time.time(),
                        "payload": payload,
                    }
                    return payload, None

                last_error = "INVALID_COIN_RESPONSE"
            except Exception as exc:
                last_error = str(exc)

        if last_error in {"HTTP_401", "HTTP_403"} and not os.getenv("PUMP_FUN_JWT"):
            return None, "PUMPFUN_COIN_AUTH_REQUIRED"

        return None, last_error or "PUMPFUN_COIN_UNAVAILABLE"

    async def trades(
        self,
        mint,
        limit=200,
        offset=0,
        minimum_size=0,
        fresh=False,
    ):
        """Fetch Pump.fun's own trade history for fast historical reconstruction."""
        mint = (mint or "").strip()
        limit = max(25, min(int(limit or 200), 200))
        offset = max(0, int(offset or 0))
        minimum_size = max(0, int(minimum_size or 0))

        key = (
            "trades",
            mint,
            limit,
            offset,
            minimum_size,
        )
        now = time.time()

        cached = self._cache.get(key)
        if (
            not fresh and
            cached and
            now - cached["time"] < 1.0
        ):
            return cached["payload"], None

        params = {
            "limit": limit,
            "offset": offset,
            "minimumSize": minimum_size,
        }

        last_error = None

        for base in self.base_urls:
            try:
                r = await self._client.get(
                    f"{base}/trades/all/{mint}",
                    params=params,
                )

                if r.status_code >= 400:
                    last_error = f"HTTP_{r.status_code}"
                    continue

                def unwrap_trade_payload(value):
                    if isinstance(value, list):
                        return value

                    if isinstance(value, dict):
                        for field in (
                            "data",
                            "trades",
                            "results",
                            "items",
                            "rows",
                        ):
                            nested = value.get(field)
                            if isinstance(nested, list) and nested:
                                return nested
                            if isinstance(nested, dict):
                                rows = unwrap_trade_payload(nested)
                                if rows:
                                    return rows

                    return []

                payload = unwrap_trade_payload(r.json())

                if isinstance(payload, list):
                    self._cache[key] = {
                        "time": time.time(),
                        "payload": payload,
                    }
                    return payload, None

                last_error = "INVALID_TRADE_RESPONSE"
            except Exception as exc:
                last_error = str(exc)

        if last_error in {"HTTP_401", "HTTP_403"} and not os.getenv("PUMP_FUN_JWT"):
            return None, "PUMPFUN_TRADE_HISTORY_AUTH_REQUIRED"

        return None, last_error or "PUMPFUN_TRADE_HISTORY_UNAVAILABLE"

    async def candles(self, mint, limit=1000, timeframe=1, offset=0, fresh=False):
        mint = (mint or "").strip()
        limit = max(25, min(int(limit or 1000), 1000))
        timeframe = int(timeframe or 1)
        offset = max(0, int(offset or 0))
        key = (mint, limit, timeframe, offset)
        now = time.time()
        cached = self._cache.get(key)
        if (
            not fresh and
            cached and
            now - cached["time"] < self._cache_ttl
        ):
            return cached["payload"], None

        params = {
            "offset": offset,
            "limit": limit,
            "timeframe": timeframe,
        }
        last_error = None

        try:
            for base in self.base_urls:
                try:
                    r = await self._client.get(
                        f"{base}/candlesticks/{mint}",
                        params=params,
                    )
                    if r.status_code >= 400:
                        last_error = f"HTTP_{r.status_code}"
                        continue
                    payload = r.json()
                    self._cache[key] = {"time": time.time(), "payload": payload}
                    return payload, None
                except Exception as exc:
                    last_error = str(exc)
        except Exception as exc:
            last_error = str(exc)

        if last_error in {"HTTP_401", "HTTP_403"} and not os.getenv("PUMP_FUN_JWT"):
            return None, "PUMPFUN_CHART_AUTH_REQUIRED"
        return None, last_error or "PUMPFUN_CHART_UNAVAILABLE"


def public_rpc_endpoints():
    return PUBLIC_SOLANA_RPCS


class HeliusAdapter:
    def __init__(self, key=None):
        self.key = key or os.getenv("HELIUS_API_KEY")
        self.source = Source(
            "Helius",
            bool(self.key),
            "HELIUS_API_KEY missing" if not self.key else ""
        )
        self._chart_cache = {}
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=3.0),
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            headers={
                "Accept": "application/json",
                "User-Agent": "Meme-Intel/2.5",
            },
        )

    async def _rpc(self, method, params, rpc_base=None):
        if rpc_base:
            url = rpc_base
        else:
            if not self.key:
                return None, "NOT_CONFIGURED"
            url = f"{HELIUS_RPC}/?api-key={self.key}"
        last_error = None

        for attempt in range(3):
            try:
                r = await self._client.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": "meme-intel",
                        "method": method,
                        "params": params,
                    },
                )

                if r.status_code == 429 or r.status_code >= 500:
                    last_error = f"HTTP_{r.status_code}"
                    retry_after = r.headers.get("Retry-After")
                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        delay = 0.35 * (2 ** attempt)
                    await asyncio.sleep(min(max(delay, 0.2), 2.0))
                    continue

                if r.status_code >= 400:
                    return None, f"HTTP_{r.status_code}"

                payload = r.json()
                if payload.get("error"):
                    return None, str(payload["error"])
                return payload.get("result"), None

            except Exception as exc:
                last_error = str(exc)
                if attempt < 2:
                    await asyncio.sleep(0.25 * (2 ** attempt))

        return None, last_error or "HELIUS_RPC_FAILED"

    async def _transactions_for_address(
        self,
        address: str,
        cutoff: int,
        limit: int = 100,
        max_pages: int = 2,
        rpc_base: str | None = None,
    ):
        """Fast Helius archival backfill using getTransactionsForAddress when available."""
        if not address:
            return [], "INVALID_ADDRESS"

        rows = []
        pagination = None
        last_error = None

        for _ in range(max(1, int(max_pages or 1))):
            params = {
                "transactionDetails": "full",
                "sortOrder": "desc",
                "limit": min(100, max(1, int(limit or 100) - len(rows))),
                "filters": {
                    "status": "succeeded",
                    "blockTime": {
                        "gte": int(cutoff),
                        "lte": int(time.time()) + 2,
                    },
                },
            }

            if pagination:
                params["paginationToken"] = pagination

            result, err = await self._rpc(
                "getTransactionsForAddress",
                [address, params],
                rpc_base=rpc_base,
            )

            if err:
                last_error = err
                break

            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list) or not data:
                break

            rows.extend(
                item for item in data
                if isinstance(item, dict)
            )

            pagination = (
                result.get("paginationToken")
                if isinstance(result, dict)
                else None
            )

            if not pagination or len(rows) >= int(limit or 100):
                break

        return rows, last_error

    def _aggregate_decoded_trade_candles(
        self,
        trades: list[dict[str, Any]],
        span: int,
        cutoff: int,
    ):
        buckets = {}

        for trade in trades:
            ts = int(trade.get("timestamp") or 0)
            price = float(trade.get("price") or 0)

            if (
                ts <= 0
                or price <= 0
                or ts < cutoff
            ):
                continue

            bucket = (ts // span) * span
            row = buckets.get(bucket)

            if row is None:
                buckets[bucket] = {
                    "ts": bucket,
                    "o": price,
                    "h": price,
                    "l": price,
                    "c": price,
                    "v": float(trade.get("volume_sol") or 0),
                    "_first_ts": ts,
                    "_last_ts": ts,
                }
                continue

            row["h"] = max(row["h"], price)
            row["l"] = min(row["l"], price)
            row["v"] += float(trade.get("volume_sol") or 0)

            if ts < row["_first_ts"]:
                row["_first_ts"] = ts
                row["o"] = price

            if ts >= row["_last_ts"]:
                row["_last_ts"] = ts
                row["c"] = price

        candles = [
            Candle(
                ts=int(row["ts"]),
                o=float(row["o"]),
                h=float(row["h"]),
                l=float(row["l"]),
                c=float(row["c"]),
                v=float(row["v"]),
            )
            for row in buckets.values()
        ]
        candles.sort(key=lambda x: x.ts)
        return candles

    async def historical_trade_candles(
        self,
        mint: str,
        timeframe: int | str = 1,
        lookback_minutes: int = 120,
        max_signatures: int = 1500,
        rpc_base: str | None = None,
        extra_addresses: list[str] | None = None,
    ):
        """Rebuild real Pump.fun/PumpSwap OHLC from on-chain trades."""
        if not self.key and not rpc_base:
            return [], "NOT_CONFIGURED"

        mint = (mint or "").strip()
        raw_timeframe = str(timeframe or "1").strip().lower()

        if raw_timeframe in {"1s", "1sec", "1second"}:
            span = 1
            timeframe_key = "1s"
            lookback_minutes = max(
                5,
                min(int(lookback_minutes or 10), 10080),
            )
        else:
            try:
                minutes = int(float(raw_timeframe))
            except (TypeError, ValueError):
                return [], "UNSUPPORTED_TIMEFRAME"

            if minutes not in {1, 5, 15, 60}:
                return [], "UNSUPPORTED_TIMEFRAME"

            span = minutes * 60
            timeframe_key = str(minutes)
            lookback_minutes = max(
                30,
                min(int(lookback_minutes or 120), 10080),
            )

        max_signatures = max(100, min(int(max_signatures or 1500), 1500))

        addresses = [mint]
        for address in extra_addresses or []:
            address = str(address or "").strip()
            if address and address not in addresses:
                addresses.append(address)

        cache_key = (
            mint,
            tuple(addresses[1:]),
            timeframe_key,
            lookback_minutes,
            max_signatures,
            rpc_base or "helius",
        )
        cached = self._chart_cache.get(cache_key)
        if cached and time.time() - cached["time"] < 20:
            return cached["candles"], cached["error"]

        cutoff = int(time.time()) - lookback_minutes * 60

        # Helius' archival transaction endpoint is dramatically cheaper/faster
        # than resolving hundreds of individual signatures. Use it first for
        # 1-second history when the normal Helius key is available, then fall
        # back to the older signature/getTransaction path for free/public RPCs.
        if self.key and not rpc_base and timeframe_key == "1s":
            archival_rows = []
            per_address_limit = min(
                200,
                max(100, (max_signatures + len(addresses) - 1) // len(addresses)),
            )

            for address in addresses:
                page_rows, _ = await self._transactions_for_address(
                    address,
                    cutoff,
                    limit=per_address_limit,
                    max_pages=2,
                    rpc_base=None,
                )
                archival_rows.extend(page_rows)

            if archival_rows:
                seen_tx = set()
                archival_trades = []

                for transaction in archival_rows:
                    signature = str(transaction.get("signature") or "")
                    if signature and signature in seen_tx:
                        continue
                    if signature:
                        seen_tx.add(signature)

                    block_time = transaction.get("blockTime")
                    trade = parse_live_trade_from_transaction(
                        transaction,
                        mint,
                        signature=signature,
                        slot=transaction.get("slot"),
                        block_time=block_time,
                    )
                    if trade:
                        archival_trades.append(trade)

                archival_candles = self._aggregate_decoded_trade_candles(
                    archival_trades,
                    span,
                    cutoff,
                )

                if archival_candles:
                    self._chart_cache[cache_key] = {
                        "time": time.time(),
                        "candles": archival_candles,
                        "error": None,
                    }
                    return archival_candles, None

        rows = []
        seen = set()

        # Query the token mint and any PumpSwap pool with a balanced
        # per-address budget. A migrated token can have many ordinary mint
        # signatures that would otherwise exhaust the whole history budget before
        # the AMM pool is examined.
        per_address_cap = max(
            100,
            (max_signatures + len(addresses) - 1) // len(addresses),
        )

        for address in addresses:
            before = None
            address_rows = 0

            while address_rows < per_address_cap and len(rows) < max_signatures:
                page_limit = min(
                    100 if rpc_base else 1000,
                    per_address_cap - address_rows,
                    max_signatures - len(rows),
                )
                params = {
                    "limit": page_limit,
                    "commitment": "confirmed",
                }
                if before:
                    params["before"] = before

                signatures, sig_err = await self._rpc(
                    "getSignaturesForAddress",
                    [address, params],
                    rpc_base=rpc_base,
                )

                if sig_err or not signatures:
                    # One address can legitimately have no history even when
                    # the associated PumpSwap pool does. Continue to the other
                    # address before declaring the token history unavailable.
                    if rows:
                        break
                    continue

                page = [
                    item
                    for item in signatures
                    if isinstance(item, dict) and item.get("signature")
                ]

                for item in page:
                    signature = str(item["signature"])
                    if signature in seen:
                        continue
                    seen.add(signature)
                    rows.append(item)
                    address_rows += 1

                block_times = [
                    int(item.get("blockTime"))
                    for item in page
                    if item.get("blockTime") is not None
                ]

                if block_times and min(block_times) <= cutoff:
                    break

                if len(page) < page_limit:
                    break

                before = str(page[-1]["signature"])

        rows = [
            item
            for item in rows
            if item.get("blockTime") is None
            or int(item.get("blockTime")) >= cutoff
        ]

        trades = []
        sem = asyncio.Semaphore(20)

        async def load_one(item):
            async with sem:
                signature = str(item["signature"])
                result, err = await self._rpc(
                    "getTransaction",
                    [
                        signature,
                        {
                            "encoding": "jsonParsed",
                            "commitment": "confirmed",
                            "maxSupportedTransactionVersion": 1,
                        },
                    ],
                    rpc_base=rpc_base,
                )
                if err or not result:
                    return None

                return parse_live_trade_from_transaction(
                    result,
                    mint,
                    signature=signature,
                    slot=result.get("slot") or item.get("slot"),
                    block_time=result.get("blockTime") or item.get("blockTime"),
                )

        results = await asyncio.gather(
            *(load_one(item) for item in rows),
            return_exceptions=True,
        )

        trades = [
            trade
            for trade in results
            if isinstance(trade, dict)
        ]
        trades.sort(
            key=lambda x: (
                int(x.get("timestamp") or 0),
                str(x.get("id") or ""),
            )
        )

        candles = self._aggregate_decoded_trade_candles(
            trades,
            span,
            cutoff,
        )

        error = None if candles else "NO_TRADES_DECODED"

        # Never cache an empty historical decode. A token can be between
        # Pump.fun bonding-curve and PumpSwap migration, or an RPC request can
        # transiently return no decodable transactions. Caching that empty
        # result was enough to keep a healthy chart blank for 20 seconds.
        if candles:
            self._chart_cache[cache_key] = {
                "time": time.time(),
                "candles": candles,
                "error": error,
            }

        return candles, error

    async def asset(self, mint):
        if not self.key:
            return None, "NOT_CONFIGURED"

        return await self._rpc(
            "getAsset",
            {
                "id": mint,
                "displayOptions": {
                    "showFungible": True
                }
            },
        )

    async def security(self, mint):
        if not self.key:
            return None, "NOT_CONFIGURED"

        account_task = self._rpc(
            "getAccountInfo",
            [
                mint,
                {"encoding": "jsonParsed"},
            ],
        )

        asset_task = self.asset(mint)
        account, account_err = await account_task
        asset, asset_err = await asset_task

        parsed = (
            (account or {})
            .get("value", {})
            .get("data", {})
            .get("parsed", {})
            .get("info", {})
        )

        mint_authority = parsed.get("mintAuthority")
        freeze_authority = parsed.get("freezeAuthority")
        token_extensions = []
        for extension in parsed.get("extensions") or []:
            if isinstance(extension, dict):
                value = extension.get("extension")
                if value:
                    token_extensions.append(str(value))
        token_program = ((account or {}).get("value") or {}).get("owner")

        supply = (
            ((asset or {}).get("token_info") or {}).get("supply")
        )

        try:
            supply = int(supply) if supply is not None else 0
        except Exception:
            supply = 0

        token_accounts = []
        page = 1
        max_pages = 5

        while page <= max_pages:
            holders, holders_err = await self._rpc(
                "getTokenAccounts",
                {
                    "mint": mint,
                    "page": page,
                    "limit": 1000,
                    "displayOptions": {},
                },
            )

            if holders_err:
                if page == 1 and not account and not asset:
                    return None, holders_err
                break

            rows = (holders or {}).get("token_accounts") or []
            token_accounts.extend(rows)

            if len(rows) < 1000:
                break

            page += 1

        if account_err and not parsed:
            return None, account_err
        if asset_err and not supply:
            return None, asset_err
        if not token_accounts:
            return None, "NO_HOLDER_ACCOUNTS"

        owner_balances = {}
        sampled_amount = 0

        for row in token_accounts:
            owner = row.get("owner")
            try:
                amount = int(row.get("amount") or 0)
            except Exception:
                amount = 0

            if owner:
                owner_balances[owner] = owner_balances.get(owner, 0) + amount

            sampled_amount += amount

        ranked = sorted(
            owner_balances.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        if supply <= 0:
            supply = sampled_amount

        coverage_ratio = (
            sampled_amount / supply
            if supply > 0
            else 0
        )

        top_share = (
            ranked[0][1] / supply
            if ranked and supply > 0
            else None
        )

        top5_share = (
            sum(v for _, v in ranked[:5]) / supply
            if ranked and supply > 0
            else None
        )

        top10_share = (
            sum(v for _, v in ranked[:10]) / supply
            if ranked and supply > 0
            else None
        )

        return {
            "state": "READY",
            "asset": asset,
            "mint_authority": bool(mint_authority),
            "freeze_authority": bool(freeze_authority),
            "holder_count": len(ranked),
            "top_holder_share": top_share,
            "top5_holder_share": top5_share,
            "top10_holder_share": top10_share,
            "token_program": token_program,
            "token_extensions": token_extensions,
            "sampled_accounts": len(token_accounts),
            "sampled_supply": sampled_amount,
            "coverage_ratio": coverage_ratio,
            "supply": supply,
            "pages_scanned": page,
            "coverage_complete": coverage_ratio >= 0.95,
            "top_holders": [
                {
                    "owner": owner,
                    "raw_amount": amount,
                    "share": amount / supply if supply > 0 else None,
                }
                for owner, amount in ranked[:10]
            ],
            "warnings": [
                warning for warning in [
                    "MINT_AUTHORITY_ACTIVE" if mint_authority else None,
                    "FREEZE_AUTHORITY_ACTIVE" if freeze_authority else None,
                    "TOP_HOLDER_OVER_50PCT" if top_share is not None and top_share > 0.50 else None,
                    "TOP10_OVER_70PCT" if top10_share is not None and top10_share > 0.70 else None,
                    "HOLDER_COVERAGE_INCOMPLETE" if coverage_ratio < 0.95 else None,
                ]
                if warning
            ],
        }, None



class JupiterAdapter:
    WSOL = "So11111111111111111111111111111111111111112"
    BASE = "https://lite-api.jup.ag/swap/v1/quote"

    def __init__(self):
        self.source = Source("Jupiter", True)
        self._cache = {}

    async def sell_probe(
        self,
        mint: str,
        decimals: int | None,
        supply: int | float | None,
        amount_ui: float | None = None,
    ):
        mint = (mint or "").strip()
        if not mint or mint == self.WSOL:
            return None, "INVALID_SELL_PROBE"

        cached = self._cache.get(mint)
        if cached and time.time() - cached["time"] < 12:
            return cached["data"], cached["error"]

        try:
            decimals = int(decimals if decimals is not None else 6)
            total_supply_ui = (
                float(supply) / (10 ** decimals)
                if supply is not None
                else 0
            )

            if amount_ui is None:
                # Small deterministic probe: enough to test routing without
                # making the quote meaningfully dependent on whale size.
                amount_ui = min(
                    max(total_supply_ui * 0.00001, 1.0),
                    total_supply_ui * 0.001 if total_supply_ui > 0 else 10000.0,
                )

            amount_atomic = max(
                1,
                int(round(amount_ui * (10 ** decimals))),
            )

            params = {
                "inputMint": mint,
                "outputMint": self.WSOL,
                "amount": amount_atomic,
                "slippageBps": 100,
                "swapMode": "ExactIn",
            }

            async with httpx.AsyncClient(
                timeout=3.5,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Meme-Intel/2.2",
                },
            ) as c:
                r = await c.get(self.BASE, params=params)

            if r.status_code == 200:
                payload = r.json()
                route = payload.get("routePlan") or []
                result = {
                    "state": "ROUTE_FOUND" if route else "NO_ROUTE",
                    "probe_tokens": amount_ui,
                    "in_amount": payload.get("inAmount"),
                    "out_amount": payload.get("outAmount"),
                    "price_impact_pct": _num(payload.get("priceImpactPct")),
                    "route_count": len(route),
                }
                self._cache[mint] = {
                    "time": time.time(),
                    "data": result,
                    "error": None,
                }
                return result, None

            error = f"HTTP_{r.status_code}"
        except Exception as exc:
            error = str(exc)

        result = {
            "state": "UNAVAILABLE",
        }
        self._cache[mint] = {
            "time": time.time(),
            "data": result,
            "error": error,
        }
        return result, error


class RugCheckAdapter:
    """Read-only RugCheck token report with a short cache.

    The public report includes normalized risk, detected risks, holder/creator
    information, markets, liquidity and LP-lock information when available.
    """

    BASE = "https://api.rugcheck.xyz"

    def __init__(self):
        self.source = Source("RugCheck", True)
        self._cache = {}

    async def report(self, mint: str):
        mint = (mint or "").strip()
        cached = self._cache.get(mint)
        if cached and time.time() - cached["time"] < 30:
            return cached["data"], cached["error"]

        urls = (
            f"{self.BASE}/v1/tokens/{mint}/report",
            f"{self.BASE}/v1/tokens/{mint}/report/summary",
        )
        last_error = None

        try:
            async with httpx.AsyncClient(
                timeout=4.0,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Meme-Intel/2.2",
                },
            ) as c:
                for url in urls:
                    try:
                        r = await c.get(url)
                        if r.status_code == 200:
                            payload = r.json()
                            if isinstance(payload, dict):
                                self._cache[mint] = {
                                    "time": time.time(),
                                    "data": payload,
                                    "error": None,
                                }
                                return payload, None

                        last_error = f"HTTP_{r.status_code}"
                    except Exception as exc:
                        last_error = str(exc)

        except Exception as exc:
            last_error = str(exc)

        self._cache[mint] = {
            "time": time.time(),
            "data": None,
            "error": last_error or "RUGCHECK_UNAVAILABLE",
        }
        return None, last_error or "RUGCHECK_UNAVAILABLE"


class XAdapter:
    def __init__(self, token=None):
        self.token = token or os.getenv("X_BEARER_TOKEN")
        self.source = Source(
            "X",
            bool(self.token),
            "X_BEARER_TOKEN missing" if not self.token else ""
        )
        self.last_state = "CONFIGURED" if self.token else "NOT_CONFIGURED"
        self.last_error = ""
        self._cache = {}

    def _cache_key(self, query, max_results):
        return (str(query or ""), int(max_results))

    def _cached(self, key):
        item = self._cache.get(key)
        if not item:
            return None
        ts, payload, err, ttl = item
        if time.time() - ts <= ttl:
            return payload, err
        self._cache.pop(key, None)
        return None

    async def recent(self, query, max_results=50):
        if not self.token:
            self.last_state = "NOT_CONFIGURED"
            self.last_error = "X_BEARER_TOKEN missing"
            return None, "NOT_CONFIGURED"

        max_results = min(max(10, int(max_results)), 100)
        key = self._cache_key(query, max_results)

        cached = self._cached(key)
        if cached:
            payload, err = cached
            return payload, err

        params = {
            "query": query,
            "max_results": max_results,
            "tweet.fields": "created_at,public_metrics,author_id,lang,entities,referenced_tweets,in_reply_to_user_id",
            "expansions": "author_id,referenced_tweets.id",
            "user.fields": "name,username,profile_image_url,public_metrics,verified,verified_type,created_at,description"
        }

        headers = {
            "Authorization": f"Bearer {self.token}"
        }

        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    f"{X_API}/tweets/search/recent",
                    params=params,
                    headers=headers
                )

            if r.status_code == 200:
                payload = r.json()
                self.last_state = "READY"
                self.last_error = ""
                self._cache[key] = (time.time(), payload, None, 30)
                return payload, None

            if r.status_code == 402:
                err = "X_CREDITS_DEPLETED"
                self.last_state = err
                self.last_error = r.text[:300]
                self._cache[key] = (time.time(), None, err, 300)
                return None, err

            if r.status_code in {401, 403}:
                err = f"X_AUTH_{r.status_code}"
                self.last_state = err
                self.last_error = r.text[:300]
                self._cache[key] = (time.time(), None, err, 120)
                return None, err

            if r.status_code == 429:
                err = "X_RATE_LIMITED"
                self.last_state = err
                self.last_error = r.text[:300]
                self._cache[key] = (time.time(), None, err, 60)
                return None, err

            err = f"X_HTTP_{r.status_code}"
            self.last_state = err
            self.last_error = r.text[:300]
            self._cache[key] = (time.time(), None, err, 30)
            return None, err

        except Exception as exc:
            self.last_state = "X_UNREACHABLE"
            self.last_error = str(exc)
            return None, "X_UNREACHABLE"

def _num(value):
    try:
        if value is None:
            return None

        return float(value)

    except Exception:
        return None


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def extract_cas(text: str):
    return CA_RE.findall(text or "")


def extract_tickers(text: str):
    return [x.upper() for x in TICKER_RE.findall(text or "")]


def x_items(payload):
    if not payload:
        return []

    users = {
        u["id"]: u
        for u in payload.get("includes", {}).get("users", [])
        if u.get("id")
    }

    items = []
    for t in payload.get("data", []):
        author = users.get(t.get("author_id"), {})
        pm = t.get("public_metrics", {}) or {}
        text_value = t.get("text", "")
        refs = t.get("referenced_tweets") or []
        followers = ((author.get("public_metrics") or {}).get("followers_count") or 0)

        engagement = (
            int(pm.get("like_count", 0))
            + int(pm.get("retweet_count", 0)) * 2
            + int(pm.get("quote_count", 0)) * 3
            + int(pm.get("reply_count", 0))
        )

        items.append({
            "id": t.get("id"),
            "text": text_value,
            "created_at": t.get("created_at"),
            "username": author.get("username", ""),
            "name": author.get("name", ""),
            "followers": int(followers),
            "verified": bool(author.get("verified")),
            "verified_type": author.get("verified_type"),
            "likes": pm.get("like_count", 0),
            "reposts": pm.get("retweet_count", 0),
            "replies": pm.get("reply_count", 0),
            "quotes": pm.get("quote_count", 0),
            "engagement": engagement,
            "cas": extract_cas(text_value),
            "tickers": extract_tickers(text_value),
            "url": "https://x.com/" + author.get("username", "i") + "/status/" + str(t.get("id")),
        })

    return items


def _log10_score(value):
    try:
        return min(100.0, max(0.0, __import__("math").log10(max(1, value + 1)) * 20))
    except Exception:
        return 0.0



def social_metrics(items):
    if not items:
        return {
            "state": "NO_POSTS",
            "available": False,
            "post_count": 0,
            "recent_15m": 0,
            "recent_5m": 0,
            "mention_velocity": None,
            "velocity_acceleration": None,
            "sentiment": None,
            "domination": None,
            "coordination_risk": None,
            "duplicates": None,
            "duplicate_ratio": None,
            "unique_author_count": 0,
            "author_quality": None,
            "engagement_velocity": None,
            "reach_proxy": None,
            "top_authors": [],
        }

    now = datetime.now(timezone.utc)
    dated = []
    for item in items:
        dt = _parse_dt(item.get("created_at"))
        if dt is None:
            continue
        age = (now - dt).total_seconds()
        if age >= 0:
            item["age_seconds"] = round(age, 1)
            dated.append(item)

    recent_15 = [x for x in dated if x["age_seconds"] <= 15 * 60]
    recent_5 = [x for x in dated if x["age_seconds"] <= 5 * 60]
    prior_10 = [x for x in dated if 5 * 60 < x["age_seconds"] <= 15 * 60]

    velocity = len(recent_15) / 15.0
    fast_velocity = len(recent_5) / 5.0
    prior_velocity = len(prior_10) / 10.0
    acceleration = fast_velocity / max(prior_velocity, 0.05)

    author_counts = {}
    for item in recent_15:
        username = item.get("username") or "unknown"
        author_counts[username] = author_counts.get(username, 0) + 1

    unique_authors = len(author_counts)
    domination = max(author_counts.values(), default=0) / max(1, len(recent_15))

    texts = [re.sub(r"\W+", " ", x.get("text", "").lower()).strip() for x in recent_15]
    unique_texts = set(texts)
    duplicates = len(texts) - len(unique_texts)
    duplicate_ratio = duplicates / max(1, len(texts))

    positive = {"bullish", "moon", "breakout", "buy", "strong", "send", "pump", "good", "runner", "running", "squeeze", "accumulate", "up"}
    negative = {"rug", "scam", "dump", "bearish", "sell", "bad", "dead", "fake", "exit", "warning", "avoid", "honeypot", "drain"}

    pos = neg = 0
    for value in texts:
        words = set(value.split())
        pos += len(words & positive)
        neg += len(words & negative)

    sent_words = pos + neg
    sentiment = 50.0 if sent_words == 0 else 50 + 50 * ((pos - neg) / sent_words)

    engagements = sum(int(x.get("engagement", 0)) for x in recent_15)
    engagement_velocity = engagements / 15.0
    reach_proxy = sum(int(x.get("followers", 0)) for x in recent_15)

    author_scores = []
    for item in recent_15:
        quality = _log10_score(int(item.get("followers", 0)))
        if item.get("verified"):
            quality += 15
        author_scores.append(min(100, quality))

    author_quality = sum(author_scores) / len(author_scores) if author_scores else None

    coordination_risk = min(
        100,
        domination * 100 * 0.55
        + duplicate_ratio * 100 * 0.65
        + (20 if len(recent_15) >= 10 and unique_authors <= 3 else 0)
    )

    top_authors = []
    for username, count in sorted(author_counts.items(), key=lambda x: x[1], reverse=True)[:6]:
        rows = [x for x in recent_15 if (x.get("username") or "unknown") == username]
        top_authors.append({
            "username": username,
            "posts": count,
            "followers": max([int(x.get("followers", 0)) for x in rows], default=0),
            "verified": any(x.get("verified") for x in rows),
        })

    return {
        "state": "READY",
        "available": True,
        "post_count": len(items),
        "recent_15m": len(recent_15),
        "recent_5m": len(recent_5),
        "mention_velocity": round(velocity, 3),
        "velocity_acceleration": round(acceleration, 2),
        "sentiment": round(max(0, min(100, sentiment)), 1),
        "domination": round(domination, 3),
        "coordination_risk": round(coordination_risk, 1),
        "duplicates": duplicates,
        "duplicate_ratio": round(duplicate_ratio, 3),
        "unique_author_count": unique_authors,
        "author_quality": round(author_quality, 1) if author_quality is not None else None,
        "engagement_velocity": round(engagement_velocity, 2),
        "reach_proxy": reach_proxy,
        "top_authors": top_authors,
    }


def x_radar_candidates(items, limit=20):
    buckets = {}

    for item in items or []:
        for ca in item.get("cas") or []:
            bucket = buckets.setdefault(ca, [])
            bucket.append(item)

    rows = []

    for ca, mentions in buckets.items():
        sm = social_metrics(mentions)

        velocity = float(sm.get("mention_velocity") or 0)
        acceleration = float(sm.get("velocity_acceleration") or 0)
        sentiment = float(sm.get("sentiment") or 50)
        authors = int(sm.get("unique_author_count") or 0)
        engagement = float(sm.get("engagement_velocity") or 0)
        author_quality = float(sm.get("author_quality") or 0)
        coordination = float(sm.get("coordination_risk") or 0)

        # Discovery score favors speed + independent authors + engagement,
        # while explicitly penalizing likely coordinated/copied promotion.
        score = (
            min(35, velocity * 7)
            + min(20, max(0, acceleration - 1) * 4)
            + min(15, authors * 2.5)
            + min(15, __import__("math").log10(max(1, engagement + 1)) * 5)
            + author_quality * 0.15
            + max(0, sentiment - 50) * 0.10
            - coordination * 0.25
        )

        top = (sm.get("top_authors") or [{}])[0]

        best = max(
            mentions,
            key=lambda x: (
                int(x.get("engagement") or 0),
                int(x.get("followers") or 0),
            ),
        )

        rows.append({
            "mint": ca,
            "score": round(max(0, min(100, score)), 1),
            "posts_15m": sm.get("recent_15m", 0),
            "posts_5m": sm.get("recent_5m", 0),
            "mention_velocity": sm.get("mention_velocity"),
            "acceleration": sm.get("velocity_acceleration"),
            "unique_authors": authors,
            "sentiment": sm.get("sentiment"),
            "engagement_velocity": sm.get("engagement_velocity"),
            "author_quality": sm.get("author_quality"),
            "coordination_risk": sm.get("coordination_risk"),
            "top_author": top.get("username"),
            "ticker_hints": sorted({
                ticker
                for mention in mentions
                for ticker in (mention.get("tickers") or [])
            })[:5],
            "best_post": {
                "text": best.get("text"),
                "username": best.get("username"),
                "followers": best.get("followers"),
                "engagement": best.get("engagement"),
                "url": best.get("url"),
            },
        })

    rows.sort(
        key=lambda x: (
            x["score"],
            x["posts_5m"],
            x["unique_authors"],
            x["engagement_velocity"],
        ),
        reverse=True,
    )

    return rows[:limit]
