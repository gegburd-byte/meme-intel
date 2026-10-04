from __future__ import annotations

import os
import time
import asyncio
import re
from pathlib import Path
from collections import defaultdict, deque

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from engine import (
    Candle, evaluate_setup, risk_flags, opportunity_score,
    market_metrics, decision_engine,
)
from discovery import discover_candidates
from history_store import save_candles

from adapters import (
    DexScreenerAdapter,
    GeckoTerminalAdapter,
    HeliusAdapter,
    PumpFunAdapter,
    RugCheckAdapter,
    JupiterAdapter,
    XAdapter,
    x_items,
    social_metrics,
    x_radar_candidates,
)
from storage import Store
from live_stream import trade_hub
from risk_model import build_safety_profile

load_dotenv()

app = FastAPI(
    title="Meme Intel",
    version="2.0"
)

RATE_LIMIT_RULES = {
    "/api/chart": (30, 10.0),
    "/api/chart/history": (12, 10.0),
    "/api/chart/meta": (10, 10.0),
    "/api/chart/current": (60, 10.0),
    "/api/live/price": (20, 10.0),
    "/api/analyze": (4, 30.0),
}
_rate_state = defaultdict(deque)

@app.middleware("http")
async def api_rate_limit(request, call_next):
    rule = RATE_LIMIT_RULES.get(request.url.path)
    client = request.client
    if rule and client:
        limit, window = rule
        now = time.monotonic()
        key = (client.host, request.url.path)
        bucket = _rate_state[key]
        while bucket and now - bucket[0] >= window:
            bucket.popleft()
        if len(bucket) >= limit:
            return JSONResponse(
                status_code=429,
                content={
                    "state": "RATE_LIMITED",
                    "detail": "Too many requests. Please slow down.",
                },
                headers={"Retry-After": str(max(1, int(window)))} ,
            )
        bucket.append(now)
    return await call_next(request)

app.mount(
    "/static",
    StaticFiles(
        directory=Path(__file__).with_name("static")
    ),
    name="static",
)

store = Store()

ds = DexScreenerAdapter()
gt = GeckoTerminalAdapter()
he = HeliusAdapter()
pf = PumpFunAdapter()
rc = RugCheckAdapter()
ju = JupiterAdapter()
xa = XAdapter()


class AnalyzeReq(BaseModel):
    mint: str = Field(
        min_length=32,
        max_length=44
    )

    x_query: str = (
        '(solana OR "pump.fun" OR memecoin OR $SOL) '
        'lang:en -is:retweet'
    )
    include_x: bool = False


class XSearchReq(BaseModel):
    query: str
    max_results: int = 25


class PaperOpenReq(BaseModel):
    mint: str
    side: str = "LONG"
    entry: float = Field(gt=0)
    qty: float = Field(gt=0)
    note: str = ""


class PaperCloseReq(BaseModel):
    trade_id: int
    exit: float


def security_gate(security):
    if (
        not isinstance(security, dict)
        or security.get("state") != "READY"
        or not security.get("sampled_accounts")
        or not security.get("supply")
        or float(security.get("coverage_ratio") or 0) < 0.70
    ):
        return {
            "state": "UNKNOWN",
            "label": "SECURITY UNKNOWN",
            "score": None,
            "reasons": ["On-chain holder/authority coverage is incomplete or below the minimum coverage threshold."],
        }

    reasons = []
    score = 0

    coverage = float(security.get("coverage_ratio") or 0)
    if coverage < 0.95:
        reasons.append(
            f"Holder coverage is partial ({coverage * 100:.0f}% of reported supply sampled)."
        )

    if security.get("mint_authority"):
        score += 35
        reasons.append("Mint authority appears active.")
    if security.get("freeze_authority"):
        score += 25
        reasons.append("Freeze authority appears active.")

    top = security.get("top_holder_share")
    top10 = security.get("top10_holder_share")

    if top is not None and top > 0.50:
        score += 35
        reasons.append("Top holder controls more than 50% of sampled supply.")
    elif top is not None and top > 0.25:
        score += 18
        reasons.append("Top holder concentration is elevated.")

    if top10 is not None and top10 > 0.70:
        score += 25
        reasons.append("Top 10 holders control more than 70% of sampled supply.")
    elif top10 is not None and top10 > 0.50:
        score += 12
        reasons.append("Top 10 holder concentration is elevated.")

    score = min(100, score)

    if score >= 65:
        label = "BLOCK"
    elif score >= 30 or coverage < 0.95:
        label = "WARN"
    else:
        label = "PASS"

    if not reasons:
        reasons.append("No major detectable authority or concentration warning in the sampled data.")

    return {
        "state": "READY",
        "label": label,
        "score": score,
        "reasons": reasons,
    }


def build_token_x_query(mint, symbol, name, base_query):
    terms = ['"' + mint + '"']

    if symbol:
        clean = re.sub(r"[^A-Za-z0-9_]", "", str(symbol))[:18]
        if clean:
            terms.append('"' + clean.upper() + '"')

    if name:
        clean_name = re.sub(r"[\r\n\\]", " ", str(name)).strip()[:55]
        if len(clean_name) >= 3:
            terms.append('"' + clean_name + '"')

    token_part = "(" + " OR ".join(terms) + ")"
    base = (base_query or "").strip()[:420]
    return token_part + (" (" + base + ")" if base else "") + " -is:retweet"

def parse_candles(data):
    items = (
        (data or {})
        .get("data", {})
        .get("attributes", {})
        .get("ohlcv_list", [])
    )

    candles = []
    for item in items:
        if len(item) < 6:
            continue
        try:
            candles.append(
                Candle(
                    ts=int(item[0]),
                    o=float(item[1]),
                    h=float(item[2]),
                    l=float(item[3]),
                    c=float(item[4]),
                    v=float(item[5] or 0),
                )
            )
        except (TypeError, ValueError, IndexError):
            continue

    candles.sort(key=lambda item: item.ts)
    return candles


async def skipped_x():
    return None, "X_SKIPPED_FOR_MARKET_SCREEN"



def parse_pump_candles(payload):
    """Normalize Pump.fun OHLC payloads across current/legacy response shapes."""
    def unwrap(value):
        if isinstance(value, dict):
            for key in ("candles", "candlesticks", "ohlcv_list", "ohlcv", "bars", "rows", "items", "results"):
                rows = value.get(key)
                if isinstance(rows, list) and rows:
                    return rows

            for key in ("data", "result"):
                nested = value.get(key)
                if isinstance(nested, (dict, list)):
                    rows = unwrap(nested)
                    if rows:
                        return rows

            attrs = value.get("attributes")
            if isinstance(attrs, dict):
                rows = unwrap(attrs)
                if rows:
                    return rows

        return value if isinstance(value, list) else []

    items = unwrap(payload)
    candles = []

    for item in items or []:
        try:
            if isinstance(item, dict):
                ts = (
                    item.get("timestamp")
                    if item.get("timestamp") is not None
                    else item.get("time")
                )
                if ts is None:
                    ts = item.get("ts") or item.get("t") or item.get("startTime")

                o = item.get("open")
                if o is None:
                    o = item.get("o")
                h = item.get("high")
                if h is None:
                    h = item.get("h")
                low = item.get("low")
                if low is None:
                    low = item.get("l")
                close = item.get("close")
                if close is None:
                    close = item.get("c")
                volume = item.get("volume")
                if volume is None:
                    volume = item.get("v", 0)

            elif isinstance(item, (list, tuple)) and len(item) >= 5:
                # Common order: time, open, high, low, close, volume.
                ts, o, h, low, close = item[:5]
                volume = item[5] if len(item) > 5 else 0
            else:
                continue

            if ts is None or any(x is None for x in (o, h, low, close)):
                continue

            ts = int(float(ts))
            if ts > 10_000_000_000:
                ts //= 1000

            o = float(o)
            h = float(h)
            low = float(low)
            close = float(close)
            volume = float(volume or 0)

            if not all(
                x == x and abs(x) != float("inf")
                for x in (o, h, low, close)
            ):
                continue

            if o <= 0 or h <= 0 or low <= 0 or close <= 0:
                continue

            if low > min(o, close) or h < max(o, close) or h < low:
                continue

            if ts < 1_500_000_000:
                continue

            candles.append(
                Candle(
                    ts=ts,
                    o=o,
                    h=h,
                    l=low,
                    c=close,
                    v=max(0.0, volume),
                )
            )
        except (TypeError, ValueError, IndexError):
            continue

    deduped = {c.ts: c for c in candles}
    return list(sorted(deduped.values(), key=lambda x: x.ts))

def parse_pump_trades(payload):
    """Normalize Pump.fun trade-history rows into real price/time/volume trades."""
    def unwrap_trade_payload(value):
        if isinstance(value, list):
            return value

        if isinstance(value, dict):
            for key in ("data", "trades", "results", "items", "rows"):
                nested = value.get(key)

                if isinstance(nested, list) and nested:
                    return nested

                if isinstance(nested, dict):
                    rows = unwrap_trade_payload(nested)
                    if rows:
                        return rows

        return []

    payload = unwrap_trade_payload(payload)

    if not payload:
        return []

    def first_value(item, *keys):
        for key in keys:
            value = item.get(key)
            if value is not None:
                return value, key
        return None, None

    def first_positive_value(item, *keys):
        for key in keys:
            value = item.get(key)
            if value is None:
                continue
            try:
                if float(value) > 0:
                    return value, key
            except (TypeError, ValueError):
                continue
        return None, None

    def sol_amount_ui(value, key):
        number = float(value)

        # Pump.fun canonical fields are integer lamports. Normalize them
        # explicitly instead of using magnitude heuristics that break on tiny trades.
        if key in {
            "sol_amount",
            "solAmount",
            "sol_amount_lamports",
            "quote_amount",
            "quoteAmount",
            "quote_amount_lamports",
            "quoteAmountLamports",
            "quote_amount_in",
            "quoteAmountIn",
            "quote_amount_out",
            "quoteAmountOut",
        }:
            return number / 1_000_000_000

        # Legacy/direct UI fields may already be SOL.
        if key in {"amount_sol", "amountSol", "sol_ui"}:
            return number

        # Generic sol fields can be either form depending on endpoint shape.
        if key == "sol" and number > 1_000_000:
            return number / 1_000_000_000

        return number

    def token_amount_ui(value, key):
        number = float(value)

        if key in {
            "token_amount",
            "tokenAmount",
            "base_amount",
            "baseAmount",
            "base_amount_in",
            "baseAmountIn",
            "base_amount_out",
            "baseAmountOut",
            "base_amount_raw",
            "baseAmountRaw",
            "token_amount_raw",
            "tokens",
        }:
            return number / 1_000_000

        if key in {"amount_token", "amountToken", "token_ui"}:
            return number

        return number

    trades = []

    for item in payload:
        if not isinstance(item, dict):
            continue

        try:
            timestamp, _ = first_value(
                item,
                "timestamp",
                "time",
                "created_timestamp",
                "createdTimestamp",
                "created_at",
                "createdTs",
                "created_ts",
                "createdAt",
                "created_at_ms",
                "block_time",
                "blockTime",
            )

            if timestamp is None:
                continue

            ts = int(float(timestamp))
            if ts > 10_000_000_000:
                ts //= 1000

            if ts < 1_500_000_000:
                continue

            # Executed quote/base amounts are the trade price authority.
            # This matters after migration: stale bonding-curve virtual
            # reserves can remain in API rows even though the token is trading
            # on PumpSwap.
            sol_raw, sol_key = first_positive_value(
                item,
                "sol_amount",
                "solAmount",
                "sol_amount_lamports",
                "quote_amount_in",
                "quoteAmountIn",
                "quote_amount_out",
                "quoteAmountOut",
                "quote_amount",
                "quoteAmount",
                "quote_amount_lamports",
                "sol",
                "amount_sol",
                "amountSol",
                "sol_ui",
                "quote_ui",
            )

            token_raw, token_key = first_positive_value(
                item,
                "token_amount",
                "tokenAmount",
                "base_amount_out",
                "baseAmountOut",
                "base_amount_in",
                "baseAmountIn",
                "base_amount",
                "baseAmount",
                "token_amount_raw",
                "tokens",
                "amount_token",
                "amountToken",
                "token_ui",
                "base_ui",
            )

            price = None

            if sol_raw is not None and token_raw is not None:
                sol_value = sol_amount_ui(sol_raw, sol_key)
                token_value = token_amount_ui(token_raw, token_key)

                if sol_value > 0 and token_value > 0:
                    price = sol_value / token_value

            # Legacy bonding-curve rows without executed quote/base fields.
            if price is None:
                virtual_sol, _ = first_positive_value(
                    item,
                    "virtual_sol_reserves",
                    "virtualSolReserves",
                )
                virtual_token, _ = first_positive_value(
                    item,
                    "virtual_token_reserves",
                    "virtualTokenReserves",
                )

                if virtual_sol is not None and virtual_token is not None:
                    vs = float(virtual_sol)
                    vt = float(virtual_token)

                    if vs > 0 and vt > 0:
                        price = (
                            (vs / 1_000_000_000) /
                            (vt / 1_000_000)
                        )

            if price is None:
                raw_price, _ = first_value(
                    item,
                    "price",
                    "price_sol",
                    "priceSol",
                    "tokenPrice",
                    "token_price",
                    "pricePerToken",
                    "price_per_token",
                )

                if raw_price is not None:
                    price = float(raw_price)

            if price is None or price <= 0:
                continue

            volume = 0.0
            if sol_raw is not None:
                volume = max(
                    0.0,
                    sol_amount_ui(sol_raw, sol_key),
                )

            trade_id, _ = first_value(
                item,
                "signature",
                "tx_signature",
                "txSignature",
                "txHash",
                "tx_hash",
                "transaction",
                "transactionHash",
                "id",
                "tradeId",
                "trade_id",
            )

            raw_side, _ = first_value(
                item,
                "is_buy",
                "isBuy",
                "side",
                "txType",
                "tx_type",
            )

            side = "SELL"
            if (
                raw_side is True
                or str(raw_side).lower() == "true"
                or str(raw_side).upper() == "BUY"
            ):
                side = "BUY"

            trades.append({
                "id": str(trade_id or ""),
                "ts": ts,
                "price": float(price),
                "volume": volume,
                "side": side,
            })
        except (TypeError, ValueError, OverflowError):
            continue

    trades.sort(key=lambda x: x["ts"])
    return trades

def aggregate_pump_trade_candles(payloads, timeframe=1, limit=120):
    raw = str(timeframe or "1").strip().lower()

    if raw in {"1s", "1sec", "1second"}:
        span = 1
    else:
        try:
            span = max(60, int(float(raw)) * 60)
        except (TypeError, ValueError):
            span = 60

    buckets = {}

    for payload in payloads or []:
        for trade in parse_pump_trades(payload):
            ts = int(trade["ts"])
            price = float(trade["price"])

            bucket = (ts // span) * span
            row = buckets.get(bucket)

            if row is None:
                buckets[bucket] = {
                    "ts": bucket,
                    "o": price,
                    "h": price,
                    "l": price,
                    "c": price,
                    "v": float(trade["volume"]),
                    "_first_ts": ts,
                    "_last_ts": ts,
                }
                continue

            row["h"] = max(row["h"], price)
            row["l"] = min(row["l"], price)
            row["v"] += float(trade["volume"])

            if ts < row["_first_ts"]:
                row["_first_ts"] = ts
                row["o"] = price

            if ts >= row["_last_ts"]:
                row["_last_ts"] = ts
                row["c"] = price

    rows = sorted(
        buckets.values(),
        key=lambda x: x["ts"],
    )[-int(limit or 120):]

    return [
        Candle(
            ts=int(row["ts"]),
            o=float(row["o"]),
            h=float(row["h"]),
            l=float(row["l"]),
            c=float(row["c"]),
            v=float(row["v"]),
        )
        for row in rows
    ]

def closed_candles(candles, seconds_per_candle):
    now = int(time.time())

    return [
        c for c in candles
        if c.ts + seconds_per_candle <= now
    ]


@app.get("/")
async def index():
    return FileResponse(
        Path(__file__).with_name("static")
        / "index.html"
    )


@app.get("/api/health")
async def health():
    return {
        "status": "ONLINE",
        "sources": {
            "X": {
                "configured": xa.source.configured,
                "state": xa.last_state if xa.source.configured else "NOT_CONFIGURED",
                "detail": xa.last_error[:180] if xa.last_error else "",
            },
            "DexScreener": {
                "configured": True,
                "state": "READY",
            },
            "GeckoTerminal": {
                "configured": True,
                "state": "READY",
            },
            "Security": {
                "configured": he.source.configured,
                "state": "READY" if he.source.configured else "NOT_CONFIGURED",
            },
            "Helius": {
                "configured": he.source.configured,
                "state": (
                    "READY"
                    if he.source.configured
                    else "NOT_CONFIGURED"
                ),
            },            "LiveTrade": {
                "configured": trade_hub.active(),
                "state": trade_hub.state,
                "detail": trade_hub.last_error,
            },            "RugCheck": {
                "configured": True,
                "state": "READY",
            },
        },
        "server_time": int(time.time()),
    }


@app.post("/api/x/search")
async def x_search(req: XSearchReq):
    payload, err = await xa.recent(
        req.query,
        req.max_results
    )

    if err:
        return {
            "state": err,
            "items": []
        }

    items = x_items(payload)

    for i in items:
        i["ca_count"] = len(i["cas"])

    return {
        "state": "READY",
        "items": items,
        "social": social_metrics(items),
    }


@app.post("/api/x/radar")
async def x_radar(req: XSearchReq):
    payload, err = await xa.recent(req.query, req.max_results)
    if err:
        return {
            "state": err,
            "candidates": [],
        }

    items = x_items(payload)
    return {
        "state": "READY",
        "meta": (payload or {}).get("meta") or {},
        "candidates": x_radar_candidates(items, 20),
    }


def aggregate_timeframe_candles(candles: list[Candle], minutes: int) -> list[Candle]:
    """Aggregate available lower-timeframe candles without requiring every minute."""
    if minutes <= 1:
        return sorted(candles, key=lambda x: x.ts)

    span = minutes * 60
    buckets: dict[int, list[Candle]] = {}

    for candle in sorted(candles, key=lambda x: x.ts):
        bucket = (candle.ts // span) * span
        buckets.setdefault(bucket, []).append(candle)

    out: list[Candle] = []
    for ts, group in sorted(buckets.items()):
        group.sort(key=lambda x: x.ts)
        if not group:
            continue
        out.append(
            Candle(
                ts=ts,
                o=group[0].o,
                h=max(x.h for x in group),
                l=min(x.l for x in group),
                c=group[-1].c,
                v=sum(max(0.0, x.v) for x in group),
            )
        )
    return out


def aggregate_5m_from_1m(candles: list[Candle]) -> list[Candle]:
    buckets = {}
    for c in sorted(candles, key=lambda x: x.ts):
        bucket = (c.ts // 300) * 300
        buckets.setdefault(bucket, []).append(c)

    out = []
    for ts, group in sorted(buckets.items()):
        unique = {c.ts: c for c in group}
        group = sorted(unique.values(), key=lambda x: x.ts)

        if len(group) != 5:
            continue

        if any(group[i].ts != ts + i * 60 for i in range(5)):
            continue

        out.append(
            Candle(
                ts=ts,
                o=group[0].o,
                h=max(c.h for c in group),
                l=min(c.l for c in group),
                c=group[-1].c,
                v=sum(c.v for c in group),
            )
        )

    return out


@app.post("/api/analyze")
async def analyze(req: AnalyzeReq):
    mint = req.mint.strip()
    if not mint:
        raise HTTPException(400, "Mint required")

    try:
        overview, overview_err = await ds.overview(mint)
    except Exception as exc:
        overview, overview_err = None, f"OVERVIEW_ERROR:{str(exc)[:220]}"
    data = overview.get("data", {}) if isinstance(overview, dict) else {}
    x_query = build_token_x_query(mint, data.get("symbol"), data.get("name"), req.x_query)

    security_task = he.security(mint)
    candles_task = gt.candles(mint, "1m")
    rugcheck_task = rc.report(mint)
    x_task = xa.recent(x_query, 60) if req.include_x else skipped_x()

    provider_results = await asyncio.gather(
        security_task,
        candles_task,
        rugcheck_task,
        x_task,
        return_exceptions=True,
    )

    def unpack_provider(result, default_value, label):
        if isinstance(result, Exception):
            return default_value, f"{label}_ERROR:{str(result)[:220]}"
        if (
            isinstance(result, tuple) and
            len(result) == 2
        ):
            return result
        return default_value, f"{label}_MALFORMED_RESPONSE"

    (security, security_err) = unpack_provider(
        provider_results[0],
        (None, "NO_SECURITY_DATA"),
        "SECURITY",
    )
    (d1, e1) = unpack_provider(
        provider_results[1],
        (None, "NO_MARKET_HISTORY"),
        "GECKO",
    )
    (rugcheck, rugcheck_err) = unpack_provider(
        provider_results[2],
        (None, "NO_RUGCHECK_DATA"),
        "RUGCHECK",
    )
    (xp, xerr) = unpack_provider(
        provider_results[3],
        (None, "X_UNAVAILABLE"),
        "X",
    )

    asset = (
        security.get("asset")
        if isinstance(security, dict)
        else None
    )

    token_info = (asset or {}).get("token_info") or {}
    token_decimals = token_info.get("decimals")
    token_supply = token_info.get("supply")

    try:
        sell_probe, sell_probe_err = await ju.sell_probe(
            mint,
            decimals=token_decimals,
            supply=token_supply,
        )
    except Exception as exc:
        sell_probe, sell_probe_err = None, str(exc)[:300]
    asset_err = security_err
    creation = {
        "data": {
            "pairCreatedAt": data.get("pairCreatedAt"),
            "pairAddress": data.get("pairAddress"),
            "dexId": data.get("dexId"),
        }
    }
    creation_err = overview_err

    raw1 = parse_candles(d1)
    c1 = closed_candles(raw1, 60)
    if c1:
        try:
            save_candles(mint, c1)
        except Exception:
            # Persistent history is useful but must never break live analysis.
            pass
    c5 = aggregate_5m_from_1m(c1)
    setup = evaluate_setup(c5, c1) if c5 and c1 else None

    try:
        social_items = x_items(xp) if not xerr else []
        sm = social_metrics(social_items)
    except Exception as exc:
        social_items = []
        sm = {
            "state": "UNAVAILABLE",
            "available": False,
            "error": f"SOCIAL_MODEL_ERROR:{str(exc)[:220]}",
        }
    if xerr and xerr != "X_SKIPPED_FOR_MARKET_SCREEN":
        sm["state"] = xerr
        sm["available"] = False
        sm["error"] = xerr
    elif xerr == "X_SKIPPED_FOR_MARKET_SCREEN":
        sm["state"] = "SKIPPED"
        sm["available"] = False
    try:
        market = market_metrics(c1, c5, data)
    except Exception as exc:
        market = {
            "state": "NO_DATA",
            "profile": {
                "poc": None,
                "vah": None,
                "val": None,
                "value_area_pct": 0.70,
                "total_volume": 0.0,
                "bins": [],
            },
            "error": f"MARKET_MODEL_ERROR:{str(exc)[:220]}",
        }

    try:
        sec_gate = security_gate(security)
    except Exception as exc:
        sec_gate = {
            "state": "UNKNOWN",
            "label": "SECURITY UNKNOWN",
            "score": None,
            "reasons": [f"Security model unavailable: {str(exc)[:180]}"],
        }

    try:
        safety_profile = build_safety_profile(
            security=security,
            rugcheck=rugcheck,
            overview=data,
            sell_probe=sell_probe,
        )
    except Exception as exc:
        safety_profile = {
            "status": "UNKNOWN",
            "safety_percent": None,
            "rug_risk_percent": None,
            "confidence_percent": 0,
            "checks": [],
            "error": f"SAFETY_MODEL_ERROR:{str(exc)[:220]}",
        }

    try:
        risk = risk_flags(
            liquidity_usd=data.get("liquidity"),
            market_cap=data.get("marketCap"),
            holder_concentration=(
                security.get("top_holder_share")
                if isinstance(security, dict)
                else None
            ),
            mint_authority=(
                security.get("mint_authority")
                if isinstance(security, dict)
                else None
            ),
            freeze_authority=(
                security.get("freeze_authority")
                if isinstance(security, dict)
                else None
            ),
            social_domination=sm.get("domination"),
            coordination_risk=sm.get("coordination_risk"),
            vertical_move_pct=abs(market.get("return_30m_pct"))
            if market.get("return_30m_pct") is not None
            else None,
        )
    except Exception as exc:
        risk = {
            "overall": "UNKNOWN",
            "flags": [{
                "level": "UNKNOWN",
                "code": "RISK_MODEL_ERROR",
                "reason": f"Risk model unavailable: {str(exc)[:220]}",
            }],
        }


    if security_err:
        risk["flags"].append({
            "level": "UNKNOWN",
            "code": "SECURITY_DATA_UNAVAILABLE",
            "reason": "Holder and authority security data is unavailable.",
        })
        priority = {"LOW": 0, "UNKNOWN": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
        risk["overall"] = max(
            [f["level"] for f in risk["flags"]],
            key=lambda x: priority.get(x, 0),
            default="UNKNOWN",
        )

    technical = (
        90 if setup and setup.state == "BREAKOUT_CONFIRMED"
        else 72 if setup and setup.state == "HIGHER_LOW"
        else 55 if setup and setup.state == "PULLBACK"
        else 20
    )
    social_velocity = min(100, (sm.get("mention_velocity") or 0) * 18)
    liquidity_score = 0 if data.get("liquidity") is None else min(100, max(0, float(data["liquidity"]) / 500))
    corroboration = (
        min(100, 40 + (25 if social_items else 0) + (15 if (sm.get("unique_author_count") or 0) >= 3 else 0) + (20 if (sm.get("author_quality") or 0) >= 40 else 0))
        if not xerr else 0
    )
    sentiment = sm.get("sentiment") if sm.get("sentiment") is not None else 0
    penalty = {"LOW": 0, "UNKNOWN": 15, "MEDIUM": 20, "HIGH": 40, "CRITICAL": 80}.get(risk["overall"], 15)

    try:
        score = opportunity_score(
            technical=technical,
            social_velocity=social_velocity,
            sentiment=sentiment,
            liquidity=liquidity_score,
            corroboration=corroboration,
            risk_penalty=penalty,
            data_complete=not any([overview_err, e1, xerr]),
        )
    except Exception as exc:
        score = {
            "score": 0.0,
            "complete": False,
            "components": {},
            "note": f"Opportunity score unavailable: {str(exc)[:180]}",
        }

    try:
        decision = decision_engine(
        setup=setup,
        market=market,
        social=sm,
        risk=risk,
        overview=data,
    )
    except Exception as exc:
        decision = {
            "action": "NO TRADE",
            "exit_action": "HOLD / MONITOR",
            "confidence": "LOW",
            "score": 0,
            "entry_style": "WAIT",
            "entry_trigger": None,
            "invalidation": None,
            "target1": None,
            "target2": None,
            "reason": f"Decision model unavailable: {str(exc)[:220]}",
            "confirmation_count": 0,
            "confirmation_total": 0,
            "components": {},
            "disclaimer": "Rule-based research and paper-trading signal; it cannot know the future or guarantee an entry or exit.",
        }



    return {
        "mint": mint,
        "state": "READY" if any([overview, c5, c1, social_items, asset]) else "DATA NOT AVAILABLE",
        "x_query_used": x_query,
        "setup": setup.dict() if setup else {"state": "DATA NOT AVAILABLE"},
        "overview": data if data else "DATA NOT AVAILABLE",
        "security": "DATA NOT AVAILABLE" if security_err else security,
        "rugcheck": "DATA NOT AVAILABLE" if rugcheck_err else rugcheck,
        "sell_probe": "DATA NOT AVAILABLE" if sell_probe_err else sell_probe,
        "safety_profile": safety_profile,
        "data_quality": {
            "market": "READY" if not any([overview_err, e1]) else "LIMITED",
            "security": sec_gate.get("state", "UNKNOWN"),
            "x": sm.get("state", "NO_POSTS"),
            "mode": "FULL" if sm.get("state") == "READY" else "MARKET_ONLY",
        },
        "security_gate": sec_gate,
        "creation": creation.get("data") if isinstance(creation, dict) else "DATA NOT AVAILABLE",
        "asset": asset if asset else "DATA NOT AVAILABLE",
        "market": market,
        "candles": [
            {
                "ts": c.ts,
                "o": c.o,
                "h": c.h,
                "l": c.l,
                "c": c.c,
                "v": c.v,
            }
            for c in c1[-180:]
        ],
        "social": sm,
        "social_items": social_items,
        "risk": risk,
        "score": score,
        "decision": decision,
        "sources": {
            "DexScreener": overview_err or "READY",
            "GeckoTerminal": {"1m": e1 or "READY"},
            "Helius": asset_err or "READY",
            "X": xerr or "READY",
            "Security": security_err or "READY",
            "RugCheck": rugcheck_err or "READY",
            "JupiterSellProbe": sell_probe_err or "READY",
        },
        "timestamp": int(time.time()),
    }




@app.exception_handler(Exception)
async def api_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={
            "state": "ERROR",
            "detail": "Backend request failed.",
            "error": str(exc)[:300],
        },
    )


PRICE_CACHE = {}



_live_pool_tasks: dict[str, asyncio.Task[Any]] = {}
_live_rest_tasks: dict[str, asyncio.Task[Any]] = {}


async def live_rest_trade_loop(mint: str) -> None:
    """Independent exact-venue trade recovery that feeds the same live chart."""
    seen: deque[str] = deque(maxlen=1200)
    seen_set: set[str] = set()
    initialized = False
    last_legacy_poll = 0.0

    try:
        while mint in trade_hub.clients:
            now = time.time()
            rows = []

            # Run the fast Pump.fun/PumpSwap lane and the slower authenticated
            # legacy lane concurrently so a slow fallback can never delay the
            # primary live path.
            swap_task = asyncio.create_task(
                pf.swap_trades(
                    mint,
                    limit=100,
                    cursor=0,
                    fresh=True,
                )
            )

            legacy_due = (
                os.getenv("PUMP_FUN_JWT")
                and now - last_legacy_poll >= 1.0
            )
            legacy_task = (
                asyncio.create_task(
                    pf.trades(
                        mint,
                        limit=40,
                        offset=0,
                        minimum_size=0,
                        fresh=True,
                    )
                )
                if legacy_due
                else None
            )

            tasks = [swap_task]
            if legacy_task:
                tasks.append(legacy_task)

            try:
                results = await asyncio.wait_for(
                    asyncio.gather(
                        *tasks,
                        return_exceptions=True,
                    ),
                    timeout=0.65,
                )
            except asyncio.TimeoutError:
                results = [None] * len(tasks)
                for task in tasks:
                    if not task.done():
                        task.cancel()

            swap_result = results[0] if results else None
            if (
                isinstance(swap_result, tuple)
                and len(swap_result) == 2
            ):
                rows.extend(parse_pump_trades(swap_result[0]))

            if legacy_task:
                last_legacy_poll = now
                legacy_result = (
                    results[1]
                    if len(results) > 1
                    else None
                )
                if (
                    isinstance(legacy_result, tuple)
                    and len(legacy_result) == 2
                ):
                    rows.extend(parse_pump_trades(legacy_result[0]))

            # Deduplicate the two exact-venue HTTP sources. Stable transaction
            # IDs are preferred; the timestamp/price/side/volume tuple is only
            # a last-resort ID for providers that omit signatures.
            dedup = {}
            for index, row in enumerate(rows):
                try:
                    ts = int(row.get("ts") or 0)
                    price = float(row.get("price") or 0)
                    volume = float(row.get("volume") or 0)
                except (TypeError, ValueError):
                    continue

                if ts <= 0 or price <= 0:
                    continue

                stable_id = str(
                    row.get("id")
                    or (
                        row.get("signature")
                        + ":" if row.get("signature") else ""
                    )
                ).strip()

                if not stable_id:
                    stable_id = (
                        f"{ts}:{price:.18g}:{volume:.18g}:"
                        f"{str(row.get('side') or '')}:{index}"
                    )

                if stable_id in dedup:
                    continue

                dedup[stable_id] = {
                    "id": stable_id,
                    "signature": str(row.get("signature") or ""),
                    "source": (
                        "PUMPSWAP"
                        if str(row.get("source") or "").upper() == "PUMPSWAP"
                        else "PUMP.FUN"
                    ),
                    "side": str(row.get("side") or "BUY"),
                    "price": price,
                    "volume_sol": max(0.0, volume),
                    "timestamp": ts,
                }

            ordered = sorted(
                dedup.values(),
                key=lambda item: (item["timestamp"], item["id"]),
            )

            if not initialized:
                initialized = True
                for item in ordered:
                    if item["id"] not in seen_set:
                        seen.append(item["id"])
                        seen_set.add(item["id"])
                await asyncio.sleep(0.30)
                continue

            published_trade = False

            for item in ordered:
                trade_id = item["id"]
                if trade_id in seen_set:
                    continue

                seen.append(trade_id)
                seen_set.add(trade_id)
                if len(seen_set) > 1100:
                    while len(seen_set) > 900 and seen:
                        old_id = seen.popleft()
                        seen_set.discard(old_id)

                await trade_hub.publish_external_trade(mint, item)
                published_trade = True


            await asyncio.sleep(
                0.75
                if trade_hub.pumpfun_live
                else 0.20
            )

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        trade_hub.last_error = str(exc)[:300]
    finally:
        _live_rest_tasks.pop(mint, None)


async def attach_live_market_addresses(mint: str) -> None:
    """Attach every exact Pump.fun/PumpSwap market account to the live stream."""
    try:
        addresses: set[str] = set()
        pool_known = False

        try:
            coin, _ = await asyncio.wait_for(
                pf.coin(mint),
                timeout=0.8,
            )
            if isinstance(coin, dict):
                for key in (
                    "bonding_curve",
                    "bondingCurve",
                    "associated_bonding_curve",
                    "associatedBondingCurve",
                ):
                    value = str(coin.get(key) or "").strip()
                    if value:
                        addresses.add(value)

                for key in (
                    "pump_swap_pool",
                    "pumpSwapPool",
                    "pumpSwapPoolAddress",
                    "pool",
                    "poolAddress",
                    "amm",
                    "ammPool",
                ):
                    value = str(coin.get(key) or "").strip()
                    if value:
                        addresses.add(value)
                        pool_known = True
        except Exception:
            pass

        # A migrated coin can still expose its old bonding curve while the
        # PumpSwap pool field is temporarily absent. Discover the pool
        # independently instead of skipping the fallback just because the
        # bonding-curve address exists.
        if not pool_known:
            try:
                pairs_payload, _ = await asyncio.wait_for(
                    ds.pairs(mint),
                    timeout=0.8,
                )
                pairs = pairs_payload if isinstance(pairs_payload, list) else []
                pump_pairs = [
                    pair for pair in pairs
                    if isinstance(pair, dict)
                    and str(pair.get("dexId") or "").lower()
                        in {"pumpswap", "pump-swap", "pump_swap"}
                    and pair.get("pairAddress")
                ]

                if pump_pairs:
                    def liq(pair):
                        try:
                            return float(
                                (pair.get("liquidity") or {}).get("usd") or 0
                            )
                        except (TypeError, ValueError):
                            return 0.0

                    pool_address = max(
                        pump_pairs,
                        key=liq,
                    ).get("pairAddress")

                    if pool_address:
                        addresses.add(str(pool_address))
            except Exception:
                pass

        for address in addresses:
            await trade_hub.add_watch_address(
                mint,
                address,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        _live_pool_tasks.pop(mint, None)


@app.websocket("/ws/trades")
async def ws_trades(websocket: WebSocket):
    mint = (websocket.query_params.get("mint") or "").strip()
    if len(mint) < 32 or len(mint) > 44:
        await websocket.accept()
        await websocket.send_json({
            "type": "status",
            "state": "INVALID_MINT",
            "detail": "Invalid Solana mint.",
        })
        await websocket.close(code=1008)
        return

    await websocket.accept()
    await trade_hub.add_client(mint, websocket)

    if mint not in _live_rest_tasks or _live_rest_tasks[mint].done():
        _live_rest_tasks[mint] = asyncio.create_task(
            live_rest_trade_loop(mint)
        )

    # Start the pool lookup after the mint stream is already live. This keeps
    # first-trade latency low while adding PumpSwap coverage a moment later.
    if mint not in _live_pool_tasks or _live_pool_tasks[mint].done():
        _live_pool_tasks[mint] = asyncio.create_task(
            attach_live_market_addresses(mint)
        )

    try:
        await websocket.send_json({
            "type": "status",
            "state": trade_hub.state if trade_hub.active() else "UNAVAILABLE",
            "detail": trade_hub.last_error,
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await trade_hub.remove_client(mint, websocket)
        if mint not in trade_hub.clients:
            task = _live_pool_tasks.pop(mint, None)
            if task and not task.done():
                task.cancel()

            rest_task = _live_rest_tasks.pop(mint, None)
            if rest_task and not rest_task.done():
                rest_task.cancel()


def chart_data_quality(candles: list[Candle], minimum_bars: int = 3) -> float:
    """Score real OHLC data; flat/malformed payloads must not become the chart."""
    if not candles:
        return 0.0

    ordered = sorted(candles, key=lambda x: x.ts)
    times = [int(x.ts) for x in ordered]
    closes = [float(x.c) for x in ordered if float(x.c) > 0]
    ranges = [
        max(0.0, float(x.h) - float(x.l))
        for x in ordered
        if float(x.h) > 0 and float(x.l) > 0
    ]

    if len(ordered) < minimum_bars or len(set(times)) < minimum_bars:
        return 0.0
    if len(closes) < minimum_bars:
        return 0.0

    # Reject an apparently successful payload that is just the same price
    # repeated across many timestamps with no wick/body movement.
    price_min = min(closes)
    price_max = max(closes)
    price_span = (price_max - price_min) / max(price_min, 1e-30)

    moving_bars = sum(
        1 for c in ordered
        if abs(float(c.c) - float(c.o)) > 0
        or float(c.h) > float(c.o)
        or float(c.l) < float(c.o)
    )

    if len(ordered) == 1 and minimum_bars == 1:
        # A brand-new token can legitimately have a flat first candle.
        # Native Pump.fun data is trusted on this path, so keep that real bar.
        return 1.0

    if price_span == 0 and moving_bars == 0:
        return 0.0

    return min(
        100.0,
        len(ordered)
        + min(50.0, price_span * 100.0)
        + min(20.0, moving_bars),
    )


@app.get("/api/chart/live-trades")
async def chart_live_trades(mint: str, limit: int = 200):
    """Return the newest trade stream data without changing chart price units."""
    mint = (mint or "").strip()

    if len(mint) < 32 or len(mint) > 44:
        raise HTTPException(400, "Invalid mint")

    limit = max(1, min(int(limit or 30), 200))
    now = time.time()

    cache = getattr(
        chart_live_trades,
        "_cache",
        {},
    )

    cached = cache.get(mint)
    if cached and now - cached["time"] < 0.05:
        return cached["payload"]

    # The server-side Helius stream is the lowest-latency source here. Keep
    # this endpoint cheap: the browser polls it frequently, so never make a
    # network request to another provider when decoded live trades already
    # exist in memory.
    rows = trade_hub.recent_trade_snapshot(mint, limit=limit)

    # Exact-venue HTTP recovery lane. The current Pump.fun swap API is fast
    # enough to sample only when the live in-memory lane has stopped advancing;
    # it covers both bonding-curve and migrated PumpSwap activity.
    latest_server_trade = max(
        (
            int(row.get("timestamp") or 0)
            for row in rows
            if isinstance(row, dict)
        ),
        default=0,
    )
    now_sec = int(time.time())
    live_lane_stale = (
        latest_server_trade <= 0
        or now_sec - latest_server_trade >= 2
    )

    if live_lane_stale:
        try:
            swap_payload, swap_err = await asyncio.wait_for(
                pf.swap_trades(
                    mint,
                    limit=min(25, limit),
                    cursor=0,
                    fresh=False,
                ),
                timeout=0.45,
            )
            swap_rows = parse_pump_trades(swap_payload)

            if swap_rows:
                rows = [
                    {
                        "id": f"pump-swap-http:{mint}:{int(row['ts'])}:{i}",
                        "signature": "",
                        "source": "PUMPSWAP",
                        "side": row.get("side", "BUY"),
                        "price": float(row["price"]),
                        "volume_sol": float(row["volume"]),
                        "timestamp": int(row["ts"]),
                    }
                    for i, row in enumerate(swap_rows[-limit:])
                ]
        except Exception:
            pass

    # Use Pump.fun's authenticated trade endpoint only when the in-memory
    # on-chain lane is empty or has gone stale. This gives us an independent
    # exact-venue recovery path without adding a network request to every hot
    # poll while the live stream is healthy.
    latest_server_trade = max(
        (
            int(row.get("timestamp") or 0)
            for row in rows
            if isinstance(row, dict)
        ),
        default=0,
    )
    now_sec = int(time.time())
    live_lane_stale = (
        latest_server_trade <= 0
        or now_sec - latest_server_trade >= 2
    )

    if live_lane_stale and not rows and os.getenv("PUMP_FUN_JWT"):
        native_cache = getattr(
            chart_live_trades,
            "_native_cache",
            {},
        )
        native_cached = native_cache.get(mint)

        if (
            native_cached
            and now - native_cached["time"] < 0.5
        ):
            rows = native_cached["rows"][-limit:]
        else:
            try:
                native_payload, native_err = await asyncio.wait_for(
                    pf.trades(
                        mint,
                        limit=25,
                        offset=0,
                        minimum_size=0,
                        fresh=True,
                    ),
                    timeout=0.35,
                )

                native_rows = parse_pump_trades(native_payload)

                if native_rows:
                    rows = [
                    {
                        "id": f"pump-http:{mint}:{int(row['ts'])}:{i}",
                        "signature": "",
                        "source": "PUMP.FUN",
                        "side": row.get("side", "BUY"),
                        "price": float(row["price"]),
                        "volume_sol": float(row["volume"]),
                        "timestamp": int(row["ts"]),
                    }
                    for i, row in enumerate(native_rows[-limit:])
                    ]

                    native_cache[mint] = {
                        "time": now,
                        "rows": rows,
                    }
                    chart_live_trades._native_cache = native_cache
            except Exception:
                pass

    payload = {
        "state": "READY" if rows else (
            "UNAVAILABLE" if not trade_hub.active()
            else "WAITING_FOR_TRADES"
        ),
        "mint": mint,
        "trades": rows,
        "timestamp": int(time.time() * 1000),
    }

    cache[mint] = {
        "time": now,
        "payload": payload,
    }

    if len(cache) > 32:
        oldest = min(
            cache.items(),
            key=lambda item: item[1]["time"],
        )[0]
        cache.pop(oldest, None)

    return payload


@app.get("/api/chart")
async def chart(
    mint: str,
    limit: int = 1000,
    offset: int = 0,
    timeframe: int = 1,
):
    """Compatibility chart endpoint using only Pump.fun/Solana price units."""
    mint = (mint or "").strip()

    if len(mint) < 32 or len(mint) > 44:
        raise HTTPException(400, "Invalid mint")

    if timeframe not in {1,5,15,60}:
        raise HTTPException(400, "Unsupported timeframe")

    limit = max(
        15,
        min(int(limit or 1000),1000),
    )
    offset = max(
        0,
        int(offset or 0),
    )

    if offset == 0:
        payload = await chart_history(
            mint=mint,
            timeframe=timeframe,
            limit=min(
                120,
                limit,
            ),
        )

        rows = payload.get("candles") or []

        return {
            **payload,
            "offset":offset,
            "limit":limit,
            "timeframe":timeframe,
            "has_more":False,
        }

    try:
        payload, err = await asyncio.wait_for(
            pf.candles(
                mint,
                limit=limit,
                timeframe=timeframe,
                offset=offset,
                fresh=True,
            ),
            timeout=3.0,
        )

        rows = parse_pump_candles(
            payload
        )

        return {
            "state":"READY" if rows else "NO_CANDLES",
            "source":"PUMP.FUN" if rows else "NONE",
            "offset":offset,
            "limit":limit,
            "timeframe":timeframe,
            "has_more":len(rows) >= limit,
            "candles":[
                {
                    "ts":c.ts,
                    "o":c.o,
                    "h":c.h,
                    "l":c.l,
                    "c":c.c,
                    "v":c.v,
                }
                for c in sorted(
                    rows,
                    key=lambda x:x.ts,
                )[-limit:]
            ],
            "error":None if rows else (
                err or
                "PUMPFUN_HISTORY_UNAVAILABLE"
            ),
            "timestamp":int(time.time()),
        }
    except Exception as exc:
        return {
            "state":"ERROR",
            "source":"PUMP.FUN",
            "offset":offset,
            "limit":limit,
            "timeframe":timeframe,
            "has_more":False,
            "candles":[],
            "error":str(exc)[:240],
            "timestamp":int(time.time()),
        }


@app.get("/api/chart/meta")
async def chart_meta(mint: str):
    """Return Pump.fun's exact market-cap data and supply."""
    mint = (mint or "").strip()

    if len(mint) < 32 or len(mint) > 44:
        raise HTTPException(400, "Invalid mint")

    coin, coin_err = await pf.coin(mint)

    if isinstance(coin, dict):
        def num_value(*keys):
            for key in keys:
                value = coin.get(key)
                if value is not None:
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        pass
            return None

        supply_raw = num_value(
            "total_supply",
            "totalSupply",
        )

        market_cap_sol = num_value(
            # Pump.fun exposes market_cap in SOL on current responses. Some
            # older/legacy shapes reported atomic lamports, so defensively
            # normalize only implausibly-large values.
            "market_cap",
            "marketCap",
        )

        if (
            market_cap_sol is not None and
            market_cap_sol > 1_000_000
        ):
            market_cap_sol /= 1_000_000_000

        market_cap_usd = num_value(
            "usd_market_cap",
            "usdMarketCap",
        )

        virtual_sol = num_value(
            "virtual_sol_reserves",
            "virtualSolReserves",
        )

        virtual_token = num_value(
            "virtual_token_reserves",
            "virtualTokenReserves",
        )

        complete = bool(
            coin.get("complete")
            or coin.get("is_complete")
        )

        supply_ui = None

        if (
            supply_raw is not None and
            supply_raw > 0
        ):
            # Standard Pump.fun SPL amounts may be returned in atomic units.
            supply_ui = (
                supply_raw / 1_000_000
                if supply_raw > 1_000_000_000
                else supply_raw
            )

        # If the API doesn't return market_cap, derive it from Pump.fun's
        # own bonding-curve reserves and total supply.
        if (
            market_cap_sol is None and
            virtual_sol is not None and
            virtual_token is not None and
            virtual_sol > 0 and
            virtual_token > 0 and
            supply_ui and
            supply_ui > 0
        ):
            price_sol = (
                virtual_sol / 1_000_000_000
            ) / (
                virtual_token / 1_000_000
            )
            market_cap_sol = price_sol * supply_ui
        else:
            price_sol = None

        if (
            price_sol is None and
            market_cap_sol is not None and
            supply_ui and
            supply_ui > 0
        ):
            price_sol = market_cap_sol / supply_ui

        return {
            "state": "READY",
            "source": "PUMP.FUN",
            "mint": mint,
            "symbol": coin.get("symbol"),
            "name": coin.get("name"),
            "complete": complete,
            "market_cap_sol": market_cap_sol,
            "market_cap_usd": market_cap_usd,
            "total_supply": supply_raw,
            "total_supply_ui": supply_ui,
            "price_sol": price_sol,
            "virtual_sol_reserves": virtual_sol,
            "virtual_token_reserves": virtual_token,
            "pump_swap_pool": coin.get("pump_swap_pool"),
            "timestamp": int(time.time()),
        }

    # Fallback for migrated/legacy tokens when Pump.fun metadata is unavailable.
    try:
        overview, overview_err = await ds.overview(mint)
    except Exception as exc:
        overview, overview_err = None, str(exc)

    data = overview.get("data") if isinstance(overview, dict) else {}
    data = data if isinstance(data, dict) else {}

    price = data.get("price")
    market_cap = data.get("marketCap")

    return {
        "state": "FALLBACK" if market_cap is not None else "UNAVAILABLE",
        "source": "DEXSCREENER",
        "mint": mint,
        "symbol": data.get("symbol"),
        "name": data.get("name"),
        "complete": None,
        "market_cap_sol": None,
        "market_cap_usd": (
            float(market_cap)
            if market_cap is not None
            else None
        ),
        "total_supply": None,
        "total_supply_ui": None,
        "price_sol": (
            float(price)
            if price is not None
            else None
        ),
        "error": coin_err or overview_err,
        "timestamp": int(time.time()),
    }


def normalize_chart_interval(timeframe="1m", interval=None):
    raw = str(interval if interval is not None else timeframe).strip().lower()

    aliases = {
        "1s": ("1s", 1),
        "1sec": ("1s", 1),
        "1second": ("1s", 1),
        "1m": ("1m", 1),
        "1min": ("1m", 1),
        "5m": ("5m", 5),
        "5min": ("5m", 5),
        "15m": ("15m", 15),
        "15min": ("15m", 15),
        "1h": ("1h", 60),
        "60m": ("1h", 60),
    }

    if raw in aliases:
        return aliases[raw]

    try:
        minutes = int(float(raw))
    except (TypeError, ValueError):
        raise HTTPException(400, "Unsupported timeframe")

    mapping = {
        1: ("1m", 1),
        5: ("5m", 5),
        15: ("15m", 15),
        60: ("1h", 60),
    }

    if minutes not in mapping:
        raise HTTPException(400, "Unsupported timeframe")

    return mapping[minutes]


@app.get("/api/chart/history")
async def chart_history(
    mint: str,
    timeframe: str = "1m",
    limit: int = 120,
    interval: str | None = None,
):
    """Return real Pump.fun/PumpSwap OHLC history without cross-venue substitution."""
    mint = (mint or "").strip()

    if len(mint) < 32 or len(mint) > 44:
        raise HTTPException(400, "Invalid mint")

    chart_interval, timeframe_minutes = normalize_chart_interval(
        timeframe,
        interval,
    )

    limit = max(30, min(int(limit or 120), 120))

    cache_key = (mint, chart_interval, limit)
    cached = getattr(chart_history, "_cache", {}).get(cache_key)
    now = time.time()

    if cached and now - cached["time"] < 8.0 and cached["payload"].get("candles"):
        return cached["payload"]

    async def native_history():
        if chart_interval == "1s":
            return "PUMP.FUN", [], "PUMPFUN_1S_NATIVE_UNSUPPORTED"

        try:
            payload, err = await asyncio.wait_for(
                pf.candles(
                    mint,
                    limit=limit,
                    timeframe=timeframe_minutes,
                    offset=0,
                    fresh=True,
                ),
                timeout=1.5,
            )
            rows = parse_pump_candles(payload)
            if rows and chart_data_quality(rows, minimum_bars=1) > 0:
                return "PUMP.FUN", rows, err
            return "PUMP.FUN", [], err or "NO_NATIVE_CANDLES"
        except Exception as exc:
            return "PUMP.FUN", [], str(exc)[:240]

    async def pump_trade_history():
        try:
            page_count = 6 if chart_interval == "1s" else 3
            results = await asyncio.gather(
                *[
                    pf.trades(
                        mint,
                        limit=200,
                        offset=page * 200,
                        minimum_size=0,
                        fresh=True,
                    )
                    for page in range(page_count)
                ],
                return_exceptions=True,
            )

            payloads = [
                result[0]
                for result in results
                if (
                    isinstance(result, tuple) and
                    len(result) == 2 and
                    isinstance(result[0], list)
                )
            ]

            rows = aggregate_pump_trade_candles(
                payloads,
                timeframe=chart_interval,
                limit=limit,
            )

            return (
                "PUMP.FUN TRADE HISTORY",
                rows,
                None if rows else "NO_TRADES_DECODED",
            )
        except Exception as exc:
            return "PUMP.FUN TRADE HISTORY", [], str(exc)[:240]

    async def helius_history():
        try:
            pool_address = None

            try:
                coin, _ = await asyncio.wait_for(
                    pf.coin(mint),
                    timeout=1.25,
                )
                if isinstance(coin, dict):
                    pool_address = (
                        coin.get("pump_swap_pool")
                        or coin.get("pumpSwapPool")
                        or coin.get("pumpSwapPoolAddress")
                        or coin.get("pool")
                        or coin.get("poolAddress")
                        or coin.get("amm")
                        or coin.get("ammPool")
                    )
            except Exception:
                pool_address = None

            # Pump.fun's coin endpoint is not always available for migrated
            # tokens. DexScreener is used only to discover the PumpSwap pool
            # address here; candle prices still come exclusively from Pump.fun
            # trade events / on-chain PumpSwap trades.
            if not pool_address:
                try:
                    pairs_payload, _ = await asyncio.wait_for(
                        ds.pairs(mint),
                        timeout=1.25,
                    )
                    pairs = pairs_payload if isinstance(pairs_payload, list) else []
                    pump_pairs = [
                        pair for pair in pairs
                        if isinstance(pair, dict)
                        and str(pair.get("dexId") or "").lower()
                            in {"pumpswap", "pump-swap", "pump_swap"}
                        and pair.get("pairAddress")
                    ]

                    if pump_pairs:
                        def pump_liquidity(pair):
                            try:
                                return float(
                                    (pair.get("liquidity") or {}).get("usd") or 0
                                )
                            except (TypeError, ValueError):
                                return 0.0

                        pool_address = max(
                            pump_pairs,
                            key=pump_liquidity,
                        ).get("pairAddress")
                except Exception:
                    pool_address = None

            lookback_minutes = (
                max(10, int((limit + 59) // 60) + 2)
                if chart_interval == "1s"
                else max(120, limit * timeframe_minutes)
            )

            rows, err = await asyncio.wait_for(
                he.historical_trade_candles(
                    mint,
                    timeframe=chart_interval,
                    lookback_minutes=lookback_minutes,
                    # 1s history only needs a recent trade window. Keep the
                    # archival fallback bounded so it cannot spend seconds
                    # resolving a huge signature batch before the chart paints.
                    max_signatures=(400 if chart_interval == "1s" else 1500),
                    extra_addresses=[pool_address] if pool_address else None,
                ),
                timeout=(5.0 if chart_interval == "1s" else 7.0),
            )

            return "HELIUS_ONCHAIN_TRADES", rows or [], err
        except Exception as exc:
            return "HELIUS_ONCHAIN_TRADES", [], str(exc)[:240]

    tasks = [
        asyncio.create_task(pump_trade_history()),
        asyncio.create_task(helius_history()),
    ]

    if chart_interval != "1s":
        native_task = asyncio.create_task(native_history())
        tasks.insert(0, native_task)
    else:
        native_task = None

    if native_task is not None:
        native_source, native_rows, native_err = await native_task

        if native_rows:
            payload = {
                "state": "READY",
                "source": "PUMP.FUN",
                "candles": [
                    {
                        "ts": c.ts,
                        "o": c.o,
                        "h": c.h,
                        "l": c.l,
                        "c": c.c,
                        "v": c.v,
                    }
                    for c in sorted(native_rows, key=lambda x: x.ts)[-limit:]
                ],
                "error": None,
                "timestamp": int(time.time()),
            }

            for task in tasks:
                if task is not native_task and not task.done():
                    task.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

            chart_history._cache = getattr(chart_history, "_cache", {})
            chart_history._cache[cache_key] = {
                "time": time.time(),
                "payload": payload,
            }
            return payload

    fallback_errors = []

    for task in asyncio.as_completed(
        [t for t in tasks if t is not native_task]
    ):
        try:
            source, rows, err = await task
        except Exception as exc:
            fallback_errors.append(str(exc)[:180])
            continue

        if rows and chart_data_quality(rows, minimum_bars=1) > 0:
            payload = {
                "state": "READY",
                "source": source,
                "candles": [
                    {
                        "ts": c.ts,
                        "o": c.o,
                        "h": c.h,
                        "l": c.l,
                        "c": c.c,
                        "v": c.v,
                    }
                    for c in sorted(rows, key=lambda x: x.ts)[-limit:]
                ],
                "error": None,
                "timestamp": int(time.time()),
            }

            for other in tasks:
                if not other.done():
                    other.cancel()

            await asyncio.gather(*tasks, return_exceptions=True)

            chart_history._cache = getattr(chart_history, "_cache", {})
            chart_history._cache[cache_key] = {
                "time": time.time(),
                "payload": payload,
            }
            return payload

        fallback_errors.append(str(err or source)[:180])

    # Never cache a transient empty result. Upstream timeouts/auth failures and
    # very new tokens can resolve moments later; caching an empty payload here
    # used to make a broken chart stay broken for several seconds.
    return {
        "state": "NO_CANDLES",
        "source": "NONE",
        "candles": [],
        "error": "; ".join(
            [x for x in fallback_errors if x]
        )[:600] or "NO_PUMPFUN_HISTORY",
        "timestamp": int(time.time()),
    }

@app.get("/api/chart/current")
async def chart_current(
    mint: str,
    timeframe: str = "1m",
    interval: str | None = None,
):
    """Return the current real Pump.fun/PumpSwap bar for the requested interval."""
    mint = (mint or "").strip()
    if len(mint) < 32 or len(mint) > 44:
        raise HTTPException(400, "Invalid mint")

    chart_interval, timeframe_minutes = normalize_chart_interval(
        timeframe,
        interval,
    )

    # The decoded live trade candle wins whenever it is newer than the
    # HTTP OHLC endpoint. Pump.fun's native candle endpoint can lag the trade
    # stream, which previously allowed a stale candle to keep repainting over
    # the moving chart.
    live = trade_hub.current_candle(
        mint,
        timeframe=chart_interval,
        max_age_seconds=2,
    )
    if live:
        return {
            "state": "READY",
            "source": live.get("source", "PUMP.FUN LIVE TRADES"),
            "timeframe": chart_interval,
            "candles": [{
                "ts": int(live["ts"]),
                "o": float(live["o"]),
                "h": float(live["h"]),
                "l": float(live["l"]),
                "c": float(live["c"]),
                "v": float(live.get("v") or 0),
            }],
            "error": None,
            "timestamp": int(time.time()),
        }

    # When the in-memory stream has not received a trade yet,
    # ask Pump.fun's own swap API for the latest exact-venue trade. This is
    # only a fallback; the websocket/on-chain path remains the primary source.
    try:
        swap_payload, swap_err = await asyncio.wait_for(
            pf.swap_trades(
                mint,
                limit=5,
                cursor=0,
                fresh=False,
            ),
            timeout=0.45,
        )
        swap_rows = parse_pump_trades(swap_payload)

        if swap_rows:
            bucket_span = 1 if chart_interval == "1s" else max(
                60,
                int(timeframe_minutes) * 60,
            )
            latest = swap_rows[-1]
            bucket = (int(latest["ts"]) // bucket_span) * bucket_span
            bucket_rows = [
                row for row in swap_rows
                if (int(row["ts"]) // bucket_span) * bucket_span == bucket
            ]

            if bucket_rows:
                prices = [float(row["price"]) for row in bucket_rows]
                current = {
                    "ts": bucket,
                    "o": prices[0],
                    "h": max(prices),
                    "l": min(prices),
                    "c": prices[-1],
                    "v": sum(
                        max(0.0, float(row.get("volume") or 0))
                        for row in bucket_rows
                    ),
                }
            else:
                current = {
                    "ts": bucket,
                    "o": float(latest["price"]),
                    "h": float(latest["price"]),
                    "l": float(latest["price"]),
                    "c": float(latest["price"]),
                    "v": float(latest.get("volume") or 0),
                }
            return {
                "state": "READY",
                "source": "PUMP.FUN LIVE TRADES",
                "timeframe": chart_interval,
                "candles": [current],
                "error": None,
                "timestamp": int(time.time()),
            }
    except Exception:
        pass

    if chart_interval == "1s":
        return {
            "state": "NO_CANDLES",
            "source": "PUMP.FUN LIVE TRADES",
            "timeframe": "1s",
            "candles": [],
            "error": "NO_CURRENT_1S_TRADE",
            "timestamp": int(time.time()),
        }

    try:
        payload, err = await asyncio.wait_for(
            pf.candles(
                mint,
                limit=5,
                timeframe=timeframe_minutes,
                offset=0,
                fresh=True,
            ),
            timeout=0.65,
        )
    except asyncio.TimeoutError:
        payload, err = None, "PUMPFUN_CURRENT_TIMEOUT"
    except Exception as exc:
        payload, err = None, str(exc)[:240]

    native = parse_pump_candles(payload)

    if native:
        current = max(native, key=lambda x: x.ts)
        return {
            "state": "READY",
            "source": "PUMP.FUN",
            "timeframe": chart_interval,
            "candles": [{
                "ts": current.ts,
                "o": current.o,
                "h": current.h,
                "l": current.l,
                "c": current.c,
                "v": current.v,
            }],
            "error": None,
            "timestamp": int(time.time()),
        }

    live = trade_hub.current_candle(
        mint,
        timeframe=timeframe_minutes,
    )
    if live:
        return {
            "state": "READY",
            "source": live.get("source", "PUMP.FUN LIVE TRADES"),
            "timeframe": chart_interval,
            "candles": [{
                "ts": int(live["ts"]),
                "o": float(live["o"]),
                "h": float(live["h"]),
                "l": float(live["l"]),
                "c": float(live["c"]),
                "v": float(live.get("v") or 0),
            }],
            "error": err or "PUMPFUN_NATIVE_CANDLE_UNAVAILABLE",
            "timestamp": int(time.time()),
        }

    return {
        "state": "NO_CANDLES",
        "source": "PUMP.FUN",
        "timeframe": chart_interval,
        "candles": [],
        "error": err or "NO_CURRENT_CANDLE",
        "timestamp": int(time.time()),
    }

@app.get("/api/live/price")
async def live_price(mint: str):
    mint = (mint or "").strip()
    if len(mint) < 32 or len(mint) > 44:
        raise HTTPException(400, "Invalid mint")

    now = time.time()
    cached = PRICE_CACHE.get(mint)
    if cached and now - cached["time"] < 0.75:
        return cached["data"]

    price = None
    market_cap = None
    symbol = None
    name = None
    source = "UNAVAILABLE"

    asset, asset_err = await he.asset(mint)
    if isinstance(asset, dict):
        token_info = asset.get("token_info") or {}
        price_info = token_info.get("price_info") or {}
        try:
            price = float(price_info.get("price_per_token"))
        except (TypeError, ValueError):
            price = None

        try:
            supply = float(token_info.get("supply"))
            if price is not None and supply > 0:
                market_cap = price * supply
        except (TypeError, ValueError):
            pass

        symbol = token_info.get("symbol")
        name = (
            ((asset.get("content") or {}).get("metadata") or {}).get("name")
            or symbol
        )
        if price is not None:
            source = "HELIUS"

    if price is None:
        overview, overview_err = await ds.overview(mint)
        if isinstance(overview, dict):
            data = overview.get("data") or {}
            price = data.get("price")
            market_cap = data.get("marketCap")
            symbol = data.get("symbol")
            name = data.get("name")
            if price is not None:
                source = "DEXSCREENER"
        else:
            overview_err = "NO_DATA"

    payload = {
        "state": "READY" if price is not None else "NO_PRICE",
        "mint": mint,
        "price": price,
        "market_cap": market_cap,
        "supply": (
            float((asset.get("token_info") or {}).get("supply"))
            if isinstance(asset, dict)
            and (asset.get("token_info") or {}).get("supply") is not None
            else None
        ),
        "symbol": symbol,
        "name": name,
        "source": source,
        "timestamp": int(now),
        "error": asset_err if price is None else None,
    }

    PRICE_CACHE[mint] = {
        "time": now,
        "data": payload,
    }
    return payload


TOP_CACHE = {"time": 0, "data": None}
TOP_CACHE_SECONDS = 35


@app.get("/api/top")
async def top_opportunities():
    global TOP_CACHE

    now = time.time()
    if TOP_CACHE["data"] is not None and now - TOP_CACHE["time"] < TOP_CACHE_SECONDS:
        return TOP_CACHE["data"]

    candidates = await discover_candidates(
        limit=15,
        min_liquidity=10000,
        pump_only=True,
    )

    async def inspect(candidate, include_x=False):
        try:
            analysis = await analyze(
                AnalyzeReq(
                    mint=candidate["address"],
                    x_query="lang:en -is:retweet",
                    include_x=include_x,
                )
            )
            decision = analysis.get("decision") or {}
            risk = analysis.get("risk") or {}
            gate = analysis.get("security_gate") or {}
            overview = analysis.get("overview") or {}
            social = analysis.get("social") or {}

            eligible = (
                gate.get("state") == "READY"
                and gate.get("label") == "PASS"
                and risk.get("overall") not in {"HIGH", "CRITICAL"}
                and decision.get("action") != "NO TRADE"
            )

            rank = (
                float(decision.get("score") or 0)
                + (12 if eligible else 0)
                - (25 if gate.get("label") == "BLOCK" else 0)
                - (10 if gate.get("label") == "WARN" else 0)
            )

            return {
                "candidate": candidate,
                "mint": candidate["address"],
                "symbol": overview.get("symbol") if isinstance(overview, dict) else candidate.get("symbol"),
                "name": overview.get("name") if isinstance(overview, dict) else candidate.get("name"),
                "price": overview.get("price") if isinstance(overview, dict) else candidate.get("priceUsd"),
                "research_rank": round(max(0, min(100, rank)), 1),
                "eligible": eligible,
                "decision": decision,
                "risk": risk,
                "security_gate": gate,
                "market": analysis.get("market"),
                "social": social,
                "data_quality": analysis.get("data_quality"),
                "updated_at": analysis.get("timestamp"),
            }
        except Exception as exc:
            return {
                "mint": candidate.get("address"),
                "symbol": candidate.get("symbol"),
                "name": candidate.get("name"),
                "research_rank": 0,
                "eligible": False,
                "error": str(exc),
            }

    # Stage 1: cheap market + security screening.
    screened = await asyncio.gather(
        *[inspect(candidate, include_x=False) for candidate in candidates[:8]]
    )

    screened.sort(
        key=lambda item: (
            item.get("eligible", False),
            item.get("research_rank", 0),
        ),
        reverse=True,
    )

    # Stage 2: only enrich the strongest finalists with X.
    finalists = [item for item in screened if item.get("eligible")][:2]
    if xa.source.configured and finalists:
        enriched = await asyncio.gather(
            *[
                inspect(item["candidate"], include_x=True)
                for item in finalists
            ]
        )
        by_mint = {item.get("mint"): item for item in enriched}
        screened = [by_mint.get(item.get("mint"), item) for item in screened]

    screened.sort(
        key=lambda item: (
            item.get("eligible", False),
            item.get("research_rank", 0),
        ),
        reverse=True,
    )

    return_data = {
        "state": "READY",
        "updated_at": int(now),
        "candidates": screened[:8],
        "top": next((item for item in screened if item.get("eligible")), None),
        "scan_mode": (
            "MARKET_SECURITY_PLUS_X_FINALISTS"
            if xa.source.configured
            else "MARKET_SECURITY_X_UNAVAILABLE"
        ),
    }

    TOP_CACHE = {"time": now, "data": return_data}
    return return_data


@app.get("/api/discover")
async def discover():
    candidates = await discover_candidates(
        limit=15,
        min_liquidity=10000,
    )

    return {
        "state": "READY",
        "count": len(candidates),
        "generated_at": int(time.time()),
        "candidates": candidates,
    }


@app.get("/api/paper/trades")
async def paper_trades():
    return {
        "trades": store.trades()
    }


@app.post("/api/paper/open")
async def paper_open(req: PaperOpenReq):
    return {
        "trade_id": store.add_trade(
            req.mint,
            req.side,
            req.entry,
            req.qty,
            req.note,
        )
    }


@app.post("/api/paper/close")
async def paper_close(req: PaperCloseReq):
    if req.exit <= 0:
        raise HTTPException(400, "Exit price must be greater than zero.")
    try:
        return store.close_trade(
            req.trade_id,
            req.exit
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/sources")
async def sources():
    return {
        "DexScreener": {
            "state": "READY"
        },
        "GeckoTerminal": {
            "state": "READY"
        },
        "X": {
            "state": (
                "READY"
                if xa.source.configured
                else "NOT_CONFIGURED"
            )
        },
        "Helius": {
            "state": (
                "READY"
                if he.source.configured
                else "NOT_CONFIGURED"
            )
        },
        "Security": {
            "state": (
                "READY"
                if he.source.configured
                else "NOT_CONFIGURED"
            )
        },
        "rule": (
            "Missing provider data is never "
            "invented."
        ),
    }
