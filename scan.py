import asyncio
import httpx

from discovery import discover_candidates

API = "http://127.0.0.1:3000"


async def analyze_candidate(
    client: httpx.AsyncClient,
    candidate: dict,
) -> dict:

    payload = {
        "mint": candidate["address"],
        "x_query": candidate.get("symbol") or candidate["address"],
    }

    try:
        response = await client.post(
            API + "/api/analyze",
            json=payload,
            timeout=20,
        )

        response.raise_for_status()

        data = response.json()

        return {
            "candidate": candidate,
            "analysis": data,
        }

    except Exception as exc:
        return {
            "candidate": candidate,
            "analysis": {
                "error": str(exc),
            },
        }


async def main():
    candidates = await discover_candidates(
        limit=3,
        min_liquidity=15000,
    )

    print()
    print("=== MEME INTEL LIVE SCAN ===")
    print("Candidates:", len(candidates))
    print()

    if not candidates:
        print("No candidates passed the discovery filters.")
        return

    async with httpx.AsyncClient() as client:
        results = []

        for candidate in candidates:
            result = await analyze_candidate(
                client,
                candidate,
            )

            results.append(result)

            # Give the free candle API some breathing room.
            await asyncio.sleep(15)

    for result in results:
        candidate = result["candidate"]
        analysis = result["analysis"]

        symbol = candidate.get("symbol") or "UNKNOWN"
        mint = candidate["address"]

        print("=" * 70)
        print(f"{symbol}")
        print(f"CA: {mint}")
        print(
            f"Discovery score: "
            f"{candidate.get('researchScore')}"
        )
        print(
            f"5m change: "
            f"{candidate.get('priceChange5m')}%"
        )
        print(
            f"Liquidity: "
            f"${candidate.get('liquidityUsd'):,.2f}"
        )

        if "error" in analysis:
            print("ANALYZE ERROR:", analysis["error"])
            continue

        setup = analysis.get("setup") or {}
        sources = analysis.get("sources") or {}
        risk = analysis.get("risk") or {}

        print(
            "Technical state:",
            setup.get("state"),
        )

        print(
            "Reason:",
            setup.get("last_reason"),
        )

        print(
            "Previous high:",
            setup.get("prev_high"),
        )

        print(
            "Higher low:",
            setup.get("higher_low"),
        )

        print(
            "Stop:",
            setup.get("stop"),
        )

        print(
            "Gecko:",
            sources.get("GeckoTerminal"),
        )

        print(
            "Risk:",
            risk.get("overall"),
        )

        print(
            "Score:",
            (analysis.get("score") or {}).get("score"),
        )

    print("=" * 70)
    print("SCAN COMPLETE")


if __name__ == "__main__":
    asyncio.run(main())
