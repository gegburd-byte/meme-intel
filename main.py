from __future__ import annotations

import time
import asyncio
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
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
    XAdapter,
    x_items,
    social_metrics,
    x_radar_candidates,
)
from storage import Store

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


def security_gate(security):
    if not isinstance(security, dict) or security.get("state") != "READY":
        return {
            "state": "UNKNOWN",
            "label": "SECURITY UNKNOWN",
            "score": None,
            "reasons": ["On-chain holder/authority checks are unavailable."],
        }

    reasons = []
    score = 0

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
    elif score >= 30:
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
            terms.append('"
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

    creation_task = ds.creation(mint)
    security_task = ds.security(mint)
    asset_task = he.asset(mint)
    candles_task = gt.candles(mint, "1m")
    x_task = xa.recent(x_query, 100)

    (
        (creation, creation_err),
        (security, security_err),
        (asset, asset_err),
        (d1, e1),
        (xp, xerr),
    ) = await asyncio.gather(
        creation_task,
        security_task,
        asset_task,
        candles_task,
        x_task,
    )

    raw1 = parse_candles(d1)
    c1 = closed_candles(raw1, 60)
    if c1:
        save_candles(mint, c1)
    c5 = aggregate_5m_from_1m(c1)
    setup = evaluate_setup(c5, c1) if c5 and c1 else None

    social_items = x_items(xp) if not xerr else []
    sm = social_metrics(social_items)
    market = market_metrics(c1, c5, data)

    sec_gate = security_gate(security)

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
        },
        "timestamp": int(time.time()),
    }


TOP_CACHE = {"time": 0, "data": None}
TOP_CACHE_SECONDS = 35


@app.get("/api/top")
async def top_opportunities():
    global TOP_CACHE

    now = time.time()
    if TOP_CACHE["data"] is not None and now - TOP_CACHE["time"] < TOP_CACHE_SECONDS:
        return TOP_CACHE["data"]

    candidates = await discover_candidates(
        limit=8,
        min_liquidity=10000,
    )

    async def inspect(candidate):
        try:
            analysis = await analyze(
                AnalyzeReq(
                    mint=candidate["address"],
                    x_query='lang:en -is:retweet',
                )
            )
            decision = analysis.get("decision") or {}
            risk = analysis.get("risk") or {}
            gate = analysis.get("security_gate") or {}
            overview = analysis.get("overview") or {}

            eligible = (
                gate.get("state") == "READY"
                and gate.get("label") == "PASS"
                and risk.get("overall") not in {"HIGH", "CRITICAL"}
                and decision.get("action") != "NO TRADE"
            )

            research_rank = (
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
                "research_rank": round(max(0, min(100, research_rank)), 1),
                "eligible": eligible,
                "decision": decision,
                "risk": risk,
                "security_gate": gate,
                "market": analysis.get("market"),
                "social": analysis.get("social"),
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

    inspected = await asyncio.gather(
        *[inspect(c) for c in candidates[:5]]
    )

    inspected = sorted(
        inspected,
        key=lambda x: (
            x.get("eligible", False),
            x.get("research_rank", 0),
        ),
        reverse=True,
    )

    result = {
        "state": "READY",
        "updated_at": int(now),
        "candidates": inspected,
        "top": next(
            (x for x in inspected if x.get("eligible")),
            None,
        ),
    }

    TOP_CACHE = {"time": now, "data": result}
    return result


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
 + clean.upper() + '"')
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
