import asyncio
import os
import re
import time
from typing import Any

import httpx

DEXSCREENER = "https://api.dexscreener.com"
GECKO = "https://api.geckoterminal.com/api/v2"
X_API = "https://api.x.com/2"
HELIUS_RPC = "https://mainnet.helius-rpc.com"

CA_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


class Source:
    def __init__(self, name, configured, reason=""):
        self.name = name
        self.configured = configured
        self.reason = reason


class DexScreenerAdapter:
    def __init__(self):
        self.source = Source("DexScreener", True)

    async def _get(self, path, params=None):
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(DEXSCREENER + path, params=params)
            if r.status_code >= 400:
                return None, f"HTTP_{r.status_code}"
            return r.json(), None

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

        pools.sort(key=reserve, reverse=True)

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


class HeliusAdapter:
    def __init__(self, key=None):
        self.key = key or os.getenv("HELIUS_API_KEY")
        self.source = Source(
            "Helius",
            bool(self.key),
            "HELIUS_API_KEY missing" if not self.key else ""
        )

    async def asset(self, mint):
        if not self.key:
            return None, "NOT_CONFIGURED"

        url = f"{HELIUS_RPC}/?api-key={self.key}"

        body = {
            "jsonrpc": "2.0",
            "id": "meme-intel",
            "method": "getAsset",
            "params": {
                "id": mint,
                "displayOptions": {
                    "showFungible": True
                }
            }
        }

        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, json=body)

            if r.status_code >= 400:
                return None, f"HTTP_{r.status_code}"

            j = r.json()

            return j.get("result"), None


class XAdapter:
    def __init__(self, token=None):
        self.token = token or os.getenv("X_BEARER_TOKEN")
        self.source = Source(
            "X",
            bool(self.token),
            "X_BEARER_TOKEN missing" if not self.token else ""
        )

    async def recent(self, query, max_results=50):
        if not self.token:
            return None, "NOT_CONFIGURED"

        params = {
            "query": query,
            "max_results": min(max(10, int(max_results)), 100),
            "tweet.fields": "created_at,public_metrics,author_id,lang",
            "expansions": "author_id",
            "user.fields": "name,username,profile_image_url"
        }

        headers = {
            "Authorization": f"Bearer {self.token}"
        }

        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"{X_API}/tweets/search/recent",
                params=params,
                headers=headers
            )

            if r.status_code >= 400:
                return None, f"HTTP_{r.status_code}:{r.text[:250]}"

            return r.json(), None


def _num(value):
    try:
        if value is None:
            return None

        return float(value)

    except Exception:
        return None


def extract_cas(text: str):
    return CA_RE.findall(text or "")


def x_items(payload):
    if not payload:
        return []

    users = {
        u["id"]: u
        for u in payload.get("includes", {}).get("users", [])
    }

    items = []

    for t in payload.get("data", []):
        u = users.get(t.get("author_id"), {})
        pm = t.get("public_metrics", {}) or {}

        items.append({
            "id": t["id"],
            "text": t.get("text", ""),
            "created_at": t.get("created_at"),
            "username": u.get("username", ""),
            "name": u.get("name", ""),
            "likes": pm.get("like_count", 0),
            "reposts": pm.get("retweet_count", 0),
            "replies": pm.get("reply_count", 0),
            "quotes": pm.get("quote_count", 0),
            "cas": extract_cas(t.get("text", "")),
            "url": (
                f"https://x.com/"
                f"{u.get('username', 'i')}/status/{t['id']}"
            )
        })

    return items


def social_metrics(items):
    if not items:
        return {
            "post_count": 0,
            "mention_velocity": None,
            "sentiment": None,
            "domination": None,
            "duplicates": None
        }

    now = time.time()

    recent = [
        x for x in items
        if x.get("created_at")
        and (
            now
            - time.mktime(
                __import__("datetime")
                .datetime.fromisoformat(
                    x["created_at"].replace("Z", "+00:00")
                ).timetuple()
            )
        ) <= 900
    ]

    counts = {}

    for x in recent:
        counts[x["username"]] = counts.get(
            x["username"], 0
        ) + 1

    top = max(counts.values(), default=0)

    domination = top / max(1, len(recent))

    texts = [
        re.sub(r"\W+", " ", x["text"].lower()).strip()
        for x in recent
    ]

    dup = 0

    if texts:
        dup = len(texts) - len(set(texts))

    pos_words = {
        "bullish",
        "moon",
        "breakout",
        "buy",
        "strong",
        "send",
        "pump",
        "good"
    }

    neg_words = {
        "rug",
        "scam",
        "dump",
        "bearish",
        "sell",
        "bad",
        "dead"
    }

    pos = 0
    neg = 0

    for t in texts:
        w = set(t.split())
        pos += len(w & pos_words)
        neg += len(w & neg_words)

    total = pos + neg

    sentiment = (
        50
        if total == 0
        else 50 + 50 * (pos - neg) / total
    )

    return {
        "post_count": len(items),
        "mention_velocity": len(recent) / 15,
        "sentiment": round(
            max(0, min(100, sentiment)), 1
        ),
        "domination": round(domination, 3),
        "duplicates": dup
    }
