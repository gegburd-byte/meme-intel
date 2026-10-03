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

# --- Intelligence layer ----------------------------------------------------

def _clamp2(v, lo=0.0, hi=100.0):
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return lo


def ema(values, period):
    if len(values) < period:
        return None
    out = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for value in values[period:]:
        out = (value - out) * alpha + out
    return out


def rsi(values, period=14):
    if len(values) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c.h - c.l)
        else:
            prev = candles[i - 1].c
            trs.append(max(c.h - c.l, abs(c.h - prev), abs(c.l - prev)))
    return sum(trs[-period:]) / period


def vwap(candles):
    if not candles:
        return None
    pv = 0.0
    volume = 0.0
    for c in candles:
        v = max(c.v, 0.0)
        typical = (c.h + c.l + c.c) / 3
        pv += typical * v
        volume += v
    return pv / volume if volume > 0 else None


def volume_profile(candles, bins=40, value_area_pct=0.70):
    if not candles:
        return {"poc": None, "vah": None, "val": None, "value_area_pct": value_area_pct, "total_volume": 0.0, "bins": []}
    low = min(c.l for c in candles)
    high = max(c.h for c in candles)
    total = sum(max(c.v, 0.0) for c in candles)
    if high <= low:
        return {"poc": low, "vah": low, "val": low, "value_area_pct": value_area_pct, "total_volume": total, "bins": []}

    width = (high - low) / bins
    vols = [0.0] * bins
    for c in candles:
        typical = (c.h + c.l + c.c) / 3
        idx = int((typical - low) / width)
        idx = max(0, min(bins - 1, idx))
        vols[idx] += max(c.v, 0.0)

    if sum(vols) <= 0:
        return {"poc": (low + high) / 2, "vah": high, "val": low, "value_area_pct": value_area_pct, "total_volume": 0.0, "bins": []}

    poc_idx = max(range(bins), key=lambda i: vols[i])
    target = sum(vols) * value_area_pct
    included = {poc_idx}
    area = vols[poc_idx]
    left = poc_idx - 1
    right = poc_idx + 1

    while area < target and (left >= 0 or right < bins):
        lv = vols[left] if left >= 0 else -1
        rv = vols[right] if right < bins else -1
        if rv > lv:
            included.add(right)
            area += max(0.0, rv)
            right += 1
        else:
            included.add(left)
            area += max(0.0, lv)
            left -= 1

    def center(i):
        return low + (i + 0.5) * width

    val_idx = min(included)
    vah_idx = max(included)
    return {
        "poc": center(poc_idx),
        "vah": min(high, low + (vah_idx + 1) * width),
        "val": max(low, low + val_idx * width),
        "value_area_pct": value_area_pct,
        "total_volume": sum(vols),
        "bins": [
            {"low": low + i * width, "high": low + (i + 1) * width, "volume": vols[i]}
            for i in range(bins)
        ],
    }


def market_metrics(candles, candles5=None, overview=None):
    if not candles:
        return {"state": "NO_DATA", "profile": volume_profile([])}

    closes = [c.c for c in candles]
    vols = [max(c.v, 0.0) for c in candles]
    last = candles[-1]
    price = last.c

    def ret(n):
        if len(closes) <= n or closes[-n - 1] <= 0:
            return None
        return (price / closes[-n - 1] - 1) * 100

    profile = volume_profile(candles)
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    rv = rsi(closes, 14)
    av = atr(candles, 14)
    vw = vwap(candles)

    sample = [v for v in vols[-21:-1] if v > 0]
    vol_base = median(sample) if sample else 0.0
    vol_spike = vols[-1] / vol_base if vol_base > 0 else None

    ranges = [max(c.h - c.l, 0.0) for c in candles[-21:-1]]
    range_base = median(ranges) if ranges else 0.0
    range_expansion = (last.h - last.l) / range_base if range_base > 0 else None

    tx = (overview or {}).get("txns") or {}
    tx5 = tx.get("m5") or {}
    buys = float(tx5.get("buys") or 0)
    sells = float(tx5.get("sells") or 0)
    total_tx = buys + sells
    buy_ratio = buys / total_tx if total_tx else None

    liquidity = (overview or {}).get("liquidity")
    volume5 = (overview or {}).get("v5mUSD")
    turnover = float(volume5) / float(liquidity) if liquidity and volume5 else None

    return {
        "state": "READY",
        "price": price,
        "ema9": e9,
        "ema21": e21,
        "ema_trend": "BULLISH" if e9 is not None and e21 is not None and e9 > e21 else "BEARISH" if e9 is not None and e21 is not None else "UNKNOWN",
        "rsi14": rv,
        "atr14": av,
        "atr_pct": av / price * 100 if av and price else None,
        "vwap": vw,
        "volume_spike": vol_spike,
        "range_expansion": range_expansion,
        "return_1m_pct": ret(1),
        "return_5m_pct": ret(5),
        "return_15m_pct": ret(15),
        "return_30m_pct": ret(30),
        "above_vah": profile["vah"] is not None and price > profile["vah"],
        "below_val": profile["val"] is not None and price < profile["val"],
        "inside_value": profile["val"] is not None and profile["vah"] is not None and profile["val"] <= price <= profile["vah"],
        "distance_vah_pct": (price / profile["vah"] - 1) * 100 if profile["vah"] else None,
        "distance_poc_pct": (price / profile["poc"] - 1) * 100 if profile["poc"] else None,
        "distance_val_pct": (price / profile["val"] - 1) * 100 if profile["val"] else None,
        "buy_ratio_5m": buy_ratio,
        "buys_5m": buys,
        "sells_5m": sells,
        "volume_liquidity_ratio": turnover,
        "profile": profile,
    }


def risk_flags(
    *,
    liquidity_usd=None,
    market_cap=None,
    holder_concentration=None,
    mint_authority=None,
    freeze_authority=None,
    creator_blacklisted=None,
    social_domination=None,
    coordination_risk=None,
    vertical_move_pct=None,
):
    flags = []

    def add(level, code, reason):
        flags.append({"level": level, "code": code, "reason": reason})

    if liquidity_usd is None:
        add("UNKNOWN", "NO_LIQUIDITY_DATA", "Liquidity data unavailable.")
    elif liquidity_usd < 5000:
        add("CRITICAL", "VERY_LOW_LIQUIDITY", "Liquidity is extremely thin.")
    elif liquidity_usd < 20000:
        add("HIGH", "LOW_LIQUIDITY", "Liquidity is low for a volatile meme token.")

    if market_cap is not None and market_cap < 10000:
        add("HIGH", "TINY_MARKET_CAP", "Very small market capitalization.")

    if liquidity_usd and market_cap:
        ratio = market_cap / liquidity_usd
        if ratio > 80:
            add("HIGH", "THIN_LIQUIDITY_RELATIVE_TO_MC", "Market cap is very large relative to available liquidity.")
        elif ratio > 40:
            add("MEDIUM", "ELEVATED_MC_LIQUIDITY_RATIO", "Market cap is elevated relative to available liquidity.")

    if holder_concentration is not None and holder_concentration > 0.50:
        add("HIGH", "CONCENTRATED_HOLDERS", "One or a few holders control a large share.")
    if mint_authority:
        add("HIGH", "MINT_AUTHORITY", "Mint authority appears active.")
    if freeze_authority:
        add("HIGH", "FREEZE_AUTHORITY", "Freeze authority appears active.")
    if creator_blacklisted:
        add("CRITICAL", "CREATOR_FLAG", "Creator matched a configured blacklist.")

    if social_domination is not None and social_domination > 0.55:
        add("HIGH", "SOCIAL_DOMINATION", "A small number of accounts dominate the conversation.")

    if coordination_risk is not None:
        if coordination_risk >= 75:
            add("HIGH", "SOCIAL_COORDINATION", "X activity shows strong copy/paste or concentrated-account behavior.")
        elif coordination_risk >= 55:
            add("MEDIUM", "SOCIAL_COORDINATION", "X activity shows possible coordinated promotion.")

    if vertical_move_pct is not None:
        if vertical_move_pct >= 35:
            add("HIGH", "EXTENDED_MOVE", "Recent price expansion is extremely vertical; chasing is dangerous.")
        elif vertical_move_pct >= 20:
            add("MEDIUM", "EXTENDED_MOVE", "Recent price expansion is extended.")

    order = {"CRITICAL": 5, "HIGH": 4, "MEDIUM": 3, "UNKNOWN": 2, "LOW": 1}
    overall = "LOW"
    for flag in flags:
        if order.get(flag["level"], 0) > order.get(overall, 0):
            overall = flag["level"]
    return {"overall": overall, "flags": flags}


def signal_score(*, setup, market, social, risk, overview):
    technical = {
        "NO_SETUP": 20, "PUMP": 44, "PULLBACK": 55,
        "HIGHER_LOW": 72, "BREAKOUT_CONFIRMED": 92,
        "INVALIDATED": 0, "FAILED": 0,
    }.get(setup.state if setup else "NO_SETUP", 20)

    if market.get("ema_trend") == "BULLISH":
        technical += 6
    elif market.get("ema_trend") == "BEARISH":
        technical -= 12

    rv = market.get("rsi14")
    if rv is not None:
        if 50 <= rv <= 68:
            technical += 6
        elif rv > 80:
            technical -= 10
        elif rv < 35:
            technical -= 8

    if (market.get("return_5m_pct") or -999) > 0:
        technical += 4
    if (market.get("return_15m_pct") or -999) > 0:
        technical += 4
    technical = _clamp2(technical)

    buy_ratio = market.get("buy_ratio_5m")
    flow = 45.0 if buy_ratio is None else _clamp2(50 + (buy_ratio - 0.5) * 180)
    if market.get("volume_spike") is not None:
        if market["volume_spike"] >= 2:
            flow += 12
        elif market["volume_spike"] >= 1.3:
            flow += 6
        elif market["volume_spike"] < 0.7:
            flow -= 8
    flow = _clamp2(flow)

    velocity = float(social.get("mention_velocity") or 0)
    velocity_score = _clamp2(velocity * 18)
    sentiment = float(social.get("sentiment") or 0)
    author_quality = float(social.get("author_quality") or 0)
    coordination = float(social.get("coordination_risk") or 0)
    social_score = _clamp2(
        velocity_score * 0.35
        + sentiment * 0.25
        + author_quality * 0.30
        + (100 - coordination) * 0.10
    )

    price = market.get("price")
    profile = market.get("profile") or {}
    profile_score = 50.0
    if price and profile.get("vah"):
        if price > profile["vah"]:
            extension = max(0, (price / profile["vah"] - 1) * 100)
            profile_score = _clamp2(82 - extension * 4)
        elif profile.get("val") and price < profile["val"]:
            extension = max(0, (profile["val"] / max(price, 1e-12) - 1) * 100)
            profile_score = _clamp2(30 - extension * 3)
        else:
            profile_score = 58.0
    if profile.get("poc") and price and price > profile["poc"]:
        profile_score += 8
    profile_score = _clamp2(profile_score)

    liquidity = overview.get("liquidity")
    volume5 = overview.get("v5mUSD")
    liquidity_score = 35.0
    if liquidity is not None:
        liquidity_score = _clamp2(math.log10(max(float(liquidity), 1)) * 22 - 30)
    if volume5 and liquidity:
        turnover = float(volume5) / float(liquidity)
        if 0.03 <= turnover <= 0.60:
            liquidity_score += 14
        elif turnover > 1.0:
            liquidity_score -= 12
    liquidity_score = _clamp2(liquidity_score)

    penalty = {
        "LOW": 0, "UNKNOWN": 6, "MEDIUM": 15,
        "HIGH": 28, "CRITICAL": 55,
    }.get((risk or {}).get("overall", "UNKNOWN"), 15)

    score = (
        technical * 0.30
        + flow * 0.20
        + social_score * 0.20
        + profile_score * 0.15
        + liquidity_score * 0.15
        - penalty
    )

    return {
        "score": round(_clamp2(score), 1),
        "technical": round(technical, 1),
        "flow": round(flow, 1),
        "social": round(social_score, 1),
        "profile": round(profile_score, 1),
        "liquidity": round(liquidity_score, 1),
        "risk_penalty": penalty,
    }


def decision_engine(*, setup, market, social, risk, overview):
    signal = signal_score(setup=setup, market=market, social=social, risk=risk, overview=overview)
    profile = market.get("profile") or {}
    price = market.get("price")
    candidates = [x for x in [setup.prev_high if setup else None, profile.get("vah")] if x is not None]
    trigger = max(candidates) if candidates else None
    stop = setup.stop if setup else None

    extension = (price / trigger - 1) * 100 if price is not None and trigger and trigger > 0 else None
    checks = [
        setup and setup.state == "BREAKOUT_CONFIRMED",
        market.get("ema_trend") == "BULLISH",
        market.get("buy_ratio_5m") is not None and market["buy_ratio_5m"] >= 0.55,
        market.get("volume_spike") is not None and market["volume_spike"] >= 1.2,
        market.get("above_vah"),
        (social.get("unique_author_count") or 0) >= 3,
        (social.get("coordination_risk") or 0) < 45,
    ]
    confirmations = sum(1 for x in checks if x)

    risk_level = (risk or {}).get("overall", "UNKNOWN")
    state = setup.state if setup else "NO_SETUP"
    breakout_ready = (
        state == "BREAKOUT_CONFIRMED"
        and price is not None
        and trigger is not None
        and price >= trigger
        and (extension is None or extension <= 7.0)
    )
    blocked = risk_level == "CRITICAL" or state in {"INVALIDATED", "FAILED"}

    action = "NO TRADE"
    confidence = "LOW"
    reason = "Not enough independent confirmation."
    entry_style = "WAIT"

    if blocked:
        reason = "Risk/setup guardrail blocked the signal."
    elif breakout_ready and signal["score"] >= 70 and confirmations >= 4:
        action = "PAPER LONG TRIGGER"
        confidence = "HIGH" if confirmations >= 6 else "MEDIUM"
        reason = "Breakout confirmed with agreement across structure, trend, flow, market profile and social signals."
        entry_style = "BREAKOUT CLOSE / RETEST"
    elif state in {"PULLBACK", "HIGHER_LOW"} and signal["score"] >= 55:
        action = "WATCH"
        confidence = "MEDIUM"
        reason = "Structure is developing; wait for the actual breakout trigger."
        entry_style = "WAIT FOR 1M CLOSE ABOVE TRIGGER"
    elif extension is not None and extension > 7:
        reason = "Price is too extended above the trigger; do not chase."

    risk_per_unit = target1 = target2 = None
    if trigger and stop and trigger > stop:
        risk_per_unit = trigger - stop
        target1 = trigger + 1.5 * risk_per_unit
        target2 = trigger + 2.5 * risk_per_unit

    exits = [
        "1m close below invalidation / higher low",
        "Failed breakout that closes back below the trigger",
        "Sharp deterioration in liquidity or buy flow",
    ]
    if profile.get("val") is not None:
        exits.append("1m close back below VAL after a failed breakout")

    return {
        "action": action,
        "confidence": confidence,
        "score": signal["score"],
        "entry_style": entry_style,
        "entry_trigger": trigger,
        "invalidation": stop,
        "target1": target1,
        "target2": target2,
        "risk_per_unit": risk_per_unit,
        "extension_pct": extension,
        "confirmation_count": confirmations,
        "reason": reason,
        "exit_rules": exits,
        "components": {
            "technical": signal["technical"],
            "flow": signal["flow"],
            "social": signal["social"],
            "profile": signal["profile"],
            "liquidity": signal["liquidity"],
            "risk_penalty": signal["risk_penalty"],
        },
        "disclaimer": "Rule-based research and paper-trading signal; it cannot know the future or guarantee an entry or exit.",
    }
