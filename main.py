from __future__ import annotations

import time
import asyncio
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from engine import Candle, evaluate_setup, risk_flags, opportunity_score
from discovery import discover_candidates
from history_store import save_candles

from adapters import (
    DexScreenerAdapter,
    GeckoTerminalAdapter,
    HeliusAdapter,
    XAdapter,
    x_items,
    social_metrics,
)
from storage import Store

load_dotenv()

app = FastAPI(
    title="Meme Intel",
    version="1.1"
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


class XSearchReq(BaseModel):
    query: str
    max_results: int = 25


class PaperOpenReq(BaseModel):
    mint: str
    side: str = "LONG"
    entry: float
    qty: float
    note: str = ""


class PaperCloseReq(BaseModel):
    trade_id: int
    exit: float


def parse_candles(data):
    items = (
        (data or {})
        .get("data", {})
        .get("attributes", {})
        .get("ohlcv_list", [])
    )

    candles = []

    for x in items:
        if len(x) < 6:
            continue

        try:
            candles.append(
                Candle(
                    ts=int(x[0]),
                    o=float(x[1]),
                    h=float(x[2]),
                    l=float(x[3]),
                    c=float(x[4]),
                    v=float(x[5] or 0),
                )
            )
        except Exception:
            continue

    candles.sort(key=lambda c: c.ts)

    return candles


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
                "state": (
                    "CONFIGURED"
                    if xa.source.configured
                    else "NOT_CONFIGURED"
                ),
            },
            "DexScreener": {
                "configured": True,
                "state": "READY",
            },
            "GeckoTerminal": {
                "configured": True,
                "state": "READY",
            },
            "Helius": {
                "configured": he.source.configured,
                "state": (
                    "READY"
                    if he.source.configured
                    else "NOT_CONFIGURED"
                ),
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
    if not req.mint.strip():
        raise HTTPException(
            400,
            "Mint required"
        )

    overview_task = ds.overview(req.mint)
    creation_task = ds.creation(req.mint)
    security_task = ds.security(req.mint)

    asset_task = he.asset(req.mint)


    candles1_task = gt.candles(
        req.mint,
        "1m"
    )

    x_task = xa.recent(
        req.x_query,
        50
    )

    (
        (overview, overview_err),
        (creation, creation_err),
        (security, security_err),
        (asset, asset_err),
        (d1, e1),
        (xp, xerr),
    ) = await asyncio.gather(
        overview_task,
        creation_task,
        security_task,
        asset_task,
        candles1_task,
        x_task,
    )

    raw1 = parse_candles(d1)


    c1 = closed_candles(
        raw1,
        60
    )
    save_candles(req.mint, c1)
    c5 = aggregate_5m_from_1m(c1)

    setup = (
        evaluate_setup(c5, c1)
        if c5 and c1
        else None
    )

    data = (
        overview.get("data", {})
        if isinstance(overview, dict)
        else {}
    )

    social = (
        x_items(xp)
        if not xerr
        else []
    )

    sm = social_metrics(social)

    liquidity = data.get("liquidity")
    market_cap = data.get("marketCap")

    risk = risk_flags(
        liquidity_usd=liquidity,
        market_cap=market_cap,
        holder_concentration=None,
        mint_authority=None,
        freeze_authority=None,
        social_domination=sm.get(
            "domination"
        ),
    )

    # Security provider is currently unavailable,
    # so don't call the token "low risk" merely because
    # no security problems were returned.
    if security_err:
        risk["flags"].append({
            "level": "UNKNOWN",
            "code": "SECURITY_DATA_UNAVAILABLE",
            "reason": (
                "Holder and authority security data "
                "is unavailable."
            ),
        })

        priority = {
            "LOW": 0,
            "UNKNOWN": 1,
            "MEDIUM": 2,
            "HIGH": 3,
            "CRITICAL": 4,
        }

        risk["overall"] = max(
            [f["level"] for f in risk["flags"]],
            key=lambda x: priority.get(x, 0),
            default="UNKNOWN",
        )

    technical = (
        75
        if setup
        and setup.state == "BREAKOUT_CONFIRMED"
        else 50
        if setup
        and setup.state == "HIGHER_LOW"
        else 20
    )

    social_velocity = min(
        100,
        (sm.get("mention_velocity") or 0) * 10,
    )

    liquidity_score = (
        0
        if liquidity is None
        else min(
            100,
            max(0, float(liquidity) / 500)
        )
    )

    # Missing X is treated as no corroboration,
    # not as positive evidence.
    corroboration = (
        60
        if not xerr
        and (xp or {}).get("data")
        else 0
    )

    sentiment = (
        sm.get("sentiment")
        if sm.get("sentiment") is not None
        else 0
    )

    penalty = {
        "LOW": 0,
        "UNKNOWN": 15,
        "MEDIUM": 20,
        "HIGH": 40,
        "CRITICAL": 80,
    }.get(
        risk["overall"],
        15
    )

    core_data_complete = not any([
        overview_err,
        e1,
        xerr,
        security_err,
    ])

    score = opportunity_score(
        technical=technical,
        social_velocity=social_velocity,
        sentiment=sentiment,
        liquidity=liquidity_score,
        corroboration=corroboration,
        risk_penalty=penalty,
        data_complete=core_data_complete,
    )

    return {
        "mint": req.mint,
        "state": (
            "READY"
            if any([
                overview,
                c5,
                c1,
                social,
                asset,
            ])
            else "DATA NOT AVAILABLE"
        ),

        "setup": (
            setup.dict()
            if setup
            else {
                "state": "DATA NOT AVAILABLE"
            }
        ),

        "overview": (
            data
            if data
            else "DATA NOT AVAILABLE"
        ),

        "security": (
            "DATA NOT AVAILABLE"
            if security_err
            else security
        ),

        "creation": (
            creation.get("data")
            if isinstance(creation, dict)
            else "DATA NOT AVAILABLE"
        ),

        "asset": (
            asset
            if asset
            else "DATA NOT AVAILABLE"
        ),

        "social": sm,

        "risk": risk,

        "score": score,

        "sources": {
            "DexScreener": (
                overview_err or "READY"
            ),
            "GeckoTerminal": {
                "5m": "DERIVED_FROM_1M",
                "1m": e1 or "READY",
            },
            "Helius": asset_err or "READY",
            "X": xerr or "READY",
            "Security": security_err or "READY",
        },
    }


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
    return store.close_trade(
        req.trade_id,
        req.exit
    )


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
            "state": "NOT_AVAILABLE"
        },
        "rule": (
            "Missing provider data is never "
            "invented."
        ),
    }
