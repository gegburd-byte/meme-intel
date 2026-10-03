[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/gegburd-byte/meme-intel)

# Meme Intel — Solana research terminal

Meme Intel is a browser-based research + paper-trading terminal for Solana meme tokens.

## Current capabilities

- Automatic market-wide candidate discovery from DexScreener data.
- Automatic top-opportunity scanner with a strict security eligibility gate.
- Token-specific X search using contract address, symbol and token name.
- X velocity, acceleration, independent-author count, engagement, sentiment and copy/coordination risk.
- Real-time Pump.fun new-token event feed when the optional PumpPortal WebSocket is reachable.
- 1-minute live candle chart with volume.
- POC, VAH, VAL and 70% value area.
- VWAP, EMA 9/21, RSI, ATR, buy ratio, volume spike and short-term momentum.
- 5m -> 1m pump / pullback / higher-low / breakout state machine.
- Entry trigger, invalidation, T1, T2 and continuous exit state.
- Helius on-chain security checks for mint authority, freeze authority and holder concentration when HELIUS_API_KEY is configured.
- Automatic refresh without a page reload.
- Paper trading only. No wallet connection and no real orders.
- Transparent DATA NOT AVAILABLE / SECURITY UNKNOWN states instead of fabricated data.
- Automated regression tests and GitHub Actions configuration.

## Safety model

The scanner does not claim that a token is guaranteed safe or that any signal guarantees profit.

A token can only become the automatic top clean candidate when:
1. On-chain security checks are available.
2. The security gate passes.
3. Overall risk is not HIGH or CRITICAL.
4. The rule-based decision engine is not returning NO TRADE.

When those conditions are not met, the UI says there is no fully checked clean candidate rather than silently promoting a risky token.

## Configuration

Set these secrets in the deployment environment:

- X_BEARER_TOKEN
- HELIUS_API_KEY

Optional persistent storage settings are documented in .env.example.

## Run locally / Codespaces

The app starts with:

    uvicorn main:app --host 0.0.0.0 --port 3000

Forward port 3000 in GitHub Codespaces to get an `app.github.dev` URL.

## Public website

The repository also includes `render.yaml` for a public Render Web Service. Render can deploy this FastAPI app directly from GitHub with Uvicorn and provide an `onrender.com` URL. Add `X_BEARER_TOKEN` and `HELIUS_API_KEY` as deployment secrets for X and on-chain security data.

## Important provider note

The X and blockchain providers control access, quotas and endpoint availability. PumpPortal's real-time feed is an optional third-party data source used only for launch-event discovery; deeper analysis still goes through the Meme Intel backend.

No part of the application should be treated as a guarantee of future price movement or absence of fraud.