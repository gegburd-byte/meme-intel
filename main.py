from __future__ import annotations

import time
import asyncio
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
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
    """Normalize Pump.fun frontend OHLC candles into the app's Candle shape."""
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("candles") or payload.get("results") or payload.get("data") or []
        if isinstance(items, dict):
            items = items.get("candles") or items.get("data") or items.get("results") or []
    else:
        items = []

    candles = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            ts = item.get("timestamp", item.get("time", item.get("ts")))
            if ts is None:
                continue
            ts = int(float(ts))
            if ts > 10_000_000_000:
                ts //= 1000

            o = item.get("open", item.get("o"))
            h = item.get("high", item.get("h"))
            l = item.get("low", item.get("l"))
            c = item.get("close", item.get("c"))
            v = item.get("volume", item.get("v", 0))
            if any(x is None for x in (o, h, l, c)):
                continue

            candles.append(Candle(
                ts=ts,
                o=float(o),
                h=float(h),
                l=float(l),
                c=float(c),
                v=float(v or 0),
            ))
        except (TypeError, ValueError):
            continue

    candles.sort(key=lambda x: x.ts)
    deduped = {}
    for candle in candles:
        deduped[candle.ts] = candle
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
        },
        "timestamp": int(time.time()),
    }


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


@app.get("/api/chart")
async def chart(mint: str, limit: int = 1000, offset: int = 0, timeframe: int = 1):
    """Fast Pump.fun OHLC page. The browser can page this endpoint in parallel."""
    mint = (mint or "").strip()
    if len(mint) < 32 or len(mint) > 44:
        raise HTTPException(400, "Invalid mint")

    allowed = {1, 5, 15, 60}
    timeframe = int(timeframe or 1)
    if timeframe not in allowed:
        raise HTTPException(400, "Unsupported timeframe")

    limit = max(25, min(int(limit or 1000), 1000))
    offset = max(0, int(offset or 0))

    payload, err = await pf.candles(
        mint,
        limit=limit,
        timeframe=timeframe,
        offset=offset,
    )
    candles = parse_pump_candles(payload)

    return {
        "state": "READY" if candles else "NO_CANDLES",
        "source": "PUMP.FUN" if candles else "NONE",
        "offset": offset,
        "limit": limit,
        "timeframe": timeframe,
        "has_more": len(candles) >= limit,
        "candles": [
            {
                "ts": c.ts,
                "o": c.o,
                "h": c.h,
                "l": c.l,
                "c": c.c,
                "v": c.v,
            }
            for c in candles
        ],
        "error": None if candles else (err or "NO_CANDLES"),
        "timestamp": int(time.time()),
    }


@app.get("/api/live/price")
async def live_price(mint: str):
    mint = (mint or "").strip()
    if len(mint) < 32 or len(mint) > 44:
        raise HTTPException(400, "Invalid mint")

    now = time.time()
    cached = PRICE_CACHE.get(mint)
    if cached and now - cached["time"] < 4:
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
