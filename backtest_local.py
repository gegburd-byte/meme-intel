import json
import math
from pathlib import Path
from statistics import median

from engine import Candle, evaluate_setup
from history_store import load_candles, token_counts
from main import aggregate_5m_from_1m

MIN_CANDLES = 30
MAX_HOLD_MINUTES = 30
RESULTS = Path("backtest_local_results.json")


def pct(exit_price, entry_price):
    if entry_price in (None, 0) or exit_price is None:
        return None
    return (exit_price / entry_price - 1) * 100


def contiguous_segments(rows):
    candles = [
        Candle(
            ts=int(ts),
            o=float(o),
            h=float(h),
            l=float(l),
            c=float(c),
            v=float(v),
        )
        for ts, o, h, l, c, v in rows
    ]

    segments = []
    current = []

    for candle in candles:
        if not current:
            current = [candle]
            continue

        if candle.ts - current[-1].ts == 60:
            current.append(candle)
        else:
            if len(current) >= MIN_CANDLES:
                segments.append(current)
            current = [candle]

    if len(current) >= MIN_CANDLES:
        segments.append(current)

    return segments


def replay_segment(candles):
    trades = []
    active = False
    entry_i = None
    entry_price = None
    stop = None
    entry_setup = None
    previous_state = None

    for i in range(MIN_CANDLES, len(candles)):
        history = candles[:i + 1]
        c5 = aggregate_5m_from_1m(history)

        if not c5:
            continue

        setup = evaluate_setup(c5, history)
        state = setup.state if setup else None
        current = candles[i]

        if not active:
            if (
                state == "BREAKOUT_CONFIRMED"
                and previous_state != "BREAKOUT_CONFIRMED"
                and setup.stop is not None
            ):
                active = True
                entry_i = i
                entry_price = current.c
                stop = setup.stop
                entry_setup = {
                    "prev_high": setup.prev_high,
                    "higher_low": setup.higher_low,
                    "stop": setup.stop,
                }

        else:
            held = i - entry_i

            stop_hit = (
                stop is not None
                and current.l <= stop
            )

            invalidated = state == "INVALIDATED"
            timeout = held >= MAX_HOLD_MINUTES

            if stop_hit:
                exit_price = stop
                reason = "STOP"
            elif invalidated:
                exit_price = current.c
                reason = "INVALIDATED"
            elif timeout:
                exit_price = current.c
                reason = "TIMEOUT"
            else:
                previous_state = state
                continue

            trades.append({
                "entry_ts": candles[entry_i].ts,
                "exit_ts": current.ts,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "return_pct": pct(exit_price, entry_price),
                "hold_minutes": held,
                "exit_reason": reason,
                "setup": entry_setup,
            })

            active = False
            entry_i = None
            entry_price = None
            stop = None
            entry_setup = None

        previous_state = state

    return trades


def summarize(trades):
    returns = [
        float(t["return_pct"])
        for t in trades
        if t.get("return_pct") is not None
        and math.isfinite(float(t["return_pct"]))
    ]

    if not returns:
        return {
            "trades": 0,
            "win_rate": None,
            "average_return_pct": None,
            "median_return_pct": None,
            "compounded_return_pct": 0,
            "profit_factor": None,
            "max_drawdown_pct": 0,
        }

    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]

    equity = 1.0
    peak = 1.0
    max_dd = 0.0

    for r in returns:
        equity *= 1 + r / 100
        peak = max(peak, equity)
        dd = (equity / peak - 1) * 100
        max_dd = min(max_dd, dd)

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "trades": len(returns),
        "win_rate": len(wins) / len(returns) * 100,
        "average_return_pct": sum(returns) / len(returns),
        "median_return_pct": median(returns),
        "compounded_return_pct": (equity - 1) * 100,
        "profit_factor": (
            gross_profit / gross_loss
            if gross_loss
            else None
        ),
        "max_drawdown_pct": max_dd,
    }


def main():
    print("=== MEME INTEL LOCAL BACKTEST ===")
    print("Source: market_history.db")
    print("No Gecko candle requests.")
    print()

    rows = token_counts(MIN_CANDLES)

    print("Tokens with enough history:", len(rows))
    print()

    all_trades = []
    token_results = []

    for n, (mint, count) in enumerate(rows, 1):
        raw = load_candles(mint)
        segments = contiguous_segments(raw)

        trades = []

        for segment in segments:
            trades.extend(replay_segment(segment))

        print(
            f"[{n}/{len(rows)}] "
            f"{mint} | "
            f"candles={count} | "
            f"segments={len(segments)} | "
            f"trades={len(trades)}"
        )

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
                "mint": mint,
            }
            for trade in trades
        )

        token_results.append({
            "mint": mint,
            "stored_candles": count,
            "segments": len(segments),
            "trades": trades,
        })

    summary = summarize(all_trades)

    output = {
        "summary": summary,
        "token_results": token_results,
        "method": {
            "entry": "BREAKOUT_CONFIRMED",
            "stop": "engine stop / candle low",
            "invalidated_exit": True,
            "timeout_minutes": MAX_HOLD_MINUTES,
        },
        "note": (
            "Research backtest using locally collected candles. "
            "Small or biased samples should not be treated as "
            "evidence of future profitability."
        ),
    }

    RESULTS.write_text(json.dumps(output, indent=2))

    print()
    print("=" * 70)
    print("LOCAL BACKTEST RESULTS")
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
            f"{summary['average_return_pct']:.2f}%"
            if summary["average_return_pct"] is not None
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
        f"{summary['compounded_return_pct']:.2f}%",
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
    main()
