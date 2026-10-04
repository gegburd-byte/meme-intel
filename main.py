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
    "/api/chart/history": (4, 10.0),
    "/api/chart/meta": (10, 10.0),
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

def parse_pump_trades(payload):
    """Normalize Pump.fun trade-history rows into simple price/time/volume trades."""
    if isinstance(payload, dict):
        payload = (
            payload.get("data")
            or payload.get("trades")
            or payload.get("results")
            or []
        )

    if not isinstance(payload, list):
        return []

    trades = []

    for item in payload:
        if not isinstance(item, dict):
            continue

        try:
            timestamp = (
                item.get("timestamp")
                or item.get("time")
                or item.get("created_timestamp")
                or item.get("block_time")
            )

            if timestamp is None:
                continue

            ts = int(float(timestamp))
            if ts > 10_000_000_000:
                ts //= 1000

            if ts < 1_500_000_000:
                continue

            # Pump.fun trade history normally exposes raw lamport/token units.
            sol_raw = (
                item.get("sol_amount")
                or item.get("solAmount")
                or item.get("sol_amount_lamports")
                or item.get("sol")
                or item.get("amount_sol")
            )

            token_raw = (
                item.get("token_amount")
                or item.get("tokenAmount")
                or item.get("token_amount_raw")
                or item.get("tokens")
            )

            virtual_sol = (
                item.get("virtual_sol_reserves")
                or item.get("virtualSolReserves")
            )
            virtual_token = (
                item.get("virtual_token_reserves")
                or item.get("virtualTokenReserves")
            )

            price = None

            if virtual_sol is not None and virtual_token is not None:
                vs = float(virtual_sol)
                vt = float(virtual_token)

                if vs > 0 and vt > 0:
                    price = (
                        (vs / 1_000_000_000) /
                        (vt / 1_000_000)
                    )

            if price is None and sol_raw is not None and token_raw is not None:
                sol_value = float(sol_raw)
                token_value = float(token_raw)

                if sol_value > 1_000_000:
                    sol_value /= 1_000_000_000

                if token_value > 1_000_000_000:
                    token_value /= 1_000_000

                if sol_value > 0 and token_value > 0:
                    price = sol_value / token_value

            if price is None:
                raw_price = (
                    item.get("price")
                    or item.get("price_sol")
                    or item.get("priceSol")
                )

                if raw_price is not None:
                    price = float(raw_price)

            if price is None or price <= 0:
                continue

            volume = 0.0

            if sol_raw is not None:
                volume = float(sol_raw)
                if volume > 1_000_000:
                    volume /= 1_000_000_000

            trades.append({
                "ts": ts,
                "price": price,
                "volume": max(0.0, volume),
            })
        except (TypeError, ValueError):
            continue

    trades.sort(key=lambda x: x["ts"])
    return trades


def aggregate_pump_trade_candles(payloads, timeframe=1, limit=120):
    span = max(60, int(timeframe or 1) * 60)
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
                }
                continue

            row["h"] = max(row["h"], price)
            row["l"] = min(row["l"], price)
            row["c"] = price
            row["v"] += float(trade["volume"])

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


@app.get("/api/chart/history")
async def chart_history(mint: str, timeframe: int = 1, limit: int = 120):
    """Fast historical backfill with Pump.fun as the preferred source."""
    mint = (mint or "").strip()

    if len(mint) < 32 or len(mint) > 44:
        raise HTTPException(400, "Invalid mint")

    if timeframe not in {1, 5, 15, 60}:
        raise HTTPException(400, "Unsupported timeframe")

    limit = max(30, min(int(limit or 120), 120))

    cache_key = (mint, timeframe, limit)
    cached = getattr(chart_history, "_cache", {}).get(cache_key)
    now = time.time()

    if cached and now - cached["time"] < 8.0:
        return cached["payload"]

    async def native_history():
        try:
            payload, err = await asyncio.wait_for(
                pf.candles(
                    mint,
                    limit=limit,
                    timeframe=timeframe,
                    offset=0,
                    fresh=True,
                ),
                timeout=3.0,
            )
            rows = parse_pump_candles(payload)
            return "PUMP.FUN", rows, err
        except Exception as exc:
            return "PUMP.FUN", [], str(exc)[:240]

    async def pump_trade_history():
        try:
            results = await asyncio.gather(
                pf.trades(
                    mint,
                    limit=200,
                    offset=0,
                    minimum_size=0,
                    fresh=True,
                ),
                pf.trades(
                    mint,
                    limit=200,
                    offset=200,
                    minimum_size=0,
                    fresh=True,
                ),
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
                timeframe=timeframe,
                limit=limit,
            )

            return "PUMP.FUN TRADE HISTORY", rows, (
                None if rows else "NO_TRADES_DECODED"
            )
        except Exception as exc:
            return "PUMP.FUN TRADE HISTORY", [], str(exc)[:240]

    async def helius_history():
        try:
            rows, err = await asyncio.wait_for(
                he.historical_trade_candles(
                    mint,
                    timeframe=timeframe,
                    lookback_minutes=min(
                        10080,
                        max(120, limit * timeframe),
                    ),
                    max_signatures=1500,
                ),
                timeout=7.0,
            )
            return "HELIUS_ONCHAIN_TRADES", rows or [], err
        except Exception as exc:
            return "HELIUS_ONCHAIN_TRADES", [], str(exc)[:240]

    async def gecko_history():
        try:
            payload, err = await asyncio.wait_for(
                gt.candles(mint, "1m"),
                timeout=5.0,
            )
            base = parse_candles(payload)
            rows = aggregate_timeframe_candles(
                base,
                timeframe,
            )[-limit:]
            return "GECKOTERMINAL", rows, err
        except Exception as exc:
            return "GECKOTERMINAL", [], str(exc)[:240]

    native_task = asyncio.create_task(native_history())
    gecko_task = asyncio.create_task(gecko_history())

    # Keep all sources running in parallel. Native Pump.fun remains preferred,
    # while Gecko provides a fast visible fallback if native HTTP is unavailable.
    # Fidelity-first fallbacks: Pump.fun trade history and Helius on-chain
    # reconstruction both use Pump.fun trade semantics. GeckoTerminal is kept
    # as a last-resort compatibility source because its quote/venue history can
    # use different units than Pump.fun.
    primary_fallback_tasks = [
        asyncio.create_task(pump_trade_history()),
        asyncio.create_task(helius_history()),
    ]

    native_source, native_rows, native_err = await native_task

    if native_rows:
        payload = {
            "state":"READY",
            "source":"PUMP.FUN",
            "candles":[
                {
                    "ts":c.ts,
                    "o":c.o,
                    "h":c.h,
                    "l":c.l,
                    "c":c.c,
                    "v":c.v,
                }
                for c in sorted(native_rows,key=lambda x:x.ts)[-limit:]
            ],
            "error":None,
            "timestamp":int(time.time()),
        }

        for task in primary_fallback_tasks:
            if not task.done():
                task.cancel()
        if not gecko_task.done():
            gecko_task.cancel()

        await asyncio.gather(
            *primary_fallback_tasks,
            gecko_task,
            return_exceptions=True,
        )

        chart_history._cache = getattr(
            chart_history,
            "_cache",
            {},
        )

        # Bound cache growth.
        if len(chart_history._cache) > 64:
            chart_history._cache.clear()

        chart_history._cache[cache_key] = {
            "time":time.time(),
            "payload":payload,
        }

        return payload

    # No native history: use fast Gecko data immediately when it is already
    # available; otherwise use Pump.fun trade history or Helius reconstruction.
    fallback_errors = []

    if gecko_task.done():
        try:
            gecko_source, gecko_rows, gecko_err = await gecko_task
        except Exception as exc:
            gecko_source, gecko_rows, gecko_err = "GECKOTERMINAL", [], str(exc)[:180]
    else:
        gecko_source, gecko_rows, gecko_err = "GECKOTERMINAL", [], "PENDING"

    if gecko_rows:
        payload = {
            "state":"READY",
            "source":gecko_source,
            "candles":[
                {
                    "ts":c.ts,
                    "o":c.o,
                    "h":c.h,
                    "l":c.l,
                    "c":c.c,
                    "v":c.v,
                }
                for c in sorted(gecko_rows,key=lambda x:x.ts)[-limit:]
            ],
            "error":"FAST_FALLBACK_NON_NATIVE",
            "timestamp":int(time.time()),
        }

        for other in primary_fallback_tasks:
            if not other.done():
                other.cancel()

        await asyncio.gather(
            *primary_fallback_tasks,
            return_exceptions=True,
        )

        chart_history._cache = getattr(chart_history, "_cache", {})
        chart_history._cache[cache_key] = {
            "time":time.time(),
            "payload":payload,
        }
        return payload

    for task in asyncio.as_completed(primary_fallback_tasks):
        try:
            source, rows, err = await task
        except Exception as exc:
            fallback_errors.append(str(exc)[:180])
            continue

        if rows:
            payload = {
                "state":"READY",
                "source":source,
                "candles":[
                    {
                        "ts":c.ts,
                        "o":c.o,
                        "h":c.h,
                        "l":c.l,
                        "c":c.c,
                        "v":c.v,
                    }
                    for c in sorted(rows,key=lambda x:x.ts)[-limit:]
                ],
                "error":None,
                "timestamp":int(time.time()),
            }

            for other in primary_fallback_tasks:
                if not other.done():
                    other.cancel()

            await asyncio.gather(
                *primary_fallback_tasks,
                return_exceptions=True,
            )

            chart_history._cache = getattr(
                chart_history,
                "_cache",
                {},
            )

            if len(chart_history._cache) > 64:
                chart_history._cache.clear()

            chart_history._cache[cache_key] = {
                "time":time.time(),
                "payload":payload,
            }

            return payload

        fallback_errors.append(
            str(err or source)[:180]
        )

    await asyncio.gather(
        *primary_fallback_tasks,
        return_exceptions=True,
    )

    # Last resort: wait briefly for the already-running Gecko request rather
    # than starting a duplicate HTTP request.
    if not gecko_rows:
        try:
            gecko_source, gecko_rows, gecko_err = await asyncio.wait_for(
                gecko_task,
                timeout=0.75,
            )
        except asyncio.TimeoutError:
            gecko_source, gecko_rows, gecko_err = "GECKOTERMINAL", [], "GECKO_TIMEOUT"
        except Exception as exc:
            gecko_source, gecko_rows, gecko_err = "GECKOTERMINAL", [], str(exc)[:180]

    # Last resort only: GeckoTerminal is useful for showing something when no
    # Pump.fun-semantic history can be reconstructed, but it is explicitly not
    # treated as exact Pump.fun chart data.
    if gecko_rows:
        payload = {
            "state":"READY",
            "source":gecko_source,
            "candles":[
                {
                    "ts":c.ts,
                    "o":c.o,
                    "h":c.h,
                    "l":c.l,
                    "c":c.c,
                    "v":c.v,
                }
                for c in sorted(gecko_rows,key=lambda x:x.ts)[-limit:]
            ],
            "error":"LAST_RESORT_NON_PUMPFUN_SOURCE",
            "timestamp":int(time.time()),
        }

        chart_history._cache = getattr(
            chart_history,
            "_cache",
            {},
        )
        if len(chart_history._cache) > 64:
            chart_history._cache.clear()
        chart_history._cache[cache_key] = {
            "time":time.time(),
            "payload":payload,
        }
        return payload

    fallback_errors.append(
        str(gecko_err or gecko_source)[:180]
    )

    payload = {
        "state":"NO_CANDLES",
        "source":"NONE",
        "candles":[],
        "error":(
            native_err or
            "; ".join(fallback_errors) or
            "NO_HISTORY"
        ),
        "timestamp":int(time.time()),
    }

    chart_history._cache = getattr(
        chart_history,
        "_cache",
        {},
    )

    chart_history._cache[cache_key] = {
        "time":time.time(),
        "payload":payload,
    }

    return payload


@app.get("/api/chart/current")
async def chart_current(mint: str, timeframe: int = 1):
    """Fast Pump.fun native active-candle endpoint.

    When the native Pump.fun candle endpoint is unavailable, the browser uses
    the live Pump.fun/on-chain trade stream to build the active bar itself.
    No Helius/DexScreener price is substituted into OHLC.
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

    # Native Pump.fun OHLC is authoritative. If the native HTTP endpoint is
    # unavailable (including JWT-protected deployments), fall back to the
    # already-decoded Pump.fun websocket trades in memory. This keeps the active
    # candle moving without substituting DexScreener/Helius asset prices into OHLC.
    live = trade_hub.current_candle(mint, timeframe=timeframe)
    if live:
        return {
            "state": "READY",
            "source": live.get("source", "PUMP.FUN LIVE TRADES"),
            "timeframe": timeframe,
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
        "timeframe": timeframe,
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
