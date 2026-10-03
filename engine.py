from __future__ import annotations
from dataclasses import dataclass, asdict
from statistics import median
from typing import Optional, Literal
import math

State = Literal[
    "NO_SETUP", "PUMP", "PULLBACK", "HIGHER_LOW",
    "BREAKOUT_CONFIRMED", "FAILED", "INVALIDATED"
]

@dataclass
class Candle:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0

@dataclass
class Setup:
    state: State
    prev_high: Optional[float] = None
    higher_low: Optional[float] = None
    stop: Optional[float] = None
    pump_start: Optional[float] = None
    pump_end: Optional[int] = None
    last_reason: str = ""

    def dict(self):
        return asdict(self)

def _safe_pct(a: float, b: float) -> float:
    if a <= 0:
        return 0.0
    return (b - a) / a

def detect_pump(candles: list[Candle], lookback: int = 12,
                min_move: float = 0.08, volume_mult: float = 1.25) -> bool:
    """Structure-driven impulse detection; no fixed candle count is required."""
    if len(candles) < 5:
        return False
    window = candles[-lookback:]
    start = min(window[0].o, min(c.l for c in window[:max(2, len(window)//3)]))
    end = max(c.h for c in window)
    move = _safe_pct(start, end)
    vols = [c.v for c in window[:-2] if c.v > 0]
    recent_vol = sum(c.v for c in window[-3:])
    baseline = median(vols) * 3 if vols else 0
    green_ratio = sum(1 for c in window if c.c >= c.o) / len(window)
    volume_ok = baseline == 0 or recent_vol >= baseline / max(1, len(window))
    return move >= min_move and green_ratio >= 0.45 and volume_ok and end > start

def highest_wick(candles: list[Candle]) -> Optional[float]:
    return max((c.h for c in candles), default=None)

def _local_lows(candles: list[Candle]) -> list[Candle]:
    lows = []
    for i in range(1, len(candles)-1):
        if candles[i].l <= candles[i-1].l and candles[i].l <= candles[i+1].l:
            lows.append(candles[i])
    return lows

def confirm_higher_low(candles: list[Candle], pump_start_low: float,
                       tolerance: float = 0.0) -> Optional[float]:
    lows = _local_lows(candles)
    if not lows:
        return None
    latest = lows[-1].l
    if latest > pump_start_low + tolerance:
        return latest
    return None

def evaluate_setup(c5: list[Candle], c1: list[Candle],
                   stop_buffer_pct: float = 0.01,
                   min_pump: float = 0.08,
                   min_retrace: float = 0.08) -> Setup:
    """Formal 5m→1m state machine using closed candles only."""

    if len(c1) < 6:
        return Setup("NO_SETUP", last_reason="NEED_MORE_CANDLES")

    last = c1[-1]

    # Build structure only from completed 5m candles
    # BEFORE the current 1m candle's 5m bucket.
    current_bucket = (last.ts // 300) * 300
    structure_c5 = [
        c for c in c5
        if c.ts < current_bucket
    ]

    if len(structure_c5) < 6:
        return Setup("NO_SETUP", last_reason="NEED_MORE_CANDLES")

    if not detect_pump(
        structure_c5,
        min_move=min_pump
    ):
        return Setup(
            "NO_SETUP",
            last_reason="NO_CLEAR_PUMP"
        )

    # Pump high is the highest wick BEFORE the pullback.
    highs = [c.h for c in structure_c5]
    peak_idx = max(
        range(len(highs)),
        key=lambda i: highs[i]
    )

    if peak_idx >= len(structure_c5) - 1:
        return Setup(
            "PUMP",
            prev_high=highs[peak_idx],
            last_reason="WAIT_FOR_PULLBACK"
        )

    prev_high = highs[peak_idx]

    post = structure_c5[peak_idx + 1:]

    post_low = min(
        c.l for c in post
    )

    retrace = _safe_pct(
        post_low,
        prev_high
    )

    if retrace < min_retrace:
        return Setup(
            "PUMP",
            prev_high=prev_high,
            last_reason="PULLBACK_NOT_DEEP_ENOUGH"
        )

    prior_low = min(
        c.l
        for c in structure_c5[:peak_idx + 1]
    )

    hl = confirm_higher_low(
        post,
        prior_low
    )

    if hl is None:
        return Setup(
            "PULLBACK",
            prev_high=prev_high,
            last_reason="WAITING_FOR_HIGHER_LOW"
        )

    stop = hl * (
        1 - stop_buffer_pct
    )

    # Current closed 1m candle rejects the breakout.
    if (
        last.h > prev_high
        and last.c < prev_high
    ):
        return Setup(
            "HIGHER_LOW",
            prev_high=prev_high,
            higher_low=hl,
            stop=stop,
            last_reason="WICK_BREAK_REJECTED"
        )

    # Current closed 1m candle confirms the breakout.
    if last.c > prev_high:
        return Setup(
            "BREAKOUT_CONFIRMED",
            prev_high=prev_high,
            higher_low=hl,
            stop=stop,
            last_reason="1M_CLOSE_ABOVE_PREVIOUS_HIGH"
        )

    # Current 1m candle breaks the higher low.
    if last.l < hl:
        return Setup(
            "INVALIDATED",
            prev_high=prev_high,
            higher_low=hl,
            stop=stop,
            last_reason="BREAK_BELOW_HIGHER_LOW"
        )

    return Setup(
        "HIGHER_LOW",
        prev_high=prev_high,
        higher_low=hl,
        stop=stop,
        last_reason="WAITING_FOR_1M_CLOSE"
    )

def risk_flags(*, liquidity_usd=None, market_cap=None, holder_concentration=None,
               mint_authority=None, freeze_authority=None, creator_blacklisted=None,
               social_domination=None):
    flags = []
    def add(level, code, why):
        flags.append({"level": level, "code": code, "reason": why})
    if liquidity_usd is None:
        add("UNKNOWN","NO_LIQUIDITY_DATA","Liquidity data unavailable")
    elif liquidity_usd < 5000:
        add("CRITICAL","VERY_LOW_LIQUIDITY","Liquidity is extremely thin")
    elif liquidity_usd < 20000:
        add("HIGH","LOW_LIQUIDITY","Liquidity is low")

    if market_cap is not None and market_cap < 10000:
        add("HIGH","TINY_MARKET_CAP","Very small market capitalization")

    if holder_concentration is not None and holder_concentration > 0.50:
        add("HIGH","CONCENTRATED_HOLDERS","One/few holders control a large share")

    if mint_authority:
        add("HIGH","MINT_AUTHORITY","Mint authority appears active")
    if freeze_authority:
        add("HIGH","FREEZE_AUTHORITY","Freeze authority appears active")
    if creator_blacklisted:
        add("CRITICAL","CREATOR_FLAG","Creator matched a configured blacklist")
    if social_domination is not None and social_domination > 0.55:
        add("HIGH","SOCIAL_DOMINATION","A small number of accounts dominate the conversation")

    order = {"CRITICAL":4,"HIGH":3,"MEDIUM":2,"UNKNOWN":1,"LOW":1}
    overall = "LOW"
    for f in flags:
        if order.get(f["level"],0) > order.get(overall,0):
            overall = f["level"]
    return {"overall": overall, "flags": flags}

def opportunity_score(*, technical, social_velocity, sentiment,
                      liquidity, corroboration, risk_penalty,
                      data_complete=True):
    parts = {
        "technical": max(0, min(100, technical)),
        "social_velocity": max(0, min(100, social_velocity)),
        "sentiment": max(0, min(100, sentiment)),
        "liquidity": max(0, min(100, liquidity)),
        "corroboration": max(0, min(100, corroboration)),
    }
    weights = {
        "technical": 0.20,
        "social_velocity": 0.15,
        "sentiment": 0.15,
        "liquidity": 0.15,
        "corroboration": 0.20,
    }
    raw = sum(parts[k]*weights[k] for k in weights)
    final = max(0.0, raw - max(0, min(100, risk_penalty)) * 0.15)
    return {
        "score": round(final, 1),
        "complete": bool(data_complete),
        "components": {k: {"value": round(v,1), "weight": weights[k]}
                       for k,v in parts.items()},
        "note": "Score is a transparent research heuristic, not a probability or prediction."
    }
