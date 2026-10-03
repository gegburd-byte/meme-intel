import asyncio
import json
import time
from pathlib import Path

import httpx

from discovery import discover_candidates

API = "http://127.0.0.1:3000"
STATE_FILE = Path("paper_watcher_state.json")
POLL_SECONDS = 60
MAX_CANDIDATES = 3
BETWEEN_ANALYSES = 15


def load_state():
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def print_buy(symbol, candidate, setup):
    print()
    print("=" * 78)
    print("🚨🚨🚨  PAPER BUY SIGNAL  🚨🚨🚨")
    print(f"TOKEN: {symbol}")
    print("STATE: BREAKOUT_CONFIRMED")
    print(f"CA: {candidate['address']}")
    print(f"BREAKOUT LEVEL: {setup.get('prev_high')}")
    print(f"HIGHER LOW:     {setup.get('higher_low')}")
    print(f"PAPER STOP:     {setup.get('stop')}")
    print("RULE: closed 1m candle confirmed above previous 5m high")
    print("=" * 78)
    print()


def print_sell(symbol, candidate, setup):
    print()
    print("=" * 78)
    print("🛑🛑🛑  PAPER SELL / EXIT SIGNAL  🛑🛑🛑")
    print(f"TOKEN: {symbol}")
    print("STATE: INVALIDATED")
    print(f"CA: {candidate['address']}")
    print(f"REASON: {setup.get('last_reason')}")
    print(f"PAPER STOP: {setup.get('stop')}")
    print("RULE: setup is no longer valid — exit the paper position")
    print("=" * 78)
    print()


async def analyze(client, candidate):
    try:
        r = await client.post(
            API + "/api/analyze",
            json={
                "mint": candidate["address"],
                "x_query": candidate.get("symbol")
                or candidate["address"],
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


async def main():
    state = load_state()

    print("=== MEME INTEL PAPER WATCHER ===")
    print("Paper trading only.")
    print("Ctrl+C to stop.")

    async with httpx.AsyncClient() as client:
        while True:
            try:
                discovered = await discover_candidates(
                    limit=MAX_CANDIDATES,
                    min_liquidity=15000,
                )

                active = [
                    item.get("candidate")
                    for item in state.values()
                    if item.get("active") and item.get("candidate")
                ]

                active_mints = {
                    item["address"]
                    for item in active
                }

                candidates = active + [
                    item
                    for item in discovered
                    if item["address"] not in active_mints
                ]

                print()
                print(
                    time.strftime("[%Y-%m-%d %H:%M:%S]"),
                    "Candidates:",
                    len(candidates),
                )

                for candidate in candidates:
                    symbol = candidate.get("symbol") or "UNKNOWN"
                    mint = candidate["address"]

                    analysis = await analyze(client, candidate)
                    setup = analysis.get("setup") or {}

                    new_state = setup.get(
                        "state",
                        "DATA NOT AVAILABLE",
                    )

                    old = state.get(mint, {})
                    old_state = old.get("state")

                    print(
                        f"{symbol}: "
                        f"{old_state or 'NEW'} -> {new_state}"
                    )

                    if (
                        new_state == "BREAKOUT_CONFIRMED"
                        and old_state != "BREAKOUT_CONFIRMED"
                    ):
                        print_buy(
                            symbol,
                            candidate,
                            setup,
                        )

                        state[mint] = {
                            "state": new_state,
                            "active": True,
                            "entry_time": time.time(),
                            "candidate": candidate,
                            "setup": setup,
                        }

                    elif (
                        old.get("active")
                        and new_state == "INVALIDATED"
                    ):
                        print_sell(
                            symbol,
                            candidate,
                            setup,
                        )

                        state[mint] = {
                            "state": new_state,
                            "active": False,
                            "exit_time": time.time(),
                            "candidate": candidate,
                            "setup": setup,
                        }

                    else:
                        state[mint] = {
                            **old,
                            "state": new_state,
                            "last_seen": time.time(),
                            "setup": setup,
                            "candidate": candidate,
                        }

                    save_state(state)

                    await asyncio.sleep(BETWEEN_ANALYSES)

                await asyncio.sleep(POLL_SECONDS)

            except KeyboardInterrupt:
                print("\nWatcher stopped.")
                return

            except Exception as exc:
                print("WATCHER ERROR:", exc)
                await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
