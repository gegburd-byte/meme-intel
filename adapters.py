from datetime import datetime, timezone
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

    async def _rpc(self, method, params):
        if not self.key:
            return None, "NOT_CONFIGURED"

        url = f"{HELIUS_RPC}/?api-key={self.key}"
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": "meme-intel",
                        "method": method,
                        "params": params,
                    },
                )
                if r.status_code >= 400:
                    return None, f"HTTP_{r.status_code}"
                payload = r.json()
                if payload.get("error"):
                    return None, str(payload["error"])
                return payload.get("result"), None
        except Exception as exc:
            return None, str(exc)

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

        asset_task = self.asset(mint)
        account_task = self._rpc(
            "getAccountInfo",
            [
                mint,
                {"encoding": "jsonParsed"},
            ],
        )
        holders_task = self._rpc(
            "getTokenAccounts",
            {
                "mint": mint,
                "page": 1,
                "limit": 1000,
                "displayOptions": {},
            },
        )

        asset, asset_err = await asset_task
        account, account_err = await account_task
        holders, holders_err = await holders_task

        if asset_err and account_err and holders_err:
            return None, (
                asset_err or account_err or holders_err
            )

        parsed = (
            (account or {})
            .get("value", {})
            .get("data", {})
            .get("parsed", {})
            .get("info", {})
        )

        mint_authority = parsed.get("mintAuthority")
        freeze_authority = parsed.get("freezeAuthority")

        token_accounts = (
            (holders or {}).get("token_accounts")
            or []
        )

        owner_balances = {}
        raw_total = 0

        for row in token_accounts:
            owner = row.get("owner")
            amount = row.get("amount")

            try:
                amount = int(amount or 0)
            except Exception:
                amount = 0

            if owner:
                owner_balances[owner] = (
                    owner_balances.get(owner, 0) + amount
                )

            raw_total += amount

        ranked = sorted(
            owner_balances.items(),
            key=lambda x: x[1],
            reverse=True,
        )

        supply = (
            ((asset or {}).get("token_info") or {}).get("supply")
        )

        try:
            supply = int(supply) if supply is not None else raw_total
        except Exception:
            supply = raw_total

        top_share = (
            ranked[0][1] / supply
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
            "mint_authority": bool(mint_authority),
            "freeze_authority": bool(freeze_authority),
            "holder_count": len(ranked),
            "top_holder_share": top_share,
            "top10_holder_share": top10_share,
            "sampled_accounts": len(token_accounts),
            "supply": supply,
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
                ]
                if warning
            ],
        }, None


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
