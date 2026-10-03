from collections import Counter

from engine import Candle, evaluate_setup
from history_store import load_candles, token_counts
from main import aggregate_5m_from_1m


MIN_CANDLES = 30


def segments(rows):
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

    out = []
    cur = []

    for c in candles:
        if cur and c.ts - cur[-1].ts != 60:
            if len(cur) >= MIN_CANDLES:
                out.append(cur)
            cur = []

        cur.append(c)

    if len(cur) >= MIN_CANDLES:
        out.append(cur)

    return out


all_states = Counter()

for mint, count in token_counts(MIN_CANDLES):
    total = Counter()

    for candles in segments(load_candles(mint)):
        for i in range(MIN_CANDLES, len(candles)):
            c1 = candles[:i + 1]
            c5 = aggregate_5m_from_1m(c1)

            if not c5:
                continue

            setup = evaluate_setup(c5, c1)

            state = setup.state if setup else "NONE"
            total[state] += 1
            all_states[state] += 1

    print()
    print(mint)
    print("candles:", count)
    print(dict(total))

print()
print("=" * 70)
print("TOTAL STATE COUNTS")
print("=" * 70)
for state, count in all_states.most_common():
    print(f"{state}: {count}")
