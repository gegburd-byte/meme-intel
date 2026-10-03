# Meme Intel — Solana research terminal (paper trading)

This is a Replit-friendly, browser-based research terminal built from the attached spec.

It is intentionally **analysis + paper trading only**. It does not connect to a wallet or place trades.

## What is implemented

- 5m → 1m setup state machine:
  - pump / impulse detection
  - previous high = highest wick
  - pullback detection
  - higher-low confirmation
  - breakout requires a **1m candle close** above the previous high
  - false-breakout rejection
  - invalidation stop below the higher low
- Risk screen with explicit LOW / MEDIUM / HIGH / CRITICAL flags.
- Transparent opportunity score.
- X post search adapter using the X API recent-search endpoint.
- Solana token overview + OHLCV + security/creation checks through Birdeye.
- Helius token metadata endpoint.
- Social duplicate / domination checks.
- Paper-trade ledger.
- Walk-forward-style backtest on candle data you provide.
- No-lookahead design for the setup engine.
- Clear `DATA NOT AVAILABLE` / `NOT_CONFIGURED` states instead of invented numbers.
- Single-page dark terminal UI.

## Live-data reality

Live X and market data require API credentials. X's current developer tooling requires enrollment/access for the API endpoints, and X documents authentication/rate-limit errors for inaccessible endpoints. See the official X developer docs.

Birdeye currently exposes Solana token overview, security and OHLCV endpoints; plan/access limits vary. Helius exposes Solana RPC/DAS functionality such as `getAsset`.

The app never invents missing data. If a key is not configured, the relevant source is marked `NOT_CONFIGURED`.

## Replit

1. Create a new Replit.
2. Upload the contents of this ZIP.
3. Replit should detect `requirements.txt` and `.replit`.
4. Set Secrets in Replit:
   - `X_BEARER_TOKEN`
   - `BIRDEYE_API_KEY`
   - `HELIUS_API_KEY`
5. Click Run.
6. Open the web preview.

Start command is:

    uvicorn main:app --host 0.0.0.0 --port 3000

## Environment variables

See `.env.example`.

Optional:
- `DATABASE_PATH` — defaults to `./data/meme_intel.db`
- `PAPER_START_USD` — defaults to `100`
- `SCAN_SECONDS` — defaults to `30`

## How to use the terminal

1. Paste a Solana mint address into **Analyze Token**.
2. Click Analyze.
3. The terminal pulls what your configured providers can supply.
4. Review:
   - LIVE SETUP
   - TECHNICAL STATE
   - RISK
   - SOCIAL
   - SCORE
   - PAPER TRADE
5. Use the paper-trade controls only for testing your rules.

The dashboard does **not** say a coin is "safe", "guaranteed", or a buy. It reports the conditions and your predefined setup.

## X query

A useful starter query:

    (solana OR "pump.fun" OR memecoin OR memecoin OR $SOL) lang:en -is:retweet

You can change it in the UI.

## Important

The system cannot literally see "all of Twitter/X". It only receives the posts returned by the X API for the query, account access level, availability, rate limits and post-access rules. It is a research scanner, not omniscience.
