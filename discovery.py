import asyncio
from typing import Any

import httpx

DEX = "https://api.dexscreener.com"


def num(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def score_pair(pair: dict) -> tuple[float, dict]:
    liq = num((pair.get("liquidity") or {}).get("usd"))
    vol5 = num((pair.get("volume") or {}).get("m5"))
    buys = num((pair.get("txns") or {}).get("m5", {}).get("buys"))
    sells = num((pair.get("txns") or {}).get("m5", {}).get("sells"))
    ch5 = num((pair.get("priceChange") or {}).get("m5"))
    ch1 = num((pair.get("priceChange") or {}).get("h1"))

    txns = buys + sells
    buy_ratio = buys / txns if txns else 0
    vol_ratio = vol5 / liq if liq else 0

    liq_score = min(25, liq / 100000 * 25)

    if vol_ratio < 0.01:
        vol_score = 0
    elif vol_ratio < 0.05:
        vol_score = 6
    elif vol_ratio < 0.15:
        vol_score = 13
    elif vol_ratio < 0.35:
        vol_score = 20
    elif vol_ratio < 0.75:
        vol_score = 12
    else:
        vol_score = 4

    if buy_ratio < 0.40:
        flow_score = 0
    elif buy_ratio < 0.50:
        flow_score = 3
    elif buy_ratio < 0.55:
        flow_score = 7
    elif buy_ratio < 0.60:
        flow_score = 11
    elif buy_ratio < 0.67:
        flow_score = 15
    elif buy_ratio < 0.75:
        flow_score = 18
    else:
        flow_score = 20

    momentum = max(-20, min(20, ch5 * 0.8))
    activity = min(10, txns / 20 * 10)

    if ch5 > 0 and ch1 > 0:
        trend = 5
    elif ch5 > 0:
        trend = 1
    elif ch1 > 0:
        trend = 2
    else:
        trend = -5

    score = max(
        0,
        min(
            100,
            liq_score
            + vol_score
            + flow_score
            + momentum
            + activity
            + trend,
        ),
    )

    return round(score, 2), {
        "buyRatio": round(buy_ratio, 4),
        "volumeLiquidityRatio": round(vol_ratio, 4),
        "liquidityScore": round(liq_score, 2),
        "volumeScore": round(vol_score, 2),
        "flowScore": round(flow_score, 2),
        "momentumScore": round(momentum, 2),
        "activityScore": round(activity, 2),
        "trendScore": trend,
    }


def qualifies(pair: dict, minimum_liquidity: float, pump_only: bool = False) -> bool:
    liq = num((pair.get("liquidity") or {}).get("usd"))
    vol5 = num((pair.get("volume") or {}).get("m5"))
    buys = num((pair.get("txns") or {}).get("m5", {}).get("buys"))
    sells = num((pair.get("txns") or {}).get("m5", {}).get("sells"))
    ch5 = num((pair.get("priceChange") or {}).get("m5"))

    is_pump_lane = (
        str(pair.get("dexId") or "").lower() in {"pumpswap", "pump"}
        or "pump.fun" in str(pair.get("url") or "").lower()
        or "pumpswap" in str(pair.get("url") or "").lower()
        or str((pair.get("baseToken") or {}).get("address") or "").lower().endswith("pump")
    )

    return (
        liq >= minimum_liquidity
        and vol5 >= 500
        and buys + sells >= 8
        and ch5 >= -12
        and (not pump_only or is_pump_lane)
    )


async def fetch(client: httpx.AsyncClient, path: str) -> list[dict]:
    try:
        r = await client.get(
            DEX + path,
            timeout=10,
            headers={"accept": "application/json"},
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else [data]
    except Exception:
        return []


async def discover_candidates(
    limit: int = 15,
    min_liquidity: float = 15000,
    pump_only: bool = False,
) -> list[dict]:

    async with httpx.AsyncClient() as client:
        profiles_task = fetch(
            client,
            "/token-profiles/latest/v1",
        )
        boosts_task = fetch(
            client,
            "/token-boosts/top/v1",
        )

        profiles, boosts = await asyncio.gather(
            profiles_task,
            boosts_task,
        )

        addresses = set()
        boosted = set()

        for item in profiles + boosts:
            if item.get("chainId") != "solana":
                continue

            address = item.get("tokenAddress")
            if address:
                addresses.add(address)

        for item in boosts:
            if item.get("chainId") == "solana" and item.get("tokenAddress"):
                boosted.add(item["tokenAddress"])

        addresses = list(addresses)[:80]

        if not addresses:
            return []

        pairs = await fetch(
            client,
            "/tokens/v1/solana/" + ",".join(addresses),
        )

    results = {}

    for pair in pairs:
        if pair.get("chainId") != "solana":
            continue

        token = pair.get("baseToken") or {}
        address = token.get("address")

        if not address or not qualifies(pair, min_liquidity, pump_only=pump_only):
            continue

        score, metrics = score_pair(pair)

        row = {
            "address": address,
            "symbol": token.get("symbol"),
            "name": token.get("name"),
            "pairAddress": pair.get("pairAddress"),
            "dexId": pair.get("dexId"),
            "priceUsd": num(pair.get("priceUsd")),
            "liquidityUsd": round(
                num((pair.get("liquidity") or {}).get("usd")),
                2,
            ),
            "volume5mUsd": round(
                num((pair.get("volume") or {}).get("m5")),
                2,
            ),
            "volume1hUsd": round(
                num((pair.get("volume") or {}).get("h1")),
                2,
            ),
            "buys5m": int(
                num((pair.get("txns") or {}).get("m5", {}).get("buys"))
            ),
            "sells5m": int(
                num((pair.get("txns") or {}).get("m5", {}).get("sells"))
            ),
            "priceChange5m": round(
                num((pair.get("priceChange") or {}).get("m5")),
                4,
            ),
            "priceChange1h": round(
                num((pair.get("priceChange") or {}).get("h1")),
                4,
            ),
            "marketCap": num(pair.get("marketCap")),
            "fdv": num(pair.get("fdv")),
            "pairCreatedAt": pair.get("pairCreatedAt"),
            "boosted": address in boosted,
            "pumpLane": (
                str(pair.get("dexId") or "").lower() in {"pumpswap", "pump"}
                or "pump.fun" in str(pair.get("url") or "").lower()
                or "pumpswap" in str(pair.get("url") or "").lower()
            ),
            "researchScore": score,
            "metrics": metrics,
        }

        old = results.get(address)

        if old is None or row["researchScore"] > old["researchScore"]:
            results[address] = row

    rows = list(results.values())

    rows.sort(
        key=lambda x: (
            x["pumpLane"],
            x["researchScore"],
            x["priceChange5m"],
            x["metrics"]["buyRatio"],
            x["liquidityUsd"],
        ),
        reverse=True,
    )

    return rows[:limit]
