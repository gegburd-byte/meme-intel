import asyncio
import json
import math
import time
from pathlib import Path

from discovery import discover_candidates
from engine import evaluate_setup
from main import (
    aggregate_5m_from_1m,
    closed_candles,
    gt,
    parse_candles,
)

RESULTS = Path("backtest_results.json")

MAX_CANDIDATES = 8
MIN_LIQUIDITY = 15000
MIN_CANDLES = 30
BETWEEN_TOKENS = 15

STOP_MULTIPLIER = 1.0
MAX_HOLD_MINUTES = 30


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (a / b - 1.0) * 100.0


async def get_candles(mint):
    data, err = await gt.candles(mint, "1m")

    if err:
        return None, err

    raw = parse_candles(data)
    c1 = closed_candles(raw, 60)

    return c1, None


def replay_token(candles):
    if len(candles) < MIN_CANDLES:
        return {
            "trades": [],
            "reason": "NEED_MORE_CANDLES",
        }

    trades = []
    in_trade = False
    entry_index = None
    entry_price = None
    stop_price = None
    entry_setup = None

    previous_state = None

    for i in range(MIN_CANDLES, len(candles)):
        history = candles[:i + 1]

        c5 = aggregate_5m_from_1m(history)

        if not c5:
            continue

        setup = evaluate_setup(c5, history)

        if setup is None:
            continue

        state = setup.state

        current = candles[i]

        if not in_trade:
            if (
                state == "BREAKOUT_CONFIRMED"
                and previous_state != "BREAKOUT_CONFIRMED"
            ):
                entry_price = current.c
                stop_price = setup.stop

                if entry_price is None or stop_price is None:
                    previous_state = state
                    continue

                in_trade = True
                entry_index = i
                entry_setup = {
                    "state": state,
                    "prev_high": setup.prev_high,
                    "higher_low": setup.higher_low,
                    "stop": setup.stop,
                }

        else:
            held_minutes = i - entry_index

            stop_hit = (
                stop_price is not None
                and current.l is not None
                and current.l <= stop_price
            )

            invalidated = state == "INVALIDATED"

            timed_out = held_minutes >= MAX_HOLD_MINUTES

            if stop_hit:
                exit_price = stop_price
                exit_reason = "STOP"

            elif invalidated:
                exit_price = current.c
                exit_reason = "INVALIDATED"

            elif timed_out:
                exit_price = current.c
                exit_reason = "TIMEOUT"

            else:
                previous_state = state
                continue

            return_pct = pct(exit_price, entry_price)

            trades.append({
                "entry_ts": candles[entry_index].ts,
                "exit_ts": current.ts,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "return_pct": return_pct,
                "exit_reason": exit_reason,
                "hold_minutes": held_minutes,
                "setup": entry_setup,
            })

            in_trade = False
            entry_index = None
            entry_price = None
            stop_price = None
            entry_setup = None

        previous_state = state

    if in_trade and entry_index is not None:
        final_price = candles[-1].c

        trades.append({
            "entry_ts": candles[entry_index].ts,
            "exit_ts": candles[-1].ts,
            "entry_price": entry_price,
            "exit_price": final_price,
            "return_pct": pct(final_price, entry_price),
            "exit_reason": "DATA_END",
            "hold_minutes": len(candles) - 1 - entry_index,
            "setup": entry_setup,
        })

    return {
        "trades": trades,
        "reason": None,
    }


def summarize(all_trades):
    if not all_trades:
        return {
            "trades": 0,
            "win_rate": None,
            "avg_return_pct": None,
            "median_return_pct": None,
            "total_compounded_pct": 0.0,
            "profit_factor": None,
            "max_drawdown_pct": 0.0,
        }

    returns = [
        float(t["return_pct"])
        for t in all_trades
        if t.get("return_pct") is not None
        and math.isfinite(float(t["return_pct"]))
    ]

    if not returns:
        return {
            "trades": len(all_trades),
            "win_rate": None,
            "avg_return_pct": None,
            "median_return_pct": None,
            "total_compounded_pct": 0.0,
            "profit_factor": None,
            "max_drawdown_pct": 0.0,
        }

    wins = [x for x in returns if x > 0]
    losses = [x for x in returns if x < 0]

    equity = 1.0
    peak = equity
    max_drawdown = 0.0

    for r in returns:
        equity *= 1.0 + r / 100.0
        peak = max(peak, equity)

        drawdown = (equity / peak - 1.0) * 100.0
        max_drawdown = min(max_drawdown, drawdown)

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "trades": len(returns),
        "win_rate": len(wins) / len(returns) * 100.0,
        "avg_return_pct": sum(returns) / len(returns),
        "median_return_pct": sorted(returns)[len(returns) // 2],
        "total_compounded_pct": (equity - 1.0) * 100.0,
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss > 0
            else None
        ),
        "max_drawdown_pct": max_drawdown,
    }


async def main():
    print("=== MEME INTEL BASELINE BACKTEST ===")
    print("Using current chart rules only.")
    print()

    candidates = await discover_candidates(
        limit=MAX_CANDIDATES,
        min_liquidity=MIN_LIQUIDITY,
    )

    print("Candidates discovered:", len(candidates))
    print()

    all_trades = []
    token_results = []

    for n, candidate in enumerate(candidates, start=1):
        symbol = candidate.get("symbol") or "UNKNOWN"
        mint = candidate["address"]

        print(f"[{n}/{len(candidates)}] {symbol}")

        candles, err = await get_candles(mint)

        if err:
            print("  DATA ERROR:", err)
            token_results.append({
                "symbol": symbol,
                "mint": mint,
                "error": err,
                "trades": [],
            })
            await asyncio.sleep(BETWEEN_TOKENS)
            continue

        if not candles or len(candles) < MIN_CANDLES:
            print("  Not enough closed 1m candles:", len(candles or []))
            token_results.append({
                "symbol": symbol,
                "mint": mint,
                "error": "NEED_MORE_CANDLES",
                "candle_count": len(candles or []),
                "trades": [],
            })
            await asyncio.sleep(BETWEEN_TOKENS)
            continue

        result = replay_token(candles)
        trades = result["trades"]

        print("  Candles:", len(candles))
        print("  Trades:", len(trades))

        for trade in trades:
            print(
                "   ",
                trade["exit_reason"],
                f"{trade['return_pct']:.2f}%",
                f"hold={trade['hold_minutes']}m",
            )

        all_trades.extend(
            {
                **trade,
                "symbol": symbol,
                "mint": mint,
            }
            for trade in trades
        )

        token_results.append({
            "symbol": symbol,
            "mint": mint,
            "candle_count": len(candles),
            "trades": trades,
        })

        await asyncio.sleep(BETWEEN_TOKENS)

    summary = summarize(all_trades)

    output = {
        "generated_at": int(time.time()),
        "method": {
            "entry": "BREAKOUT_CONFIRMED",
            "stop": "engine stop",
            "exit": [
                "STOP",
                "INVALIDATED",
                "TIMEOUT_30M",
                "DATA_END",
            ],
            "important": "This is a baseline research backtest, not a profit guarantee.",
        },
        "summary": summary,
        "token_results": token_results,
    }

    RESULTS.write_text(
        json.dumps(output, indent=2)
    )

    print()
    print("=" * 70)
    print("BASELINE RESULTS")
    print("=" * 70)
    print("Trades:", summary["trades"])
    print(
        "Win rate:",
        (
            f"{summary['win_rate']:.2f}%"
            if summary["win_rate"] is not None
            else "N/A"
        ),
    )
    print(
        "Average return:",
        (
            f"{summary['avg_return_pct']:.2f}%"
            if summary["avg_return_pct"] is not None
            else "N/A"
        ),
    )
    print(
        "Median return:",
        (
            f"{summary['median_return_pct']:.2f}%"
            if summary["median_return_pct"] is not None
            else "N/A"
        ),
    )
    print(
        "Compounded return:",
        f"{summary['total_compounded_pct']:.2f}%",
    )
    print(
        "Profit factor:",
        (
            f"{summary['profit_factor']:.2f}"
            if summary["profit_factor"] is not None
            else "N/A"
        ),
    )
    print(
        "Max drawdown:",
        f"{summary['max_drawdown_pct']:.2f}%",
    )
    print()
    print("Saved:", RESULTS)


if __name__ == "__main__":
    asyncio.run(main())
