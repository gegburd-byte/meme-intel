from collections import Counter

from engine import Candle, evaluate_setup
from history_store import load_candles, token_counts
from main import aggregate_5m_from_1m

MIN_CANDLES = 30

prior_higher_lows = 0
next_candle_breaks = 0
actual_confirmed = 0
by_token = Counter()

for mint, count in token_counts(MIN_CANDLES):
    rows = load_candles(mint)

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

    for i in range(MIN_CANDLES + 1, len(candles)):
        prior = candles[:i]
        c5 = aggregate_5m_from_1m(prior)

        if not c5:
            continue

        prior_setup = evaluate_setup(c5, prior)

        if prior_setup is None:
            continue

        if prior_setup.state != "HIGHER_LOW":
            continue

        prior_higher_lows += 1

        prev_high = prior_setup.prev_high
        current = candles[i]

        if (
            prev_high is not None
            and current.c is not None
            and current.c > prev_high
        ):
            next_candle_breaks += 1
            by_token[mint] += 1

            current_history = candles[:i + 1]
            current_c5 = aggregate_5m_from_1m(current_history)
            current_setup = evaluate_setup(
                current_c5,
                current_history,
            )

            actual_state = (
                current_setup.state
                if current_setup
                else "NONE"
            )

            print(
                "BREAK CANDIDATE",
                mint,
                "ts=", current.ts,
                "close=", current.c,
                "prev_high=", prev_high,
                "engine_state_after=", actual_state,
            )

            if actual_state == "BREAKOUT_CONFIRMED":
                actual_confirmed += 1

print()
print("=" * 70)
print("BREAKOUT TRANSITION DIAGNOSTIC")
print("=" * 70)
print("Prior HIGHER_LOW states:", prior_higher_lows)
print("Next-candle closes above prev_high:", next_candle_breaks)
print("Engine BREAKOUT_CONFIRMED:", actual_confirmed)

if by_token:
    print()
    print("By token:")
    for mint, count in by_token.most_common():
        print(mint, count)
