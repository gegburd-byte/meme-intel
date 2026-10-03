from __future__ import annotations

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
    public_rpc_endpoints,
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
    "/api/chart/current": (20, 10.0),
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
    """Normalize several Pump.fun/market OHLC response shapes into Candle."""
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        data = payload.get("data") or payload.get("result") or payload
        if isinstance(data, dict):
            attributes = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
            items = (
                data.get("candles")
                or data.get("results")
                or data.get("ohlcv_list")
                or data.get("items")
                or attributes.get("candles")
                or attributes.get("ohlcv_list")
                or []
            )
        else:
            items = data
    else:
        items = []

    candles = []

    for item in items or []:
        try:
            if isinstance(item, dict):
                ts = item.get("timestamp", item.get("time", item.get("ts")))
                o = item.get("open", item.get("o"))
                h = item.get("high", item.get("h"))
                l = item.get("low", item.get("l"))
                close = item.get("close", item.get("c"))
                volume = item.get("volume", item.get("v", 0))
            elif isinstance(item, (list, tuple)) and len(item) >= 5:
                # Common array order: time, open, high, low, close, volume.
                ts = item[0]
                o, h, l, close = item[1], item[2], item[3], item[4]
                volume = item[5] if len(item) > 5 else 0
            else:
                continue

            if ts is None or any(x is None for x in (o, h, l, close)):
                continue

            o = float(o)
            h = float(h)
            l = float(l)
            close = float(close)

            if not all(map(lambda x: x == x and abs(x) != float("inf"), (o, h, l, close))):
                continue
            if l <= 0 or min(o, close) < l or max(o, close) > h or h < l:
                continue

            ts = int(float(ts))
            if ts > 10_000_000_000:
                ts //= 1000

            candles.append(Candle(
                ts=ts,
                o=o,
                h=h,
                l=l,
                c=close,
                v=float(volume or 0),
            ))
        except (TypeError, ValueError, IndexError):
            continue

    candles.sort(key=lambda x: x.ts)
    deduped = {c.ts: c for c in candles}
    return list(sorted(deduped.values(), key=lambda x: x.ts))

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

    overview, overview_err = await ds.overview(mint)
    data = overview.get("data", {}) if isinstance(overview, dict) else {}
    x_query = build_token_x_query(mint, data.get("symbol"), data.get("name"), req.x_query)

    security_task = he.security(mint)
    candles_task = gt.candles(mint, "1m")
    rugcheck_task = rc.report(mint)
    x_task = xa.recent(x_query, 60) if req.include_x else skipped_x()

    (
        (security, security_err),
        (d1, e1),
        (rugcheck, rugcheck_err),
        (xp, xerr),
    ) = await asyncio.gather(
        security_task,
        candles_task,
        rugcheck_task,
        x_task,
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
        save_candles(mint, c1)
    c5 = aggregate_5m_from_1m(c1)
    setup = evaluate_setup(c5, c1) if c5 and c1 else None

    social_items = x_items(xp) if not xerr else []
    sm = social_metrics(social_items)
    if xerr and xerr != "X_SKIPPED_FOR_MARKET_SCREEN":
        sm["state"] = xerr
        sm["available"] = False
        sm["error"] = xerr
    elif xerr == "X_SKIPPED_FOR_MARKET_SCREEN":
        sm["state"] = "SKIPPED"
        sm["available"] = False
    market = market_metrics(c1, c5, data)

    sec_gate = security_gate(security)

    safety_profile = build_safety_profile(
        security=security,
        rugcheck=rugcheck,
        overview=data,
        sell_probe=sell_probe,
    )

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
        vertical_move_pct=abs(market.get("return_30m_pct")) if market.get("return_30m_pct") is not None else None,
    )

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

    score = opportunity_score(
        technical=technical,
        social_velocity=social_velocity,
        sentiment=sentiment,
        liquidity=liquidity_score,
        corroboration=corroboration,
        risk_penalty=penalty,
        data_complete=not any([overview_err, e1, xerr]),
    )

    decision = decision_engine(
        setup=setup,
        market=market,
        social=sm,
        risk=risk,
        overview=data,
    )

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

    if price_span == 0 and moving_bars == 0:
        return 0.0

    return min(
        100.0,
        len(ordered)
        + min(50.0, price_span * 100.0)
        + min(20.0, moving_bars),
    )


@app.get("/api/chart")
async def chart(mint: str, limit: int = 1000, offset: int = 0, timeframe: int = 1):
    """Fast exact Pump.fun candles with fallback only on native-feed failure."""
    mint = (mint or "").strip()
    if len(mint) < 32 or len(mint) > 44:
        raise HTTPException(400, "Invalid mint")

    if timeframe not in {1, 5, 15, 60}:
        raise HTTPException(400, "Unsupported timeframe")

    limit = max(15, min(int(limit or 1000), 1000))
    offset = max(0, int(offset or 0))

    # Primary path: ask Pump.fun for the exact requested timeframe and return
    # immediately. Do not wait on fallback providers when the native feed works.
    try:
        native_payload, native_err = await asyncio.wait_for(
            pf.candles(
                mint,
                limit=limit,
                timeframe=timeframe,
                offset=offset,
            ),
            timeout=4.0,
        )
    except asyncio.TimeoutError:
        native_payload, native_err = None, "PUMPFUN_CHART_TIMEOUT"
    except Exception as exc:
        native_payload, native_err = None, str(exc)[:240]

    native = parse_pump_candles(native_payload)
    native_quality = chart_data_quality(native, minimum_bars=3)

    if native_quality > 0:
        candles = sorted(native, key=lambda x: x.ts)[-limit:]
        return {
            "state": "READY",
            "source": "PUMP.FUN",
            "offset": offset,
            "limit": limit,
            "timeframe": timeframe,
            "has_more": len(native) >= limit,
            "candles": [
                {"ts": c.ts, "o": c.o, "h": c.h, "l": c.l, "c": c.c, "v": c.v}
                for c in candles
            ],
            "error": None,
            "diagnostics": {
                "exact_native": True,
                "primary": "PUMP.FUN",
                "sources": {
                    "PUMP.FUN": {
                        "bars": len(candles),
                        "quality": round(native_quality, 2),
                    }
                },
            },
            "timestamp": int(time.time()),
        }

    # Never splice provider pages into a native Pump.fun chart. Only page 0
    # has a safe on-chain fallback; older-page pagination remains native-only.
    if offset > 0:
        return {
            "state": "NO_CANDLES",
            "source": "NONE",
            "offset": offset,
            "limit": limit,
            "timeframe": timeframe,
            "has_more": False,
            "candles": [],
            "error": native_err or "PUMPFUN_HISTORY_UNAVAILABLE",
            "diagnostics": {
                "exact_native": False,
                "primary": "PUMP.FUN",
                "sources": {
                    "PUMP.FUN": {
                        "bars": len(native),
                        "quality": round(native_quality, 2),
                    }
                },
            },
            "timestamp": int(time.time()),
        }

    async def load_onchain():
        try:
            return await asyncio.wait_for(
                he.historical_trade_candles(
                    mint,
                    timeframe=timeframe,
                    lookback_minutes=120,
                    max_signatures=240,
                ),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            return [], "ONCHAIN_HISTORY_TIMEOUT"
        except Exception as exc:
            return [], str(exc)[:240]

    async def load_gecko():
        try:
            return await asyncio.wait_for(
                gt.candles(mint, "1m"),
                timeout=6.0,
            )
        except asyncio.TimeoutError:
            return None, "GECKO_HISTORY_TIMEOUT"
        except Exception as exc:
            return None, str(exc)[:240]

    # Native failed: fallback providers run together so this path remains fast.
    (onchain_candles, onchain_err), (gecko_payload, gecko_err) = await asyncio.gather(
        load_onchain(),
        load_gecko(),
    )

    public_errors = []
    onchain_source = "HELIUS_ONCHAIN_TRADES"

    if chart_data_quality(onchain_candles or [], minimum_bars=3) <= 0:
        async def load_public_history(public_rpc):
            try:
                result = await asyncio.wait_for(
                    he.historical_trade_candles(
                        mint,
                        timeframe=timeframe,
                        lookback_minutes=120,
                        max_signatures=180,
                        rpc_base=public_rpc,
                    ),
                    timeout=4.5,
                )
                return public_rpc, result
            except asyncio.TimeoutError:
                return public_rpc, ([], "PUBLIC_RPC_HISTORY_TIMEOUT")
            except Exception as exc:
                return public_rpc, ([], str(exc)[:240])

        public_results = await asyncio.gather(
            *(load_public_history(rpc) for rpc in public_rpc_endpoints())
        )

        for public_rpc, result in public_results:
            public_candles, public_err = result
            if chart_data_quality(public_candles or [], minimum_bars=3) > 0:
                onchain_candles = public_candles
                onchain_err = None
                onchain_source = "SOLANA_PUBLIC_RPC"
                break
            public_errors.append(f"{public_rpc}:{public_err}")

    gecko_base = parse_candles(gecko_payload)
    gecko_candles = aggregate_timeframe_candles(
        gecko_base,
        timeframe,
    )

    options = [
        (onchain_source, onchain_candles or []),
        ("GECKOTERMINAL", gecko_candles or []),
    ]
    scored = [
        (name, rows, chart_data_quality(rows, minimum_bars=3))
        for name, rows in options
    ]
    valid = [row for row in scored if row[2] > 0]

    if valid:
        best_source, best_candles, best_quality = max(
            valid,
            key=lambda row: (row[2], len(row[1])),
        )
        candles = sorted(best_candles, key=lambda x: x.ts)[-limit:]
        source = best_source
    else:
        candles = []
        source = "NONE"

    return {
        "state": "READY" if candles else "NO_CANDLES",
        "source": source,
        "offset": 0,
        "limit": limit,
        "timeframe": timeframe,
        "has_more": False,
        "candles": [
            {"ts": c.ts, "o": c.o, "h": c.h, "l": c.l, "c": c.c, "v": c.v}
            for c in candles
        ],
        "error": None if candles else (
            "; ".join(public_errors)
            or onchain_err
            or gecko_err
            or native_err
            or "NO_VALID_CANDLES"
        ),
        "diagnostics": {
            "exact_native": False,
            "primary": source if candles else "PUMP.FUN",
            "sources": {
                "PUMP.FUN": {
                    "bars": len(native),
                    "quality": round(native_quality, 2),
                },
                **{
                    name: {
                        "bars": len(rows),
                        "quality": round(quality, 2),
                    }
                    for name, rows, quality in scored
                },
            },
            "helius_error": onchain_err,
            "public_rpc_errors": public_errors,
            "gecko_error": gecko_err,
            "pump_error": native_err,
        },
        "timestamp": int(time.time()),
    }


@app.get("/api/chart/current")
async def chart_current(mint: str, timeframe: int = 1):
    """Fast current-bar endpoint used by the realtime chart loop.

    This endpoint deliberately hits only Pump.fun's native candle feed. It
    never launches Helius/Gecko/public-RPC history reconstruction, keeping the
    one-second realtime path cheap and preserving the exact Pump.fun candle.
    """
    mint = (mint or "").strip()
    if len(mint) < 32 or len(mint) > 44:
        raise HTTPException(400, "Invalid mint")

    if timeframe not in {1, 5, 15, 60}:
        raise HTTPException(400, "Unsupported timeframe")

    try:
        payload, err = await asyncio.wait_for(
            pf.candles(
                mint,
                limit=5,
                timeframe=timeframe,
                offset=0,
            ),
            timeout=2.5,
        )
    except asyncio.TimeoutError:
        return {
            "state": "TIMEOUT",
            "source": "PUMP.FUN",
            "timeframe": timeframe,
            "candles": [],
            "error": "PUMPFUN_CURRENT_TIMEOUT",
            "timestamp": int(time.time()),
        }
    except Exception as exc:
        return {
            "state": "ERROR",
            "source": "PUMP.FUN",
            "timeframe": timeframe,
            "candles": [],
            "error": str(exc)[:240],
            "timestamp": int(time.time()),
        }

    native = parse_pump_candles(payload)
    if not native:
        return {
            "state": "NO_CANDLES",
            "source": "PUMP.FUN",
            "timeframe": timeframe,
            "candles": [],
            "error": err or "NO_CURRENT_CANDLE",
            "timestamp": int(time.time()),
        }

    current = max(native, key=lambda x: x.ts)

    return {
        "state": "READY",
        "source": "PUMP.FUN",
        "timeframe": timeframe,
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
