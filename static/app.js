const $ = (id) => document.getElementById(id);

let pumpSocket = null;
let pumpFunNativeTradeSocket = null;
let pumpFunNativeReconnectTimer = null;
let pumpFunNativeBackoff = 500;
let liveTradeSocket = null;
let fallbackTimer = null;
let reconnectTimer = null;
let pricePollTimer = null;
let currentCandleSyncTimer = null;
let currentCandleSyncInFlight = false;
let currentCandleSyncPromise = null;
let currentCandleSyncQueued = false;
let liveTradeCacheTimer = null;
let liveTradeCacheBusy = false;
let liveTradeCacheGeneration = 0;
let lastIndicatorCandleTime = 0;
let lastIndicatorState = "WAIT";
let selectedMinuteGeneration = 0;

let pumpEvents = [];
let marketCandidates = [];
let selectedMint = "";
let selectedInfo = {};
let chartHistoryLoadedAtSec = 0;
let selectedCandles = [];
let selectedMinuteCandles = [];
let selectedMinuteSource = "UNKNOWN";
let selectedTrades = [];
let selectedTradeKeys = new Set();
let selectedLiveUsdPrice = 0;
let selectedLiveUsdAt = 0;
let livePreviewActive = false;
let analysisBusy = false;
let autoOpenedCandidate = false;

let chart = null;
let candleSeries = null;
let volumeSeries = null;
let ema9Series = null;
let ema21Series = null;
let markersApi = null;
let chartInitialized = false;

let chartTimeframe = 1;
let chartInterval = "1m";
let chartDataSource = "PUMP.FUN";
let chartDisplayMode = "PRICE";
let selectedSupply = 0;
let selectedMarketCap = 0;
let selectedMarketCapUsd = 0;
let selectedMarketCapSol = 0;
let chartMcFactor = 0;
let historyGeneration = 0;
let historyBusy = false;
let historyBusyGeneration = 0;
let initialHistoryBusy = false;
let initialHistoryGeneration = 0;
let historyHasMore = true;
let historyNextOffset = 0;
let historyBarsLoaded = 0;

let renderScheduled = false;
let indicatorRenderScheduled = false;
let chartInitRetryTimer = null;
let chartResizeObserver = null;
let chartHistoryRetryTimer = null;
let chartHistoryRetryGeneration = 0;
let liveTradeBackoff = 500;
let liveTradeWatchdogTimer = null;
let lastLiveTradeAtMs = 0;
let lastRenderedCandleTime = 0;

const PAGE_SIZE = 30;
const MAX_HISTORY_BARS = 120;

function formatCompactNumber(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";

  const abs = Math.abs(n);

  if (abs >= 1e9) {
    return (n / 1e9).toFixed(2).replace(/\.00$/,"") + "B";
  }

  if (abs >= 1e6) {
    return (n / 1e6).toFixed(2).replace(/\.00$/,"") + "M";
  }

  if (abs >= 1e3) {
    return (n / 1e3).toFixed(2).replace(/\.00$/,"") + "K";
  }

  return n.toLocaleString(
    undefined,
    {maximumFractionDigits:2}
  );
}

function displayValue(price) {
  const p = Number(price);

  if (!Number.isFinite(p)) {
    return null;
  }

  if (
    chartDisplayMode === "MC" &&
    Number.isFinite(chartMcFactor) &&
    chartMcFactor > 0
  ) {
    // Candle prices are SOL/token. Pump.fun's exact current market cap
    // gives us the correct USD scale factor for this token.
    return p * chartMcFactor;
  }

  return p;
}

function formatChartValue(price) {
  const value = displayValue(price);

  if (value == null) {
    return "—";
  }

  if (chartDisplayMode === "MC") {
    return "$" + formatCompactNumber(value);
  }

  return safe(value,12);
}

function refreshMarketCapFactor(priceSol = null) {
  const p =
    Number(
      priceSol ??
      (
        selectedTrades.length
          ? selectedTrades[
              selectedTrades.length - 1
            ].price
          : NaN
      )
    );

  if (
    Number.isFinite(p) &&
    p > 0 &&
    Number.isFinite(selectedMarketCapUsd) &&
    selectedMarketCapUsd > 0
  ) {
    chartMcFactor =
      selectedMarketCapUsd / p;
  } else if (
    Number.isFinite(selectedMarketCapSol) &&
    selectedMarketCapSol > 0 &&
    Number.isFinite(selectedSupply) &&
    selectedSupply > 0 &&
    Number.isFinite(selectedMarketCapUsd) &&
    selectedMarketCapUsd > 0
  ) {
    // MC = SOL/token price × supply × SOL/USD.
    // Therefore the chart's raw SOL/token price needs this single scale
    // factor to become Pump.fun's USD market-cap scale.
    chartMcFactor =
      (
        selectedMarketCapUsd /
        selectedMarketCapSol
      ) * selectedSupply;
  }

  const button =
    $("chartMc");

  if (button) {
    button.disabled =
      !(chartMcFactor > 0);

    button.title =
      chartMcFactor > 0
        ? "Show Pump.fun market-cap scale"
        : "Waiting for market-cap data";
  }
}

async function loadChartMeta(mint) {
  const mintAtStart = mint;

  try {
    let data = null;

    try {
      const r = await fetch(
        "/api/chart/meta?mint=" +
        encodeURIComponent(mint) +
        "&t=" + Date.now(),
        {
          cache:"no-store"
        }
      );

      if (r.ok) {
        data =
          await readJsonResponse(r);
      }
    } catch {}

    // Fallback to the existing fast price endpoint. It includes supply and
    // market cap even when Pump.fun's coin endpoint is blocked.
    if (!data) {
      try {
        const r = await fetch(
          "/api/live/price?mint=" +
          encodeURIComponent(mint) +
          "&t=" + Date.now(),
          {
            cache:"no-store"
          }
        );

        if (r.ok) {
          data =
            await readJsonResponse(r);
        }
      } catch {}
    }

    if (
      !data ||
      mintAtStart !== selectedMint
    ) {
      return;
    }

    const supplyRaw =
      Number(
        data.total_supply ??
        data.supply
      );

    const supplyUi =
      Number.isFinite(
        Number(data.total_supply_ui)
      )
        ? Number(data.total_supply_ui)
        : (
            Number.isFinite(supplyRaw) &&
            supplyRaw > 1e9
              ? supplyRaw / 1e6
              : supplyRaw
          );

    const mcUsd =
      Number(
        data.market_cap_usd ??
        data.market_cap
      );

    const priceSol =
      Number(
        data.price_sol
      );

    if (
      Number.isFinite(supplyUi) &&
      supplyUi > 0
    ) {
      selectedSupply =
        supplyUi;
    }

    if (
      Number.isFinite(mcUsd) &&
      mcUsd > 0
    ) {
      selectedMarketCapUsd =
        mcUsd;

      selectedMarketCap =
        mcUsd;
    }

    const marketCapSol =
      Number(data.market_cap_sol);

    if (
      Number.isFinite(marketCapSol) &&
      marketCapSol > 0
    ) {
      selectedMarketCapSol = marketCapSol;
    }

    refreshMarketCapFactor(
      priceSol
    );

    // If Pump.fun metadata returned the exact current MC, keep that exact
    // current value visible even when the chart is still loading history.
    if (
      chartDisplayMode === "MC" &&
      selectedMarketCapUsd > 0
    ) {
      $("activePrice").textContent =
        "$" +
        formatCompactNumber(
          selectedMarketCapUsd
        );
    }

    if (
      chartDisplayMode === "MC" &&
      chartMcFactor > 0 &&
      chartInitialized
    ) {
      chart.applyOptions({
        localization:{
          priceFormatter:
            formatChartValue
        }
      });

      renderChart(
        selectedCandles,
        false
      );
    }
  } catch {
    $("chartMc").disabled =
      !(chartMcFactor > 0);
  }
}


function setChartDisplayMode(mode) {
  const next =
    mode === "MC"
      ? "MC"
      : "PRICE";

  if (
    next === "MC" &&
    !(chartMcFactor > 0)
  ) {
    return;
  }

  chartDisplayMode = next;

  $("chartPrice")?.classList.toggle(
    "active",
    chartDisplayMode === "PRICE"
  );

  $("chartMc")?.classList.toggle(
    "active",
    chartDisplayMode === "MC"
  );

  if (chart) {
    chart.applyOptions({
      localization:{
        priceFormatter:formatChartValue
      }
    });
  }

  $("activePriceLabel").textContent =
    chartDisplayMode === "MC"
      ? "ACTIVE MC"
      : "ACTIVE PRICE";

  if (
    selectedCandles.length &&
    chartInitialized
  ) {
    renderChart(
      selectedCandles,
      false
    );
  } else if (selectedMint) {
    $("activePrice").textContent =
      chartDisplayMode === "MC" &&
      selectedMarketCapUsd > 0
        ? "$" + formatCompactNumber(
            selectedMarketCapUsd
          )
        : "—";
  }
}


function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, Number(v) || 0));
}

function safe(v, digits = 6) {
  if (v == null || !Number.isFinite(Number(v))) return "—";
  return Number(v).toLocaleString(undefined, {maximumFractionDigits: digits});
}

function usd(v) {
  if (v == null || !Number.isFinite(Number(v))) return "—";
  return "$" + Number(v).toLocaleString(undefined, {maximumFractionDigits: 2});
}

function pct(v, digits = 1) {
  if (v == null || !Number.isFinite(Number(v))) return "—";
  return Number(v).toFixed(digits) + "%";
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, function(c) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"}[c];
  });
}


async function readJsonResponse(response) {
  const text = await response.text();
  let data = null;

  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    throw new Error(
      "Backend returned a non-JSON error (" +
      response.status +
      ")."
    );
  }

  if (!response.ok) {
    throw new Error(
      data?.detail ||
      data?.error ||
      data?.message ||
      ("Request failed (" + response.status + ").")
    );
  }

  return data || {};
}

function cleanSymbol(v) {
  return String(v || "TOKEN").replace(/^\$+/, "").slice(0, 24);
}

function timeframeLabel(tf = null) {
  if (tf == null) return chartInterval;
  if (typeof tf === "string") return tf;
  return tf === 1 ? "1m" : tf === 5 ? "5m" : tf === 15 ? "15m" : "1h";
}

function timeframeSeconds() {
  if (chartInterval === "1s") return 1;
  return Math.max(60, Number(chartTimeframe || 1) * 60);
}

function backendTimeframe() {
  return chartInterval === "1s"
    ? 1
    : Number(chartTimeframe || 1);
}

function setSource(dotId, textId, state, goodStates) {
  const dot = $(dotId);
  const label = $(textId);
  if (!dot || !label) return;
  dot.className = "dot " + (goodStates.includes(state) ? "ok" : "bad");
  label.textContent = state || "UNKNOWN";
}

async function health() {
  try {
    const r = await fetch("/api/health?t=" + Date.now(), {cache:"no-store"});
    const j = await r.json();

    $("health").textContent = j.status || "ONLINE";

    setSource("dotPump", "pumpState", "LIVE", ["LIVE"]);

    const liveState =
      j.sources?.LiveTrade?.state ||
      (liveTradeSocket?.readyState === WebSocket.OPEN ? "LIVE" : "READY");

    setSource(
      "dotChart",
      "chartState",
      liveState,
      ["LIVE","CONNECTING","READY"]
    );

    setSource(
      "dotHelius",
      "securityState",
      j.sources?.Security?.state || "UNKNOWN",
      ["READY"]
    );

    setSource(
      "dotRug",
      "rugState",
      j.sources?.RugCheck?.state || "UNKNOWN",
      ["READY"]
    );

    setSource(
      "dotX",
      "xState",
      j.sources?.X?.state || "UNKNOWN",
      ["READY","CONFIGURED"]
    );
  } catch {
    $("health").textContent = "BACKEND ERROR";
  }
}

function ema(values, period) {
  if (values.length < period) return null;

  let out = values.slice(0, period).reduce((a,b)=>a+b,0) / period;
  const alpha = 2 / (period + 1);

  for (let i = period; i < values.length; i++) {
    out = (values[i] - out) * alpha + out;
  }

  return out;
}

function rsi(values, period = 14) {
  if (values.length < period + 1) return null;

  let gain = 0;
  let loss = 0;

  for (let i = 1; i <= period; i++) {
    const d = values[i] - values[i - 1];
    gain += Math.max(0,d);
    loss += Math.max(0,-d);
  }

  let avgGain = gain / period;
  let avgLoss = loss / period;

  for (let i = period + 1; i < values.length; i++) {
    const d = values[i] - values[i - 1];

    avgGain = ((avgGain * (period - 1)) + Math.max(0,d)) / period;
    avgLoss = ((avgLoss * (period - 1)) + Math.max(0,-d)) / period;
  }

  if (avgLoss === 0) return 100;

  return 100 - (100 / (1 + avgGain / avgLoss));
}

function atr(candles, period = 14) {
  if (!candles || candles.length < period + 1) {
    return null;
  }

  const tr = [];

  for (let i=1;i<candles.length;i++) {
    const current = candles[i];
    const previous = candles[i - 1];

    const h = Number(current.h);
    const l = Number(current.l);
    const pc = Number(previous.c);

    if (
      !Number.isFinite(h) ||
      !Number.isFinite(l) ||
      !Number.isFinite(pc)
    ) {
      continue;
    }

    tr.push(
      Math.max(
        h - l,
        Math.abs(h - pc),
        Math.abs(l - pc)
      )
    );
  }

  if (tr.length < period) {
    return null;
  }

  return (
    tr
      .slice(-period)
      .reduce((a,b)=>a+b,0) /
    period
  );
}

function vwap(candles, lookback = 30) {
  const rows =
    candles.slice(
      -Math.max(1,lookback)
    );

  let pv = 0;
  let volume = 0;
  let closeSum = 0;
  let closeCount = 0;

  for (const candle of rows) {
    const high = Number(candle.h);
    const low = Number(candle.l);
    const close = Number(candle.c);
    const vol = Math.max(
      0,
      Number(candle.v || 0)
    );

    if (
      !Number.isFinite(high) ||
      !Number.isFinite(low) ||
      !Number.isFinite(close)
    ) {
      continue;
    }

    const typical =
      (high + low + close) / 3;

    if (vol > 0) {
      pv += typical * vol;
      volume += vol;
    }

    closeSum += close;
    closeCount++;
  }

  if (volume > 0) {
    return pv / volume;
  }

  return closeCount
    ? closeSum / closeCount
    : null;
}

function liveTradePressure() {
  const rows =
    selectedTrades.slice(-120);

  let buy = 0;
  let sell = 0;

  for (const trade of rows) {
    const volume = Math.max(
      0,
      Number(
        trade.chartVolume ??
        trade.volumeSol ??
        trade.volume_sol ??
        0
      )
    );

    if (
      String(trade.side || "")
        .toUpperCase() === "SELL"
    ) {
      sell += volume || 1;
    } else {
      buy += volume || 1;
    }
  }

  const total =
    buy + sell;

  return total > 0
    ? buy / total
    : null;
}

function currentIndicatorCandles(fallback) {
  if (
    selectedMinuteCandles.length >= 21
  ) {
    return selectedMinuteCandles;
  }

  if (
    chartInterval === "1s" &&
    selectedTrades.length
  ) {
    const liveMinutes =
      aggregateLiveTrades(
        selectedTrades,
        "1m"
      );

    if (liveMinutes.length) {
      return liveMinutes;
    }
  }

  return fallback;
}

function signalFromCandles(
  candles,
  endIndex = candles.length - 1
) {
  if (
    !candles.length ||
    endIndex < 0
  ) {
    return {
      state:"WAIT",
      reason:"Waiting for live Pump.fun trades…",
      rsi:null,
      ema9:null,
      ema21:null,
      mean:null,
      pressure:null,
      score:0,
      buyTrigger:null,
      sellTrigger:null,
      slowReady:false,
      slowTrend:"UNKNOWN",
      atr:null,
      vwap:null
    };
  }

  const isCurrent =
    endIndex === candles.length - 1;

  let visible =
    isCurrent
      ? currentIndicatorCandles(candles)
      : candles.slice(
          0,
          endIndex + 1
        );

  visible = visible.filter(
    x =>
      Number.isFinite(
        Number(x.c)
      )
  );

  const closes =
    visible.map(
      x => Number(x.c)
    );

  if (closes.length < 21) {
    return {
      state:"WAIT",
      reason:"Building 21 real 1m candles for confirmation…",
      rsi:null,
      ema9:null,
      ema21:null,
      mean:null,
      pressure:null,
      score:0,
      buyTrigger:null,
      sellTrigger:null,
      slowReady:false,
      slowTrend:"UNKNOWN",
      atr:null,
      vwap:null
    };
  }

  const p =
    closes[closes.length - 1];

  const e9 =
    ema(closes,9);

  const e21 =
    ema(closes,21);

  const r =
    rsi(closes,14);

  const atrValue =
    atr(visible,14);

  const vwapValue =
    vwap(visible,30);

  const recent =
    closes.slice(-12);

  let up = 0;
  let down = 0;

  for (
    let i = 1;
    i < recent.length;
    i++
  ) {
    if (
      recent[i] >
      recent[i - 1]
    ) {
      up++;
    } else if (
      recent[i] <
      recent[i - 1]
    ) {
      down++;
    }
  }

  let pressure =
    up + down
      ? up / (up + down)
      : 0.5;

  if (isCurrent) {
    const livePressure =
      liveTradePressure();

    if (
      Number.isFinite(livePressure)
    ) {
      pressure =
        livePressure * 0.70 +
        pressure * 0.30;
    }
  }

  const c5 =
    aggregateCandles(
      visible,
      5
    );

  let e5_9 = null;
  let e5_21 = null;
  let slowTrend = "UNKNOWN";

  if (c5.length >= 9) {
    const c5Closes =
      c5.map(
        x => Number(x.c)
      );

    e5_9 =
      ema(c5Closes,9);

    if (c5.length >= 21) {
      e5_21 =
        ema(c5Closes,21);
    }

    if (e5_21 != null) {
      slowTrend =
        e5_9 > e5_21
          ? "BULLISH"
          : e5_9 < e5_21
            ? "BEARISH"
            : "NEUTRAL";
    } else if (e5_9 != null) {
      slowTrend =
        c5[c5.length - 1].c > e5_9
          ? "BULLISH"
          : "BEARISH";
    }
  }

  const recentStructure =
    closes.slice(
      Math.max(
        0,
        closes.length - 8
      ),
      closes.length - 1
    );

  const priorHigh =
    recentStructure.length
      ? Math.max(...recentStructure)
      : p;

  const priorLow =
    recentStructure.length
      ? Math.min(...recentStructure)
      : p;

  const momentum =
    closes.length >= 5
      ? (
          p /
          closes[
            closes.length - 5
          ] -
          1
        ) * 100
      : 0;

  const avgVolumeRows =
    visible.slice(-21);

  const avgVolume =
    avgVolumeRows.length
      ? (
          avgVolumeRows.reduce(
            (sum,x)=>
              sum +
              Math.max(
                0,
                Number(x.v || 0)
              ),
            0
          ) /
          avgVolumeRows.length
        )
      : 0;

  const currentVolume =
    Math.max(
      0,
      Number(
        visible[
          visible.length - 1
        ].v || 0
      )
    );

  const volumeRatio =
    avgVolume > 0
      ? currentVolume / avgVolume
      : 1;

  const volatility =
    atrValue != null &&
    p > 0
      ? atrValue / p
      : 0;

  const breakoutBuffer =
    Math.max(
      p * 0.0015,
      Number(
        atrValue || 0
      ) * 0.20
    );

  const buyTrigger =
    priorHigh +
    breakoutBuffer;

  const sellTrigger =
    e21 != null
      ? Math.max(
          priorLow,
          e21 -
            Number(
              atrValue || 0
            ) * 0.25
        )
      : priorLow;

  let score = 50;

  if (
    e9 != null &&
    e21 != null
  ) {
    score +=
      e9 > e21
        ? 18
        : -18;
  }

  if (vwapValue != null) {
    score +=
      p >= vwapValue
        ? 10
        : -10;
  }

  if (r != null) {
    if (
      r >= 48 &&
      r <= 68
    ) {
      score += 10;
    } else if (
      r >= 68 &&
      r <= 78
    ) {
      score += 3;
    } else if (
      r < 40
    ) {
      score -= 10;
    } else if (
      r > 82
    ) {
      score -= 10;
    }
  }

  if (pressure >= 0.58) {
    score += 10;
  } else if (
    pressure <= 0.42
  ) {
    score -= 10;
  }

  if (momentum > 0.5) {
    score += 8;
  } else if (
    momentum < -0.5
  ) {
    score -= 8;
  }

  if (volumeRatio >= 1.35) {
    score += 7;
  } else if (
    volumeRatio < 0.65
  ) {
    score -= 4;
  }

  if (
    slowTrend === "BULLISH"
  ) {
    score += 15;
  } else if (
    slowTrend === "BEARISH"
  ) {
    score -= 15;
  }

  if (
    volatility > 0.12
  ) {
    score -= 8;
  }

  score =
    Math.round(
      clamp(score,0,100)
    );

  const slowReady =
    c5.length >= 21;

  const buyReady =
    slowReady &&
    e9 != null &&
    e21 != null &&
    e9 > e21 &&
    r != null &&
    r >= 48 &&
    r <= 68 &&
    vwapValue != null &&
    p > vwapValue &&
    pressure >= 0.56 &&
    volumeRatio >= 0.85 &&
    slowTrend === "BULLISH" &&
    p >= buyTrigger;

  const sellReady =
    slowReady &&
    e9 != null &&
    e21 != null &&
    p <= sellTrigger &&
    (
      slowTrend === "BEARISH" ||
      e9 < e21
    );

  let state = "WAIT";

  if (buyReady) {
    state = "BUY";
  } else if (sellReady) {
    state = "SELL";
  }

  let reason =
    slowReady
      ? "WAIT — trend, momentum, flow and breakout confirmation are not fully aligned."
      : "WAIT — building 5m trend history.";

  if (state === "BUY") {
    reason =
      "BUY — 1m trend + 5m trend + VWAP + RSI + flow + breakout agree.";
  } else if (state === "SELL") {
    reason =
      "SELL / EXIT — structure or EMA support has failed with bearish confirmation.";
  } else if (slowReady) {
    reason =
      "WAIT — breakout above " +
      formatChartValue(buyTrigger) +
      " for entry; defend below " +
      formatChartValue(sellTrigger) +
      ".";
  }

  return {
    state,
    reason,
    rsi:r,
    ema9:e9,
    ema21:e21,
    mean:vwapValue,
    pressure,
    momentum,
    score,
    buyTrigger,
    sellTrigger,
    slowReady,
    slowTrend,
    ema5_9:e5_9,
    ema5_21:e5_21,
    atr:atrValue,
    vwap:vwapValue
  };
}


function launchScore(e) {
  const age = Math.max(0, (Date.now() - e.ts) / 1000);
  const freshness = clamp(25 - age / 12, 0, 25);

  const progress = Number(e.vSolInBondingCurve || 0);
  const progressScore = progress > 0
    ? clamp(30 - Math.abs(progress - 46) * 0.55, 0, 30)
    : 10;

  const mcap = Number(e.marketCapSol || 0);
  const mcapScore = clamp(Math.log10(Math.max(1,mcap)) * 8, 0, 25);

  const initialBuy = Number(e.initialBuy || 0);
  const buyScore = initialBuy > 0
    ? clamp(Math.log10(initialBuy + 1) * 3, 0, 15)
    : 5;

  return Math.round(
    clamp(
      freshness + progressScore + mcapScore + buyScore,
      0,
      100
    )
  );
}

function normalizePumpEvent(raw) {
  const mint = raw.mint || raw.tokenAddress;
  if (!mint) return null;

  return {
    mint,
    symbol:cleanSymbol(raw.symbol || raw.tokenSymbol || "NEW"),
    name:raw.name || raw.tokenName || "",
    creator:raw.traderPublicKey || raw.creator || "",
    marketCapSol:Number(raw.marketCapSol || 0),
    vSolInBondingCurve:Number(raw.vSolInBondingCurve || 0),
    initialBuy:Number(raw.initialBuy || 0),
    ts:Date.now(),
    source:"PUMP.FUN"
  };
}

function mergedCandidates() {
  const map = new Map();

  for (const e of pumpEvents) {
    map.set(e.mint,{
      ...e,
      symbol:cleanSymbol(e.symbol),
      score:launchScore(e)
    });
  }

  for (const x of marketCandidates) {
    const row = {
      mint:x.address,
      symbol:cleanSymbol(x.symbol || "TOKEN"),
      name:x.name || "",
      score:Math.round(Number(x.researchScore || 0)),
      source:x.pumpLane ? "PUMPSWAP" : "MARKET",
      price:Number(x.priceUsd || 0),
      liquidity:Number(x.liquidityUsd || 0),
      change5m:Number(x.priceChange5m || 0),
      age:x.pairCreatedAt ? Date.now() - Number(x.pairCreatedAt) : null
    };

    const old = map.get(row.mint);

    if (!old || row.score > old.score) {
      map.set(row.mint,row);
    }
  }

  return [...map.values()]
    .sort((a,b)=>b.score-a.score)
    .slice(0,8);
}

function renderCandidates() {
  const rows = mergedCandidates();
  const el = $("candidateList");

  if (!rows.length) {
    el.innerHTML =
      '<div class="empty">Waiting for live Pump.fun launches…</div>';
    return;
  }

  el.innerHTML = rows.map((x,i)=>{
    const progress = x.vSolInBondingCurve
      ? clamp((x.vSolInBondingCurve / 85) * 100,0,100)
      : null;

    const stat = x.marketCapSol
      ? "MC " + safe(x.marketCapSol,1) + " SOL"
      : x.liquidity
        ? "Liq " + usd(x.liquidity)
        : "LIVE";

    return (
      '<button class="candidate ' +
      (x.mint === selectedMint ? "selected" : "") +
      '" data-mint="' + esc(x.mint) + '">' +
        '<div class="rank">#' + (i+1) + '</div>' +
        '<div class="candidateMain">' +
          '<b>$' + esc(cleanSymbol(x.symbol)) + '</b>' +
          '<span>' + esc(x.mint) + '</span>' +
        '</div>' +
        '<div class="candidateStats">' +
          '<b>' + x.score + '</b>' +
          '<span>' + esc(stat) + '</span>' +
        '</div>' +
        '<div class="candidateTag">' +
          esc(x.source || "PUMP.FUN") +
        '</div>' +
        (
          progress != null
            ? '<div class="progress"><i style="width:' +
                progress + '%"></i></div>'
            : ''
        ) +
      '</button>'
    );
  }).join("");

  el.querySelectorAll("[data-mint]").forEach(btn=>{
    btn.addEventListener("click",()=>{
      selectToken(btn.getAttribute("data-mint"));
    });
  });

  // Bootstrap the chart from the best currently discovered token. This avoids
  // presenting an empty chart on first load and also gives us an immediate
  // real-data path to verify the chart backend. Manual CA entry still works
  // normally and takes over as soon as the user opens another token.
  if (!selectedMint && !autoOpenedCandidate && rows[0]?.mint) {
    autoOpenedCandidate = true;
    queueMicrotask(() => {
      if (!selectedMint && rows[0]?.mint) {
        selectToken(rows[0].mint).catch(() => {});
      }
    });
  }
}

function initChart() {
  if (chartInitialized && chart) return true;

  const container = $("chart");

  if (!container) {
    return false;
  }

  if (!window.LightweightCharts) {
    $("chartMode").textContent = "LOADING CHART LIBRARY…";

    if (!chartInitRetryTimer) {
      chartInitRetryTimer = setTimeout(() => {
        chartInitRetryTimer = null;
        initChart();
      }, 250);
    }

    return false;
  }

  try {
    const width = Math.max(
      320,
      Number(container.clientWidth) ||
      Number(container.parentElement?.clientWidth) ||
      800
    );

    const height = Math.max(
      330,
      Number(container.clientHeight) || 500
    );

    chart = LightweightCharts.createChart(container,{
      width,
      height,
      autoSize:false,

      layout:{
        background:{type:"solid",color:"#071019"},
        textColor:"#7f90a5",
        fontFamily:"Inter,system-ui,-apple-system,Segoe UI,sans-serif"
      },

      grid:{
        vertLines:{color:"#12212d"},
        horzLines:{color:"#12212d"}
      },

      crosshair:{
        mode:LightweightCharts.CrosshairMode?.Normal ?? 0
      },

      rightPriceScale:{
        borderColor:"#203140",
        scaleMargins:{top:0.06,bottom:0.18},
        autoScale:true,
        alignLabels:true,
        // Normal mode is the most stable/portable default for tiny Pump.fun
        // SOL-per-token values. Avoid relying on an optional enum export.
        mode:0
      },

      timeScale:{
        borderColor:"#203140",
        timeVisible:true,
        secondsVisible:false,
        rightOffset:4,
        barSpacing:7,
        minBarSpacing:1,
        maxBarSpacing:40,
        lockVisibleTimeRangeOnResize:true,
        shiftVisibleRangeOnNewBar:true
      },

      handleScale:{
        mouseWheel:true,
        pinch:true,
        axisPressedMouseMove:true
      },

      handleScroll:{
        mouseWheel:true,
        pressedMouseMove:true,
        horzTouchDrag:true,
        vertTouchDrag:true
      },

      localization:{
        priceFormatter:formatChartValue
      }
    });

    candleSeries = chart.addSeries(
      LightweightCharts.CandlestickSeries,
      {
        upColor:"#39dc89",
        downColor:"#ff6575",
        borderUpColor:"#39dc89",
        borderDownColor:"#ff6575",
        wickUpColor:"#39dc89",
        wickDownColor:"#ff6575",
        priceLineVisible:true,
        lastValueVisible:true
      }
    );

    ema9Series = chart.addSeries(
      LightweightCharts.LineSeries,
      {
        color:"#6ee7a5",
        lineWidth:1,
        crosshairMarkerVisible:false,
        lastValueVisible:false,
        priceLineVisible:false
      }
    );

    ema21Series = chart.addSeries(
      LightweightCharts.LineSeries,
      {
        color:"#ffbd54",
        lineWidth:1,
        crosshairMarkerVisible:false,
        lastValueVisible:false,
        priceLineVisible:false
      }
    );

    volumeSeries = chart.addSeries(
      LightweightCharts.HistogramSeries,
      {
        priceFormat:{type:"volume"},
        priceScaleId:"volume"
      }
    );

    chart.priceScale("volume").applyOptions({
      scaleMargins:{top:0.82,bottom:0},
      borderVisible:false,
      visible:false
    });

    markersApi = LightweightCharts.createSeriesMarkers(
      candleSeries,
      []
    );

    chartInitialized = true;

    // Keep the explicit-height chart reliable on slow/older browsers without
    // paying for a ResizeObserver-driven autoSize pass on every layout tick.
    if (chartResizeObserver) {
      chartResizeObserver.disconnect();
      chartResizeObserver = null;
    }

    if (window.ResizeObserver) {
      chartResizeObserver = new ResizeObserver(() => {
        if (!chart || !chartInitialized) return;

        const w = Math.max(
          320,
          Number(container.clientWidth) ||
          Number(container.parentElement?.clientWidth) ||
          800
        );
        const h = Math.max(
          330,
          Number(container.clientHeight) || 500
        );

        chart.resize(w,h);
      });

      chartResizeObserver.observe(container);
    }

    return true;
  } catch (error) {
    chartInitialized = false;
    chart = null;
    candleSeries = null;
    volumeSeries = null;
    ema9Series = null;
    ema21Series = null;
    markersApi = null;

    $("chartMode").textContent =
      "CHART RETRYING…";

    if (!chartInitRetryTimer) {
      chartInitRetryTimer = setTimeout(() => {
        chartInitRetryTimer = null;
        initChart();
      }, 400);
    }

    return false;
  }
}

function normalizeCandle(x) {
  if (!x) return null;

  const ts = Number(x.ts ?? x.timestamp ?? x.time);
  const o = Number(x.o ?? x.open);
  const h = Number(x.h ?? x.high);
  const l = Number(x.l ?? x.low);
  const c = Number(x.c ?? x.close);
  const v = Number(x.v ?? x.volume ?? 0);

  if (
    !Number.isFinite(ts) ||
    ![o,h,l,c].every(Number.isFinite) ||
    ![o,h,l,c].every(value => value > 0)
  ) {
    return null;
  }

  // Reject raw atomic/unit-mismatched prices before they can blow up the
  // Lightweight Charts price scale.
  if ([o,h,l,c].some(value => value > 1_000_000)) {
    return null;
  }

  if (
    l > Math.min(o,c) ||
    h < Math.max(o,c) ||
    h < l
  ) {
    return null;
  }

  const time = Math.floor(
    ts > 2e10 ? ts / 1000 : ts
  );

  if (time < 1_500_000_000) return null;

  return {
    time,
    ts:time,
    o,
    h,
    l,
    c,
    v:Math.max(0,v)
  };
}

function sanitizeChartCandles(candles) {
  const byTime = new Map();

  for (const raw of candles || []) {
    const c = normalizeCandle(raw);
    if (!c) continue;
    byTime.set(c.time,c);
  }

  return [...byTime.values()]
    .sort((a,b)=>a.time-b.time)
    .slice(-MAX_HISTORY_BARS);
}

function findNativePumpFunTradePayload(value, depth = 0) {
  if (depth > 7 || value == null) return null;

  if (typeof value === "string") {
    const text = value.trim();
    if (!text || !/^[\[{]/.test(text)) return null;
    try {
      return findNativePumpFunTradePayload(JSON.parse(text), depth + 1);
    } catch {
      return null;
    }
  }

  if (Array.isArray(value)) {
    for (const child of value) {
      const found = findNativePumpFunTradePayload(child, depth + 1);
      if (found) return found;
    }
    return null;
  }

  if (typeof value !== "object") return null;

  const mint = String(value.mint || "").trim();
  const hasSol = value.sol_amount != null || value.solAmount != null;
  const hasToken = value.token_amount != null || value.tokenAmount != null;
  if (mint && hasSol && hasToken) return value;

  for (const key of ["payload", "trade", "result", "subscribe", "data", "event"]) {
    if (!(key in value)) continue;
    const found = findNativePumpFunTradePayload(value[key], depth + 1);
    if (found) return found;
  }

  for (const child of Object.values(value)) {
    if (typeof child === "object" || typeof child === "string") {
      const found = findNativePumpFunTradePayload(child, depth + 1);
      if (found) return found;
    }
  }
  return null;
}

function parseNativePumpFunTrade(raw) {
  const payload = findNativePumpFunTradePayload(raw);
  if (!payload) return null;

  const mint = String(payload.mint || "").trim();
  const solAmount = Number(payload.sol_amount ?? payload.solAmount);
  const tokenAmount = Number(payload.token_amount ?? payload.tokenAmount);
  const timestampRaw = Number(payload.timestamp);

  if (!mint || !Number.isFinite(solAmount) || solAmount <= 0 || !Number.isFinite(tokenAmount) || tokenAmount <= 0 || !Number.isFinite(timestampRaw)) {
    return null;
  }

  const timestamp = Math.floor(timestampRaw > 2e10 ? timestampRaw / 1000 : timestampRaw);
  if (timestamp < 1500000000) return null;

  const price = (solAmount / 1_000_000_000) / (tokenAmount / 1_000_000);
  if (!Number.isFinite(price) || price <= 0) return null;

  return {
    id:String(payload.signature || ("pumpfun-native:" + mint + ":" + timestamp + ":" + String(payload.slot || "") + ":" + String(payload.tx_index ?? payload.txIndex ?? ""))),
    signature:String(payload.signature || ""),
    timestamp,
    time:timestamp,
    price,
    side:((payload.is_buy ?? payload.isBuy) === true || String(payload.side ?? payload.txType ?? "").toUpperCase() === "BUY") ? "BUY" : "SELL",
    volumeSol:solAmount / 1_000_000_000,
    volume_sol:solAmount / 1_000_000_000,
    source:"PUMP.FUN",
    nativePumpFun:true
  };
}

function normalizeTrade(raw) {
  if (!raw) return null;

  const price = Number(raw.price);
  const ts = Number(raw.timestamp);

  if (
    !Number.isFinite(price) ||
    price <= 0 ||
    !Number.isFinite(ts)
  ) return null;

  return {
    id:String(
      raw.id ||
      raw.signature ||
      (Date.now() + ":" + Math.random())
    ),
    time:Math.floor(
      ts > 2e10 ? ts / 1000 : ts
    ),
    price,
    side:String(raw.side || "BUY").toUpperCase() === "SELL"
      ? "SELL"
      : "BUY",
    volumeSol:Number(raw.volume_sol || 0),
    source:raw.source || "ONCHAIN",
    signature:raw.signature || ""
  };
}

function normalizeTradeForChart(trade) {
  // Chart history and live Pump.fun trades intentionally use the same
  // SOL/token unit. Never infer a USD conversion from an unrelated venue's
  // last candle; that was a source of wrong live prices when a fallback chart
  // was previously GeckoTerminal-backed.
  return {
    ...trade,
    price:Number(trade.price),
    volume_sol:Number(trade.volume_sol || 0),
  };
}

function aggregateCandles(source, tfMinutes) {
  const span =
    tfMinutes === "1s"
      ? 1
      : Math.max(60, Number(tfMinutes || 1) * 60);

  if (span === 60) {
    return source.map(x=>({...x}));
  }
  const buckets = new Map();

  for (const c of source) {
    const bucket = Math.floor(c.time / span) * span;

    let row = buckets.get(bucket);

    if (!row) {
      row = {
        time:bucket,
        ts:bucket,
        o:c.o,
        h:c.h,
        l:c.l,
        c:c.c,
        v:Number(c.v || 0)
      };

      buckets.set(bucket,row);
      continue;
    }

    row.h = Math.max(row.h,c.h);
    row.l = Math.min(row.l,c.l);
    row.c = c.c;
    row.v += Number(c.v || 0);
  }

  return [...buckets.values()]
    .sort((a,b)=>a.time-b.time);
}

function indicatorSeries(candles) {
  const closes = [];
  const e9 = [];
  const e21 = [];

  for (let i=0;i<candles.length;i++) {
    closes.push(candles[i].c);
    e9.push(ema(closes,9));
    e21.push(ema(closes,21));
  }

  return {
    ema9:e9
      .map((v,i)=>v == null
        ? null
        : {time:candles[i].time,value:v}
      )
      .filter(Boolean),

    ema21:e21
      .map((v,i)=>v == null
        ? null
        : {time:candles[i].time,value:v}
      )
      .filter(Boolean)
  };
}

function buildMarkers(candles) {
  const markers = [];
  let previous = "WAIT";

  // Only calculate the visible tail. This keeps huge histories fast.
  const start = Math.max(0,candles.length - 1500);
  const tail = candles.slice(start);

  for (let i=0;i<tail.length;i++) {
    const realIndex = start + i;
    const signal = signalFromCandles(
      candles,
      realIndex
    );

    if (
      signal.state !== "WAIT" &&
      signal.state !== previous
    ) {
      markers.push({
        time:tail[i].time,
        position:signal.state === "BUY"
          ? "belowBar"
          : "aboveBar",
        color:signal.state === "BUY"
          ? "#39dc89"
          : "#ff6575",
        shape:signal.state === "BUY"
          ? "arrowUp"
          : "arrowDown",
        text:signal.state,
        id:signal.state + "-" + tail[i].time
      });
    }

    previous = signal.state;
  }

  return markers.slice(-40);
}

function updateIndicatorPanel(signal) {
  $("liveRsi").textContent =
    signal.rsi == null
      ? "—"
      : Number(signal.rsi).toFixed(1);

  $("liveEma").textContent =
    safe(signal.ema9,12) + " / " +
    safe(signal.ema21,12);

  $("metricRsi").textContent =
    signal.rsi == null
      ? "—"
      : Number(signal.rsi).toFixed(1);

  $("metricEma9").textContent =
    safe(signal.ema9,12);

  $("metricEma21").textContent =
    safe(signal.ema21,12);

  $("metricMean").textContent =
    signal.vwap == null
      ? "—"
      : formatChartValue(signal.vwap) +
        " / " +
        safe(signal.atr,12);

  $("metricPressure").textContent =
    signal.pressure == null
      ? "—"
      : pct(signal.pressure * 100,0);

  $("metricConfirm").textContent =
    signal.score + "/100";
  
  if ($("metricConfirm")) {
    $("metricConfirm").title =
      signal.slowReady
        ? "5m trend: " + (signal.slowTrend || "UNKNOWN")
        : "Waiting for 5m trend history";
  }


  $("buyTrigger").textContent =
    signal.buyTrigger == null
      ? "—"
      : formatChartValue(signal.buyTrigger);

  $("sellTrigger").textContent =
    signal.sellTrigger == null
      ? "—"
      : formatChartValue(signal.sellTrigger);
}

function renderSignal(signal) {
  const state = signal.state || "WAIT";

  $("signalBadge").textContent = state;
  $("signalBadge").className =
    "signal " + state.toLowerCase();

  $("signalText").textContent = state;
  $("signalText").className =
    "signalText " + state.toLowerCase();

  $("signalReason").textContent =
    signal.reason || "Waiting for more data.";

  updateIndicatorPanel(signal);
}

function updateActivePrice(price, ts = Date.now()) {
  $("activePrice").textContent = formatChartValue(price);
  $("livePrice").textContent = safe(price,12);

  const age = Math.max(
    0,
    Math.round(Date.now() - Number(ts || Date.now()))
  );

  $("activeAge").textContent = age < 10000
    ? "LIVE · " + age + " ms"
    : "LIVE";
}

function renderTape() {
  const rows = selectedTrades.slice(-14).reverse();

  if (!rows.length) {
    const candles = selectedCandles.slice(-10).reverse();

    if (!candles.length) {
      $("tape").innerHTML =
        '<div class="empty">No live trades yet.</div>';
      return;
    }

    $("tape").innerHTML = candles.map((x,i)=>{
      const prev = candles[i+1];
      const up = !prev || x.c >= prev.c;

      return (
        '<div class="tick ' +
        (up ? "up" : "down") +
        '">' +
          '<span>' +
            new Date(x.time * 1000).toLocaleTimeString() +
          '</span>' +
          '<b>' + safe(x.c,12) + '</b>' +
          '<i>' + (up ? "▲" : "▼") + '</i>' +
        '</div>'
      );
    }).join("");

    return;
  }

  $("tape").innerHTML = rows.map(x=>(
    '<div class="tick ' +
      (x.side === "BUY" ? "up" : "down") +
    '">' +
      '<span>' +
        new Date(x.time * 1000).toLocaleTimeString() +
      '</span>' +
      '<b>' + safe(
        Number(x.chartPrice ?? x.price),
        12
      ) + '</b>' +
      '<i>' + (
        x.side === "BUY" ? "BUY" : "SELL"
      ) + '</i>' +
    '</div>'
  )).join("");

  $("lastUpdate").textContent =
    new Date(rows[0].time * 1000).toLocaleTimeString();
}

function renderSafety(data) {
  const p = data?.safety_profile || {};

  const safety = Number(p.safety_percent);
  const risk = Number(p.rug_risk_percent);
  const confidence = Number(p.confidence_percent);

  $("safetyPercent").textContent =
    Number.isFinite(safety) ? Math.round(safety) : "—";

  $("rugRiskPercent").textContent =
    Number.isFinite(risk) ? Math.round(risk) : "—";

  $("riskConfidence").textContent =
    Number.isFinite(confidence) ? Math.round(confidence) : "—";

  const status = p.status || "UNKNOWN";
  const statusClass =
    status === "LOW RUG RISK"
      ? "safe"
      : status === "GUARDED"
        ? "guarded"
        : status.includes("HIGH")
          ? "risk"
          : "unknown";

  $("safetyStatus").textContent = status;
  $("safetyStatus").className = "safetyStatus " + statusClass;
  $("safetyDetail").textContent =
    p.status_detail ||
    "Risk engine is waiting for more evidence.";

  const checks = Array.isArray(p.checks)
    ? p.checks.slice(0,10)
    : [];

  if (!checks.length) {
    $("safetyChecks").innerHTML =
      '<div class="empty">No detailed safety checks were returned yet.</div>';
    return;
  }

  $("safetyChecks").innerHTML = checks.map(c=>{
    const state = ["safe","warn","danger"].includes(c.status)
      ? c.status
      : "unknown";

    return (
      '<div class="safetyCheck">' +
        '<i class="safetyDot '+state+'"></i>' +
        '<div>' +
          '<b>'+esc(c.name || "Check")+'</b>' +
          '<span>'+esc(c.detail || "No detail")+'</span>' +
        '</div>' +
      '</div>'
    );
  }).join("");
}

function renderSecurity(data) {
  const gate = data?.security_gate || {};

  $("securityBadge").textContent =
    gate.label || "UNKNOWN";

  $("securityBadge").className =
    "miniBadge " +
    String(gate.label || "UNKNOWN")
      .toLowerCase()
      .replace(/[^a-z]+/g,"-");

  const s = data?.security;

  if (!s || typeof s !== "object") {
    $("securityRows").innerHTML =
      '<div class="empty">Security data unavailable.</div>';
    return;
  }

  $("securityRows").innerHTML =
    '<div class="row"><span>Holder coverage</span><b>' +
      pct(Number(s.coverage_ratio || 0) * 100,0) +
    '</b></div>' +

    '<div class="row"><span>Top holder</span><b>' +
      pct(Number(s.top_holder_share || 0) * 100,1) +
    '</b></div>' +

    '<div class="row"><span>Top 10</span><b>' +
      pct(Number(s.top10_holder_share || 0) * 100,1) +
    '</b></div>' +

    '<div class="row"><span>Mint authority</span><b>' +
      (s.mint_authority ? "ACTIVE" : "OFF") +
    '</b></div>' +

    '<div class="row"><span>Freeze authority</span><b>' +
      (s.freeze_authority ? "ACTIVE" : "OFF") +
    '</b></div>';
}

function renderChart(candles, fit = false) {
  const cleanCandles = sanitizeChartCandles(candles);

  if (!initChart() || !cleanCandles.length) return;

  candles = cleanCandles;

  candleSeries.setData(
    candles.map(x=>({
      time:x.time,
      open:displayValue(x.o),
      high:displayValue(x.h),
      low:displayValue(x.l),
      close:displayValue(x.c)
    }))
  );

  volumeSeries.setData(
    candles.map(x=>({
      time:x.time,
      value:Math.max(0,Number(x.v) || 0),
      color:x.c >= x.o
        ? "rgba(57,220,137,.22)"
        : "rgba(255,101,117,.22)"
    }))
  );

  const ind = indicatorSeries(candles);

  ema9Series.setData(
    ind.ema9.map(x=>({
      time:x.time,
      value:displayValue(x.value / 1)
    }))
  );
  ema21Series.setData(
    ind.ema21.map(x=>({
      time:x.time,
      value:displayValue(x.value / 1)
    }))
  );
  markersApi.setMarkers(buildMarkers(candles));

  const signal = signalFromCandles(candles);
  renderSignal(signal);

  chart.priceScale("right").applyOptions({
    autoScale:true
  });

  if (fit) {
    chart.timeScale().fitContent();
  }

  const last = candles[candles.length - 1];

  lastRenderedCandleTime =
    Number(last.time || 0);

  updateActivePrice(last.c);
  $("chartMode").textContent =
    chartDataSource + " · " + timeframeLabel() +
    (livePreviewActive ? " · LIVE" : " CANDLES") +
    " · " + candles.length + " BARS";

  $("chartState").textContent = "LIVE";
  setSource("dotChart","chartState","LIVE",["LIVE","READY"]);

  $("historyStatus").textContent =
    historyBarsLoaded.toLocaleString() + " bars";

  renderTape();
}

function updateLiveCandleOnSeries(candle) {
  if (!candleSeries) return;

  candleSeries.update({
    time:candle.time,
    open:displayValue(candle.o),
    high:displayValue(candle.h),
    low:displayValue(candle.l),
    close:displayValue(candle.c)
  });

  volumeSeries.update({
    time:candle.time,
    value:Math.max(0,candle.v || 0),
    color:candle.c >= candle.o
      ? "rgba(57,220,137,.22)"
      : "rgba(255,101,117,.22)"
  });
}

function renderLiveIndicators() {
  if (!chartInitialized || !selectedCandles.length) return;

  const cleanCandles = sanitizeChartCandles(selectedCandles);
  if (!cleanCandles.length) return;

  const ind = indicatorSeries(cleanCandles);

  ema9Series.setData(
    ind.ema9.map(x=>({
      time:x.time,
      value:displayValue(x.value)
    }))
  );

  ema21Series.setData(
    ind.ema21.map(x=>({
      time:x.time,
      value:displayValue(x.value)
    }))
  );

  const signal =
    signalFromCandles(
      cleanCandles
    );

  renderSignal(signal);

  const last =
    cleanCandles[
      cleanCandles.length - 1
    ];

  const markerNeedsRefresh =
    lastIndicatorCandleTime !== last.time ||
    lastIndicatorState !== signal.state;

  if (
    markersApi &&
    markerNeedsRefresh
  ) {
    markersApi.setMarkers(
      buildMarkers(
        cleanCandles
      )
    );

    lastIndicatorCandleTime =
      last.time;

    lastIndicatorState =
      signal.state;
  }
}

function scheduleLiveIndicatorRender() {
  if (indicatorRenderScheduled) return;

  indicatorRenderScheduled = true;

  requestAnimationFrame(() => {
    indicatorRenderScheduled = false;

    if (!selectedMint || !chartInitialized) return;

    renderLiveIndicators();
  });
}

function updateRealtimeChart(candle) {
  if (!candle || !candleSeries) return;

  // The price/OHLC path is intentionally synchronous so the visible candle
  // moves on the same animation frame as the trade. Expensive indicators and
  // marker generation are coalesced to one pass per browser frame.
  const previousRenderedTime =
    lastRenderedCandleTime;

  updateLiveCandleOnSeries(candle);

  // Pump.fun keeps the viewport pinned to the right edge while the live
  // stream advances. Do the same only when a genuinely new candle appears;
  // normal intra-candle wick updates do not disturb the user's current view.
  if (
    chart &&
    Number(candle.time || 0) > previousRenderedTime
  ) {
    lastRenderedCandleTime =
      Number(candle.time || 0);

    chart.timeScale().scrollToRealTime();
  }

  updateActivePrice(
    candle.c,
    Date.now()
  );

  $("historyStatus").textContent =
    historyBarsLoaded.toLocaleString() + " bars";

  scheduleLiveIndicatorRender();
}

function scheduleLiveRender() {
  if (renderScheduled) return;

  renderScheduled = true;

  requestAnimationFrame(()=>{
    renderScheduled = false;

    // Websocket trades update the low-latency tape/active price immediately.
    // OHLC/EMA/markers stay under periodic Pump.fun candle reconciliation so
    // execution-price differences can never create fake wicks.
    if (!selectedMint) return;
    renderTape();
  });
}


function rebuildSelectedFromRaw(raw) {
  selectedCandles = aggregateCandles(
    raw,
    chartTimeframe
  );

  selectedCandles.sort((a,b)=>a.time-b.time);

  if (selectedCandles.length > MAX_HISTORY_BARS) {
    selectedCandles =
      selectedCandles.slice(-MAX_HISTORY_BARS);
  }

  historyBarsLoaded = selectedCandles.length;
}

function mergePage(page) {
  if (!page.length) return;

  const byTime = new Map(
    selectedCandles.map(x=>[x.time,x])
  );

  // Pump.fun's native OHLC is the single source of truth. Never merge a
  // Helius execution-price sample into an OHLC bar because the two can use
  // different price definitions/timing.
  for (const item of page) {
    byTime.set(item.time,{...item});
  }

  selectedCandles = [...byTime.values()]
    .sort((a,b)=>a.time-b.time)
    .slice(-MAX_HISTORY_BARS);

  historyBarsLoaded = selectedCandles.length;
}
async function fetchInitialHistory() {
  const generation = historyGeneration;

  if (
    initialHistoryBusy &&
    initialHistoryGeneration === generation
  ) {
    return false;
  }

  initialHistoryBusy = true;
  initialHistoryGeneration = generation;
  chartHistoryRetryGeneration = generation;

  try {
    $("chartMode").textContent =
      "CONNECTING TO REAL MARKET HISTORY…";

    $("historyStatus").textContent =
      selectedCandles.length
        ? selectedCandles.length.toLocaleString() + " live bars"
        : "loading…";

    if (chartInterval !== "1s") {
      syncCurrentPumpCandle().catch(()=>{});
    }

    // Try the history pipeline more than once. A just-migrated Pump.fun coin,
    // a provider timeout, or a rate-limit response should not leave the user
    // staring at a permanently empty chart.
    let loaded = false;
    for (let attempt = 0; attempt < 3; attempt++) {
      if (
        generation !== historyGeneration ||
        !selectedMint
      ) {
        return false;
      }

      loaded = await fetchFastHistoricalBackfill(generation);

      if (loaded || selectedCandles.length) {
        break;
      }

      if (attempt < 2) {
        $("historyStatus").textContent =
          "retrying history (" + (attempt + 2) + "/3)…";
        await new Promise(resolve =>
          setTimeout(resolve, 500 * Math.pow(2, attempt))
        );
      }
    }

    if (
      generation !== historyGeneration ||
      !selectedMint
    ) {
      return false;
    }

    if (selectedCandles.length) {
      historyBarsLoaded = selectedCandles.length;

      if (chartInitialized) {
        renderChart(selectedCandles,true);
      }

      $("chartMode").textContent =
        chartDataSource +
        " · " +
        timeframeLabel() +
        " · " +
        (chartInterval === "1s" ? "LIVE TRADES" : "LIVE");

      $("historyStatus").textContent =
        historyBarsLoaded.toLocaleString() + " bars";
    } else if (!loaded) {
      $("chartMode").textContent =
        "NO HISTORY YET — WAITING FOR MARKET DATA…";
      $("historyStatus").textContent =
        "retrying automatically";

      scheduleChartHistoryRetry(generation);
    }

    return Boolean(selectedCandles.length);
  } catch {
    if (generation === historyGeneration && !selectedCandles.length) {
      $("chartMode").textContent =
        "NO HISTORY YET — RETRYING…";
      $("historyStatus").textContent =
        "retrying automatically";
      scheduleChartHistoryRetry(generation);
    }
    refreshIndicatorHistory(
        generation
      ).catch(()=>{});

    return Boolean(selectedCandles.length);
  } finally {
    if (initialHistoryGeneration === generation) {
      initialHistoryBusy = false;
    }
  }
}

function scheduleChartHistoryRetry(generation) {
  if (chartHistoryRetryTimer) {
    clearTimeout(chartHistoryRetryTimer);
    chartHistoryRetryTimer = null;
  }

  chartHistoryRetryGeneration = generation;

  chartHistoryRetryTimer = setTimeout(() => {
    chartHistoryRetryTimer = null;

    if (
      !selectedMint ||
      generation !== historyGeneration ||
      generation !== chartHistoryRetryGeneration ||
      selectedCandles.length
    ) {
      return;
    }

    fetchInitialHistory().catch(()=>{});
  }, 2500);
}
async function fetchFastHistoricalBackfill(generation) {
  if (
    !selectedMint ||
    generation !== historyGeneration
  ) {
    return false;
  }

  try {
    let historyJson = null;

    try {
      const r = await fetch(
        "/api/chart/history?mint=" +
        encodeURIComponent(selectedMint) +
        "&timeframe=" + backendTimeframe() +
        "&interval=" + encodeURIComponent(chartInterval) +
        "&limit=120&t=" + Date.now(),
        {cache:"no-store"}
      );

      if (r.ok) {
        historyJson = await readJsonResponse(r);
      }
    } catch {}

    const j = historyJson || {
      source:"NONE",
      candles:[]
    };

    let candles = sanitizeChartCandles(j.candles || []);
    let usedLiveTradeFallback = false;
    let fallbackTimestampSec = 0;

    // Never leave the chart blank just because one historical provider timed
    // out. Exact-venue decoded trades are safe to aggregate at any requested
    // timeframe, including true 1-second bars.
    if (!candles.length) {
      try {
        const liveResponse = await fetch(
          "/api/chart/live-trades?mint=" +
          encodeURIComponent(selectedMint) +
          "&limit=200&t=" + Date.now(),
          {cache:"no-store"}
        );

        if (liveResponse.ok) {
          const liveJson = await readJsonResponse(liveResponse);
          const liveRows = (liveJson.trades || [])
            .map(normalizeTrade)
            .filter(Boolean);

          candles = aggregateLiveTrades(
            liveRows,
            chartInterval === "1s"
              ? "1s"
              : chartTimeframe
          );

          if (candles.length) {
            usedLiveTradeFallback = true;
            const rawFallbackTimestamp = Number(
              liveJson.timestamp || 0
            );
            fallbackTimestampSec =
              rawFallbackTimestamp > 2e10
                ? Math.floor(rawFallbackTimestamp / 1000)
                : (
                    rawFallbackTimestamp ||
                    Math.floor(Date.now() / 1000)
                  );
          }
        }
      } catch {}
    }

    // A fast live websocket/cache snapshot can arrive while the historical
    // request is in flight. Use it as the final client-side fallback too.
    if (!candles.length && selectedTrades.length) {
      candles = aggregateLiveTrades(
        selectedTrades,
        chartInterval === "1s"
          ? "1s"
          : chartTimeframe
      );

      if (candles.length) {
        usedLiveTradeFallback = true;
        fallbackTimestampSec = Math.floor(Date.now() / 1000);
      }
    }

    if (
      generation !== historyGeneration ||
      !selectedMint ||
      !candles.length
    ) {
      return false;
    }

    // A timeframe switch must not mix old-timeframe bars with the new
    // series. Replace the displayed series atomically with the real response.
    selectedCandles = candles
      .map(x=>({...x}))
      .sort((a,b)=>a.time-b.time)
      .slice(-MAX_HISTORY_BARS);

    historyBarsLoaded = selectedCandles.length;
    chartHistoryLoadedAtSec =
      usedLiveTradeFallback
        ? (
            fallbackTimestampSec ||
            Math.floor(Date.now() / 1000)
          )
        : (
            Number(j.timestamp) > 0
              ? Number(j.timestamp)
              : Math.floor(Date.now() / 1000)
          );

    if (chartInterval === "1m") {
      selectedMinuteCandles = candles
        .map(x => ({...x}))
        .sort((a,b) => a.time - b.time)
        .slice(-MAX_HISTORY_BARS);

      selectedMinuteSource =
        j.source || "PUMP.FUN";
    }

    chartDataSource = usedLiveTradeFallback
      ? "PUMP.FUN LIVE TRADES"
      : (
          j.source ||
          "PUMP.FUN"
        );
    livePreviewActive = usedLiveTradeFallback;

    if (chartInitialized) {
      renderChart(selectedCandles,true);
    }

    $("historyStatus").textContent =
      historyBarsLoaded.toLocaleString() + " bars";

    return true;
  } catch {
    return false;
  }
}

function liveTradeKey(trade) {
  if (!trade) return "";

  const signature =
    String(trade.signature || "").trim();

  const time =
    Math.floor(
      Number(
        trade.time ??
        trade.timestamp ??
        0
      )
    );

  const price =
    Number(
      trade.chartPrice ??
      trade.price ??
      0
    );

  const side =
    String(
      trade.side || "BUY"
    ).toUpperCase();

  const volume =
    Number(
      trade.chartVolume ??
      trade.volumeSol ??
      trade.volume_sol ??
      0
    );

  const base =
    [
      time,
      Number.isFinite(price)
        ? price.toPrecision(17)
        : "0",
      side,
      Number.isFinite(volume)
        ? volume.toPrecision(17)
        : "0"
    ].join("|");

  return signature
    ? signature + "|" + base
    : String(trade.id || "") + "|" + base;
}

function applyLivePrice(
  price,
  timestampMs = Date.now(),
  recordTrade = null
) {
  price = Number(price);

  if (
    !Number.isFinite(price) ||
    price <= 0 ||
    !selectedMint
  ) {
    return;
  }

  updateActivePrice(
    price,
    Number(timestampMs || Date.now())
  );

  if (recordTrade) {
    const key =
      liveTradeKey(
        recordTrade
      );

    if (
      !key ||
      selectedTradeKeys.has(key)
    ) {
      return;
    }

    selectedTradeKeys.add(key);

    selectedTrades.push({
      ...recordTrade,
      _clientKey:key
    });

    while (
      selectedTrades.length > 2000
    ) {
      const removed =
        selectedTrades.shift();

      if (
        removed &&
        removed._clientKey
      ) {
        selectedTradeKeys.delete(
          removed._clientKey
        );
      }
    }

    $("lastUpdate").textContent =
      new Date(
        recordTrade.time * 1000
      ).toLocaleTimeString();
  }

  scheduleLiveRender();
}



function aggregateLiveTrades(trades, tfMinutes = chartTimeframe) {
  const span =
    tfMinutes === "1s"
      ? 1
      : Math.max(
          60,
          Number(tfMinutes || 1) * 60
        );

  const rows = new Map();

  for (
    const trade of [...(trades || [])]
      .sort((a,b)=>a.time-b.time)
  ) {
    const price = Number(
      trade.chartPrice ?? trade.price
    );
    const ts = Number(trade.time);

    if (
      !Number.isFinite(price) ||
      price <= 0 ||
      !Number.isFinite(ts)
    ) {
      continue;
    }

    const bucket =
      Math.floor(ts / span) * span;

    const volume = Math.max(
      0,
      Number(
        trade.chartVolume ??
        trade.volumeSol ??
        trade.volume_sol ??
        0
      )
    );

    let row = rows.get(bucket);

    if (!row) {
      row = {
        time:bucket,
        ts:bucket,
        o:price,
        h:price,
        l:price,
        c:price,
        v:volume,
        _firstTs:ts,
        _lastTs:ts
      };

      rows.set(bucket,row);
      continue;
    }

    row.h = Math.max(
      row.h,
      price
    );

    row.l = Math.min(
      row.l,
      price
    );

    row.v += volume;

    if (ts < row._firstTs) {
      row._firstTs = ts;
      row.o = price;
    }

    if (ts >= row._lastTs) {
      row._lastTs = ts;
      row.c = price;
    }
  }

  return [...rows.values()]
    .map(x=>{
      delete x._firstTs;
      delete x._lastTs;
      return x;
    })
    .sort((a,b)=>a.time-b.time)
    .slice(-MAX_HISTORY_BARS);
}

async function refreshIndicatorHistory(generation) {
  if (
    !selectedMint ||
    generation !== historyGeneration
  ) {
    return;
  }

  try {
    const r = await fetch(
      "/api/chart/history?mint=" +
      encodeURIComponent(selectedMint) +
      "&timeframe=1&interval=1m&limit=120&t=" +
      Date.now(),
      {cache:"no-store"}
    );

    if (!r.ok) return;

    const j = await readJsonResponse(r);

    if (
      generation !== historyGeneration ||
      !selectedMint
    ) {
      return;
    }

    const rows =
      sanitizeChartCandles(
        j.candles || []
      );

    if (!rows.length) return;

    selectedMinuteCandles =
      rows
        .map(x=>({...x}))
        .sort((a,b)=>a.time-b.time)
        .slice(-MAX_HISTORY_BARS);

    selectedMinuteSource =
      j.source || "PUMP.FUN";

    selectedMinuteGeneration =
      generation;

    lastIndicatorCandleTime = 0;
    lastIndicatorState = "WAIT";

    scheduleLiveIndicatorRender();
  } catch {
    // Indicator history is isolated from the displayed chart.
  }
}

function updateIndicatorMinuteCandle(trade) {
  if (!trade) return;

  const price = Number(trade.price);
  const ts = Number(trade.time);

  if (
    !Number.isFinite(price) ||
    price <= 0 ||
    !Number.isFinite(ts)
  ) {
    return;
  }

  const bucket =
    Math.floor(ts / 60) * 60;

  let bar =
    selectedMinuteCandles.find(
      x => x.time === bucket
    );

  if (!bar) {
    const last =
      selectedMinuteCandles[
        selectedMinuteCandles.length - 1
      ];

    if (
      last &&
      bucket < last.time
    ) {
      return;
    }

    bar = {
      time:bucket,
      ts:bucket,
      o:price,
      h:price,
      l:price,
      c:price,
      v:Math.max(
        0,
        Number(
          trade.volumeSol ??
          trade.volume_sol ??
          0
        )
      ),
      _firstTs:ts,
      _lastTs:ts
    };

    selectedMinuteCandles = [
      ...selectedMinuteCandles,
      bar
    ].slice(-MAX_HISTORY_BARS);
  } else {
    bar.h =
      Math.max(
        Number(bar.h),
        price
      );

    bar.l =
      Math.min(
        Number(bar.l),
        price
      );

    const lastTs =
      Number.isFinite(bar._lastTs)
        ? bar._lastTs
        : bar.time;

    if (ts >= lastTs) {
      bar._lastTs = ts;
      bar.c = price;
    }

    if (
      !Number.isFinite(bar._firstTs) ||
      ts < bar._firstTs
    ) {
      bar._firstTs = ts;
      bar.o = price;
    }
  }

  selectedMinuteSource =
    "PUMP.FUN LIVE TRADES";

  selectedMinuteCandles =
    selectedMinuteCandles.map(x =>
      x.time !== bar.time
        ? x
        : {
            time:x.time,
            ts:x.ts,
            o:x.o,
            h:x.h,
            l:x.l,
            c:x.c,
            v:Number(x.v || 0),
            _firstTs:x._firstTs,
            _lastTs:x._lastTs
          }
    );
}

function updateCandleFromLiveTrade(trade) {
  if (!trade || !selectedMint) return false;

  const price = Number(trade.price);
  const ts = Number(trade.time);

  if (
    !Number.isFinite(price) ||
    price <= 0 ||
    !Number.isFinite(ts)
  ) {
    return false;
  }

  const span = timeframeSeconds();

  const bucket =
    Math.floor(ts / span) * span;

  const volume = Math.max(
    0,
    Number(
      trade.volumeSol ??
      trade.volume_sol ??
      0
    )
  );

  let bar = selectedCandles.find(
    x => x.time === bucket
  );
  let lateTradeForCurrentBar = false;

  // A live price fallback is never an OHLC authority. The first real trade
  // replaces it completely.
  if (
    chartDataSource === "LIVE_PRICE" &&
    selectedCandles.length === 1 &&
    selectedCandles[0].time === bucket
  ) {
    bar = {
      time:bucket,
      ts:bucket,
      o:price,
      h:price,
      l:price,
      c:price,
      v:volume,
      _firstTs:ts,
      _lastTs:ts
    };

    selectedCandles = [bar];
  } else if (!bar) {
    const last =
      selectedCandles[
        selectedCandles.length - 1
      ];

    if (
      last &&
      bucket < last.time
    ) {
      // Providers can timestamp a processed trade a fraction late while the
      // history endpoint has already exposed the next bucket. When the gap is
      // only one candle wide, treat the trade as the current live candle
      // instead of silently throwing away the real trade.
      if (last.time - bucket <= span) {
        bar = last;
        lateTradeForCurrentBar = true;
      } else {
        return false;
      }
    } else {
      bar = {
      time:bucket,
      ts:bucket,
      o:price,
      h:price,
      l:price,
      c:price,
      v:volume,
      _firstTs:ts,
      _lastTs:ts
    };

      selectedCandles = [
        ...selectedCandles,
        bar
      ].slice(-MAX_HISTORY_BARS);
    }
  } else {
    bar.h = Math.max(
      bar.h,
      price
    );

    bar.l = Math.min(
      bar.l,
      price
    );

    bar.v =
      Number(bar.v || 0) +
      volume;

    const firstTs =
      Number.isFinite(bar._firstTs)
        ? bar._firstTs
        : bar.time;

    const lastTs =
      Number.isFinite(bar._lastTs)
        ? bar._lastTs
        : bar.time;

    if (ts < firstTs) {
      bar._firstTs = ts;
      bar.o = price;
    }

    if (ts >= lastTs) {
      bar._lastTs = ts;
      bar.c = price;
    }
  }

  if (lateTradeForCurrentBar) {
    bar.h = Math.max(
      bar.h,
      price
    );
    bar.l = Math.min(
      bar.l,
      price
    );
    bar.v =
      Number(bar.v || 0) +
      volume;

    const firstTs =
      Number.isFinite(bar._firstTs)
        ? bar._firstTs
        : bar.time;
    const lastTs =
      Number.isFinite(bar._lastTs)
        ? bar._lastTs
        : bar.time;

    if (ts < firstTs) {
      bar._firstTs = ts;
      bar.o = price;
    }
    if (ts >= lastTs) {
      bar._lastTs = ts;
      bar.c = price;
    }
  }

  // Keep internal chronological timestamps on the active bar so a
  // slightly out-of-order websocket event cannot corrupt OPEN/CLOSE.
  bar._firstTs =
    Number.isFinite(bar._firstTs)
      ? bar._firstTs
      : ts;

  bar._lastTs =
    Number.isFinite(bar._lastTs)
      ? bar._lastTs
      : ts;

  const cleanBar = {
    time:bar.time,
    ts:bar.ts,
    o:bar.o,
    h:bar.h,
    l:bar.l,
    c:bar.c,
    v:bar.v
  };

  livePreviewActive = true;

  historyBarsLoaded =
    selectedCandles.length;

  updateActivePrice(
    price,
    ts * 1000
  );

  if (
    chartInitialized &&
    candleSeries
  ) {
    updateRealtimeChart(
      cleanBar
    );

    renderTape();

    $("chartMode").textContent =
      "PUMP.FUN LIVE · " +
      timeframeLabel() +
      " · LIVE TRADES";
  }

  return true;
}


function liveTradePriceIsPlausible(price) {
  price = Number(price);

  if (
    !Number.isFinite(price) ||
    price <= 0 ||
    !selectedCandles.length
  ) {
    return true;
  }

  const current = selectedCandles[selectedCandles.length - 1];
  const baseline = Number(current?.c);

  if (!Number.isFinite(baseline) || baseline <= 0) {
    return true;
  }

  // A real Pump.fun trade can move very quickly, but a 100x+ or 100x-
  // single update against the already-rendered candle is overwhelmingly more
  // likely to be a unit/schema error than a real execution. Refuse it before
  // it can move the active price or corrupt the visible candle.
  const ratio = price / baseline;
  return ratio >= 0.01 && ratio <= 100;
}

function applyLiveTrade(rawTrade, record = true) {
  const t = normalizeTrade(rawTrade);
  if (!t || !selectedMint) return;

  if (
    record &&
    selectedTrades.some(x => x.id === t.id)
  ) {
    return;
  }

  // Accept both sides of Pump.fun's lifecycle: bonding-curve trades and
  // PumpSwap AMM trades after migration.
  if (
    t.source !== "PUMP.FUN" &&
    t.source !== "PUMPSWAP"
  ) {
    syncCurrentPumpCandle();
    return;
  }

  const chartTrade = normalizeTradeForChart(t);

  if (!liveTradePriceIsPlausible(chartTrade.price)) {
    return;
  }

  lastLiveTradeAtMs = Date.now();

  const recordedTrade = (
    record &&
    !t.synthetic
  )
    ? {
        ...t,
        chartPrice: chartTrade.price,
        chartVolume: chartTrade.volume_sol,
      }
    : null;

  applyLivePrice(
    chartTrade.price,
    chartTrade.time * 1000,
    recordedTrade
  );

  // Update the current OHLC bucket and every dependent indicator
  // immediately on the trade/price event. Synthetic Pump.fun price pulses
  // never enter the tape, but they still keep the active wick current while
  // the next decoded trade is in flight.
  updateIndicatorMinuteCandle(
    chartTrade
  );

  updateCandleFromLiveTrade(
    chartTrade
  );
}

async function pollLivePrice() {
  if (!selectedMint) return;

  try {
    const r = await fetch(
      "/api/live/price?mint=" +
      encodeURIComponent(selectedMint) +
      "&t=" + Date.now(),
      {cache:"no-store"}
    );

    const data = await readJsonResponse(r);
    const price = Number(data.price);

    if (Number.isFinite(price) && price > 0) {
      selectedLiveUsdPrice = price;
      selectedLiveUsdAt = Date.now();
      const now = Date.now();

      // HTTP asset price is only a backup display value. Never let it
      // overwrite a live Pump.fun trade or a real chart candle.
      if (
        !selectedTrades.length &&
        !selectedCandles.length
      ) {
        updateActivePrice(price, now);
      }

      if (!selectedCandles.length) {
        requestCurrentCandleSync();
      }
    }
  } catch {}
}

async function syncLiveTradeCache() {
  if (!selectedMint || liveTradeCacheBusy) return;

  // Websocket trade events are authoritative. HTTP only repairs a silent feed.
  if (
    lastLiveTradeAtMs > 0 &&
    Date.now() - lastLiveTradeAtMs < 750
  ) {
    return;
  }

  liveTradeCacheBusy = true;

  try {
    const generation = historyGeneration;

    const r = await fetch(
      "/api/chart/live-trades?mint=" +
      encodeURIComponent(selectedMint) +
      "&limit=" +
        (chartInterval === "1s" ? "200" : "50") +
        "&t=" + Date.now(),
      {cache:"no-store"}
    );

    if (!r.ok) return;

    const j = await readJsonResponse(r);
    const rows = Array.isArray(j.trades) ? j.trades : [];

    if (
      generation !== historyGeneration ||
      !selectedMint
    ) {
      return;
    }

    for (const row of rows) {
      const normalized = normalizeTrade(row);
      if (!normalized) continue;
      applyLiveTrade(normalized,true);
    }
  } catch {
    // Native/backend websockets remain primary.
  } finally {
    liveTradeCacheBusy = false;
  }
}

function startLiveTradeCachePoll() {
  if (liveTradeCacheTimer) {
    clearInterval(liveTradeCacheTimer);
  }

  if (liveTradeWatchdogTimer) {
    clearInterval(liveTradeWatchdogTimer);
    liveTradeWatchdogTimer = null;
  }

  liveTradeCacheGeneration = historyGeneration;

  liveTradeCacheTimer = setInterval(() => {
    if (
      selectedMint &&
      liveTradeCacheGeneration === historyGeneration
    ) {
      syncLiveTradeCache();
    }
  },250);

  liveTradeWatchdogTimer = setInterval(() => {
    if (
      !selectedMint ||
      liveTradeCacheGeneration !== historyGeneration ||
      !liveTradeSocket ||
      liveTradeSocket.readyState !== WebSocket.OPEN
    ) {
      return;
    }

    const now = Date.now();

    if (
      lastLiveTradeAtMs > 0 &&
      now - lastLiveTradeAtMs > 4500
    ) {
      // Reset only the backend websocket; never tear down the direct native
      // Pump.fun websocket from this recovery path.
      connectLiveTrade(selectedMint);
    }
  },1500);

  syncLiveTradeCache();
}

async function syncCurrentPumpCandle() {
  if (!selectedMint) return;

  // Do not repaint the live candle from HTTP immediately after a real trade.
  if (
    lastLiveTradeAtMs > 0 &&
    Date.now() - lastLiveTradeAtMs < 750
  ) {
    return;
  }

  if (currentCandleSyncInFlight) {
    currentCandleSyncQueued = true;
    if (currentCandleSyncPromise) {
      await currentCandleSyncPromise;
    }
    return;
  }

  currentCandleSyncInFlight = true;

  const work = (async() => {
    try {
      const generation = historyGeneration;

      const r = await fetch(
        "/api/chart/current?mint=" +
        encodeURIComponent(selectedMint) +
        "&timeframe=" +
        backendTimeframe() +
        "&interval=" +
        encodeURIComponent(chartInterval) +
        "&t=" + Date.now(),
        {cache:"no-store"}
      );

      if (!r.ok) return;

      const j = await readJsonResponse(r);

      if (
        generation !== historyGeneration ||
        !selectedMint
      ) {
        return;
      }

      const candles = (j.candles || [])
        .map(normalizeCandle)
        .filter(Boolean);

      if (!candles.length) return;

      const incomingSource = String(
        j.source || chartDataSource
      );

      const liveTradeSnapshot =
        incomingSource === "PUMP.FUN LIVE TRADES";

      if (
        incomingSource === "LIVE_PRICE" &&
        selectedCandles.length
      ) {
        return;
      }

      const incoming = candles[0];
      const current =
        selectedCandles[
          selectedCandles.length - 1
        ];

      if (
        livePreviewActive &&
        current &&
        incoming &&
        incoming.time === current.time
      ) {
        return;
      }

      if (
        current &&
        incoming &&
        incoming.time < current.time
      ) {
        mergePage(candles);
        return;
      }

      mergePage(candles);

      const last =
        selectedCandles[
          selectedCandles.length - 1
        ];

      if (liveTradeSnapshot) {
        livePreviewActive = true;
      } else {
        chartDataSource = incomingSource;
        livePreviewActive = false;
      }

      updateRealtimeChart(last);
    } catch {
      // Real websocket lanes remain live if HTTP recovery fails.
    }
  })();

  currentCandleSyncPromise = work;

  try {
    await work;
  } finally {
    currentCandleSyncPromise = null;
    currentCandleSyncInFlight = false;
    currentCandleSyncQueued = false;
  }
}

function requestCurrentCandleSync() {
  if (!selectedMint) return;
  syncCurrentPumpCandle();
}

function startLivePricePoll() {
  if (pricePollTimer) clearInterval(pricePollTimer);
  pricePollTimer = setInterval(pollLivePrice, 1200);
  pollLivePrice();
}

function startCurrentCandleSync() {
  if (currentCandleSyncTimer) {
    clearTimeout(currentCandleSyncTimer);
  }

  const tick = async () => {
    if (!selectedMint) return;

    await syncCurrentPumpCandle();

    if (!selectedMint) return;

    // A 250 ms reconciliation lane is still well below the server's current
    // chart/current rate limit. The websocket remains the primary path; this
    // only repairs missed/stalled live updates.
    currentCandleSyncTimer = setTimeout(
      tick,
      250
    );
  };

  tick();
}
function scheduleNativePumpFunReconnect(mint) {
  if (
    pumpFunNativeReconnectTimer ||
    !selectedMint ||
    selectedMint !== mint
  ) {
    return;
  }

  const wait = pumpFunNativeBackoff;

  pumpFunNativeBackoff = Math.min(
    5000,
    Math.round(
      pumpFunNativeBackoff * 1.6
    )
  );

  pumpFunNativeReconnectTimer = setTimeout(() => {
    pumpFunNativeReconnectTimer = null;

    if (
      selectedMint &&
      selectedMint === mint
    ) {
      connectNativePumpFunTrades(mint);
    }
  },wait);
}

const PUMP_FUN_SUBSCRIBE_FRAMES = [
  "42[\"subscribe\",\"tradeCreated\"]",
  "42[\"subscribe\",{\"event\":\"tradeCreated\"}]",
  "42[\"subscribe\",{\"type\":\"tradeCreated\"}]",
  "42[\"join\",\"tradeCreated\"]",
  "42[\"join\",\"trades\"]",
  "42[\"subscribe\",\"all\"]"
];

function connectNativePumpFunTrades(mint) {
  if (!mint) return;

  if (pumpFunNativeReconnectTimer) {
    clearTimeout(
      pumpFunNativeReconnectTimer
    );
    pumpFunNativeReconnectTimer = null;
  }

  if (pumpFunNativeTradeSocket) {
    const oldSocket = pumpFunNativeTradeSocket;
    pumpFunNativeTradeSocket = null;

    try {
      oldSocket.close();
    } catch {}
  }

  const url =
    "wss://frontend-api-v3.pump.fun/socket.io/" +
    "?EIO=4&transport=websocket";

  try {
    const socket = new WebSocket(url);
    pumpFunNativeTradeSocket = socket;

    socket.addEventListener("open",() => {
      if (socket !== pumpFunNativeTradeSocket) {
        return;
      }

      pumpFunNativeBackoff = 500;

      $("chartMode").textContent =
        "PUMP.FUN NATIVE LIVE · " +
        timeframeLabel() +
        " · CONNECTING";
    });

    socket.addEventListener("message",(ev) => {
      if (sock      if (data.startsWith("40")) {
        for (const frame of PUMP_FUN_SUBSCRIBE_FRAMES) {
          try {
            socket.send(frame);
          } catch {}
        }
open.
      if (data.startsWith("0")) {
        try {
          socket.send("40");
        } catch {}
        return;
      }

      if (data.startsWith("40")) {
        try {
          socket.send(
            '42["subscribe","tradeCreated"]'
          );
        } catch {}

        $("chartMode").textContent =
          "PUMP.FUN NATIVE LIVE · " +
          timeframeLabel() +
          " · LIVE";

        return;
      }

      // Engine.IO ping -> pong.
      if (
        data === "2" ||
        data.startsWith("2")
      ) {
        try {
          socket.send("3");
        } catch {}
        return;
      }

      if (!data.startsWith("42")) {
        return;
      }

      if (
        selectedMint &&
        data.indexOf(selectedMint) < 0
      ) {
        return;
      }

      try {
        const packet =
          JSON.parse(
            data.slice(2)
          );

        if (
          !Array.isArray(packet) ||
          packet.length < 2 ||
          packet[0] !== "tradeCreated" ||
          !packet[1] ||
          typeof packet[1] !== "object"
        ) {
          return;
        }

        const raw = packet[1];

        if (
          !selectedMint ||
          raw.mint !== selectedMint
        ) {
          return;
        }

        const trade =
          parseNativePumpFunTrade(raw);

        if (!trade) {
          return;
        }

        applyLiveTrade(
          trade,
          true
        );
      } catch {}
    });

    socket.addEventListener("close",() => {
      if (socket !== pumpFunNativeTradeSocket) {
        return;
      }

      pumpFunNativeTradeSocket = null;

      $("chartMode").textContent =
        "RECONNECTING PUMP.FUN LIVE FEED…";

      scheduleNativePumpFunReconnect(
        mint
      );
    });

    socket.addEventListener("error",() => {
      if (socket !== pumpFunNativeTradeSocket) {
        return;
      }

      try {
        socket.close();
      } catch {}
    });
  } catch {
    scheduleNativePumpFunReconnect(mint);
  }
}

function disconnectBackendLiveTrade() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  if (fallbackTimer) {
    clearInterval(fallbackTimer);
    fallbackTimer = null;
  }

  if (liveTradeSocket) {
    try {
      liveTradeSocket.close();
    } catch {}

    liveTradeSocket = null;
  }
}

function disconnectLiveTrade() {
  if (chartHistoryRetryTimer) {
    clearTimeout(chartHistoryRetryTimer);
    chartHistoryRetryTimer = null;
  }

  if (fallbackTimer) {
    clearInterval(fallbackTimer);
    fallbackTimer = null;
  }

  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }

  if (pricePollTimer) {
    clearInterval(pricePollTimer);
    pricePollTimer = null;
  }

  if (liveTradeCacheTimer) {
    clearInterval(liveTradeCacheTimer);
    liveTradeCacheTimer = null;
  }

  if (liveTradeWatchdogTimer) {
    clearInterval(liveTradeWatchdogTimer);
    liveTradeWatchdogTimer = null;
  }

  liveTradeCacheBusy = false;

  if (pumpFunNativeReconnectTimer) {
    clearTimeout(pumpFunNativeReconnectTimer);
    pumpFunNativeReconnectTimer = null;
  }

  if (pumpFunNativeTradeSocket) {
    const oldSocket = pumpFunNativeTradeSocket;
    pumpFunNativeTradeSocket = null;

    try {
      oldSocket.close();
    } catch {}
  }

  if (currentCandleSyncTimer) {
    clearTimeout(currentCandleSyncTimer);
    currentCandleSyncTimer = null;
  }

  disconnectBackendLiveTrade();
}

function scheduleReconnect() {
  if (reconnectTimer || !selectedMint) return;

  const wait = liveTradeBackoff;
  liveTradeBackoff =
    Math.min(5000,liveTradeBackoff * 2);

  reconnectTimer = setTimeout(()=>{
    reconnectTimer = null;

    if (selectedMint) {
      connectLiveTrade(selectedMint);
    }
  },wait);

  // If the stream is temporarily down, do a light reconciliation against
  // Pump.fun instead of hammering it continuously.
  if (!fallbackTimer) {
    fallbackTimer = setInterval(()=>{
      if (
        selectedMint &&
        (!liveTradeSocket ||
        liveTradeSocket.readyState !== WebSocket.OPEN)
      ) {
        fetchInitialHistory();
      }
    },10000);
  }
}

function connectLiveTrade(mint) {
  disconnectBackendLiveTrade();

  if (!mint) return;

  const proto =
    location.protocol === "https:" ? "wss:" : "ws:";

  const url =
    proto +
    "//" +
    location.host +
    "/ws/trades?mint=" +
    encodeURIComponent(mint);

  setSource(
    "dotChart",
    "chartState",
    "CONNECTING",
    ["LIVE","READY","CONNECTING"]
  );

  try {
    const socket = new WebSocket(url);
    liveTradeSocket = socket;

    // connectLiveTrade() is also used by the watchdog/reconnect path.
    // disconnectLiveTrade() clears the recovery timers, so restart those
    // timers here too. Otherwise the first automatic reconnect could
    // accidentally leave the chart with no live reconciliation lane.
    startCurrentCandleSync();
    startLiveTradeCachePoll();
    startLivePricePoll();

    socket.addEventListener("open",()=>{
      if (socket !== liveTradeSocket) return;

      liveTradeBackoff = 500;

      if (fallbackTimer) {
        clearInterval(fallbackTimer);
        fallbackTimer = null;
      }

      setSource(
        "dotChart",
        "chartState",
        "LIVE",
        ["LIVE","READY"]
      );

      $("chartMode").textContent =
        "LIVE ON-CHAIN · " +
        timeframeLabel() +
        " · WAITING FOR TRADES";

      // History is loaded by selectToken/setTimeframe; do not launch a second request here.
    });

    socket.addEventListener("message",(ev)=>{
      if (socket !== liveTradeSocket) return;

      try {
        const msg = JSON.parse(ev.data);

        if (msg.type === "status") {
          if (msg.state === "LIVE") {
            setSource(
              "dotChart",
              "chartState",
              "LIVE",
              ["LIVE","READY"]
            );
          }
          return;
        }

        if (msg.type === "trade" && msg.trade) {
          applyLiveTrade(msg.trade,true);
        }
      } catch {}
    });

    socket.addEventListener("close",()=>{
      if (socket !== liveTradeSocket) return;

      liveTradeSocket = null;

      setSource(
        "dotChart",
        "chartState",
        "RECONNECTING",
        ["LIVE","READY","CONNECTING"]
      );

      $("chartMode").textContent =
        "RECONNECTING LIVE FEED…";

      scheduleReconnect();
    });

    socket.addEventListener("error",()=>{
      if (socket !== liveTradeSocket) return;

      setSource(
        "dotChart",
        "chartState",
        "RECONNECTING",
        ["LIVE","READY","CONNECTING"]
      );
    });
  } catch {
    scheduleReconnect();
  }
}

async function setTimeframe(tf) {
  const requested =
    typeof tf === "string"
      ? tf
      : String(tf);

  if (![
    "1s",
    "1m",
    "5m",
    "15m",
    "1h"
  ].includes(requested)) {
    return;
  }

  if (requested === chartInterval) {
    return;
  }

  chartInterval = requested;

  chartTimeframe =
    requested === "1s" ? 1 :
    requested === "1m" ? 1 :
    requested === "5m" ? 5 :
    requested === "15m" ? 15 :
    60;

  if (chart) {
    chart.applyOptions({
      timeScale:{
        timeVisible:true,
        secondsVisible:requested === "1s"
      }
    });
  }

  historyGeneration++;
  liveTradeCacheGeneration = historyGeneration;
  historyBusy = false;
  historyHasMore = true;
  historyNextOffset = 0;
  historyBarsLoaded = 0;
  lastIndicatorCandleTime = 0;
  lastIndicatorState = "WAIT";

  document.querySelectorAll(".tf").forEach(btn=>{
    btn.classList.toggle(
      "active",
      btn.dataset.tf === chartInterval
    );
  });

  if (chartInterval === "1s") {
    // Paint any already-captured real trades immediately. The authoritative
    // history request below then replaces this preview with actual 1-second
    // Pump.fun trade buckets.
    selectedCandles = aggregateLiveTrades(
      selectedTrades,
      "1s"
    );

    livePreviewActive = true;
    historyBarsLoaded = selectedCandles.length;

    if (selectedCandles.length) {
      renderChart(selectedCandles,true);
    }

    $("chartMode").textContent =
      selectedCandles.length
        ? "LOADING PUMP.FUN · 1s · HISTORY"
        : "LOADING PUMP.FUN · 1s · HISTORY…";

    $("historyStatus").textContent =
      selectedCandles.length
        ? selectedCandles.length.toLocaleString() + " live bars"
        : "loading…";

    fetchInitialHistory().catch(()=>{});
    // The normal recovery poll already seeds recent trades when the live
    // websocket is unavailable. Avoid re-applying the same trades here after
    // historical 1s bars have just been loaded.
    return;
  }

  // Keep the last visible chart while the requested Pump.fun timeframe loads.
  $("chartMode").textContent =
    "LOADING PUMP.FUN " + timeframeLabel() + " HISTORY…";

  $("historyStatus").textContent = "loading…";

  fetchInitialHistory().catch(()=>{});
}
function showAll() {
  if (!chart) return;

  chart.timeScale().fitContent();

  $("chartLive").classList.remove("active");
  $("chartAll").classList.add("active");
}

function showLive() {
  if (!chart) return;

  chart.timeScale().scrollToRealTime();

  $("chartAll").classList.remove("active");
  $("chartLive").classList.add("active");
}

async function analyzeSelected() {
  if (!selectedMint || analysisBusy) return;

  analysisBusy = true;
  $("analyzeButton").textContent = "ANALYZING…";

  const mintAtStart = selectedMint;
  let lastError = null;
  let data = null;

  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const r = await fetch(
          "/api/analyze",
          {
            method:"POST",
            headers:{"Content-Type":"application/json"},
            body:JSON.stringify({
              mint:mintAtStart,
              include_x:false
            }),
            cache:"no-store"
          }
        );

        if (
          r.ok ||
          (r.status < 500 && r.status !== 429)
        ) {
          data = await readJsonResponse(r);
          break;
        }

        lastError = new Error(
          "Analysis service returned HTTP " + r.status + "."
        );
      } catch (e) {
        lastError = e;
      }

      await new Promise(
        resolve => setTimeout(resolve, 700)
      );
    }

    if (
      !data ||
      mintAtStart !== selectedMint
    ) {
      if (mintAtStart === selectedMint) {
        $("safetyStatus").textContent =
          "RISK DATA UNAVAILABLE";
        $("safetyStatus").className =
          "safetyStatus unknown";
        $("safetyDetail").textContent =
          lastError?.message ||
          "Risk sources did not respond. Press ANALYZE to retry.";
        $("safetyPercent").textContent = "—";
        $("rugRiskPercent").textContent = "—";
        $("riskConfidence").textContent = "0";
        $("safetyChecks").innerHTML =
          '<div class="empty">Risk checks unavailable right now.</div>';
      }
      return;
    }

    renderSafety(data);
    renderSecurity(data);

    const overview =
      data.overview &&
      typeof data.overview === "object"
        ? data.overview
        : {};

    const asset =
      data.asset &&
      typeof data.asset === "object"
        ? data.asset
        : {};

    const meta = asset.token_info || {};

    selectedInfo.symbol =
      cleanSymbol(
        overview.symbol ||
        meta.symbol ||
        selectedInfo.symbol ||
        "TOKEN"
      );

    selectedInfo.name =
      overview.name ||
      selectedInfo.name ||
      "";

    $("selectedTitle").textContent =
      "$" + selectedInfo.symbol;

    $("selectedMint").textContent =
      selectedMint;

    $("securityState").textContent =
      data.security_gate?.label ||
      "UNKNOWN";

    setSource(
      "dotRug",
      "rugState",
      data?.sources?.RugCheck === "READY"
        ? "READY"
        : "LIMITED",
      ["READY"]
    );
  } catch(e) {
    if (mintAtStart === selectedMint) {
      $("safetyStatus").textContent =
        "RISK DATA UNAVAILABLE";
      $("safetyStatus").className =
        "safetyStatus unknown";
      $("safetyDetail").textContent =
        e.message ||
        "Risk sources did not respond. Press ANALYZE to retry.";
      $("safetyPercent").textContent = "—";
      $("rugRiskPercent").textContent = "—";
      $("riskConfidence").textContent = "0";
      $("safetyChecks").innerHTML =
        '<div class="empty">Risk checks unavailable right now.</div>';
    }
  } finally {
    analysisBusy = false;
    $("analyzeButton").textContent =
      "ANALYZE";
  }
}
async function selectToken(mint) {
  if (!mint) return;

  disconnectLiveTrade();

  selectedMint = mint;
  chartHistoryLoadedAtSec = 0;
  selectedCandles = [];
  lastRenderedCandleTime = 0;
  selectedMinuteCandles = [];
  selectedMinuteSource = "UNKNOWN";
  selectedMinuteGeneration = 0;
  lastIndicatorCandleTime = 0;
  lastIndicatorState = "WAIT";
  selectedTrades = [];
  selectedTradeKeys = new Set();
  selectedLiveUsdPrice = 0;
  selectedLiveUsdAt = 0;
  livePreviewActive = false;
  selectedSupply = 0;
  selectedMarketCap = 0;
  selectedMarketCapUsd = 0;
  selectedMarketCapSol = 0;
  chartMcFactor = 0;
  chartDisplayMode = "PRICE";
  $("chartPrice")?.classList.add("active");
  $("chartMc")?.classList.remove("active");
  $("chartMc")?.setAttribute("disabled","disabled");
  $("activePriceLabel").textContent = "ACTIVE PRICE";
  selectedInfo =
    mergedCandidates().find(x=>x.mint===mint) ||
    {mint};

  historyGeneration++;
  historyBusy = false;
  historyHasMore = true;
  historyNextOffset = 0;
  historyBarsLoaded = 0;
  chartTimeframe = 1;
  chartInterval = "1m";

  if (chart) {
    chart.applyOptions({
      timeScale:{
        timeVisible:true,
        secondsVisible:false
      }
    });
  }

  document.querySelectorAll(".tf").forEach(btn=>{
    btn.classList.toggle(
      "active",
      btn.dataset.tf === "1m"
    );
  });

  $("selectedTitle").textContent =
    "$" +
    cleanSymbol(
      selectedInfo.symbol || "TOKEN"
    );

  $("selectedMint").textContent = mint;

  // Hard-reset the chart series so the previous token can never bleed into
  // the newly selected token while its history is loading.
  if (chartInitialized) {
    candleSeries.setData([]);
    volumeSeries.setData([]);
    ema9Series.setData([]);
    ema21Series.setData([]);
    markersApi.setMarkers([]);
  }
  chartDataSource = "LOADING";

  $("securityRows").innerHTML =
    '<div class="empty">Checking security in background…</div>';

  $("securityBadge").textContent = "CHECKING";
  $("securityBadge").className =
    "miniBadge checking";

  $("signalText").textContent = "WAIT";
  $("signalText").className =
    "signalText wait";

  $("signalBadge").textContent = "WAIT";
  $("signalBadge").className =
    "signal wait";

  $("signalReason").textContent =
    "Connecting to the live on-chain feed…";

  $("safetyPercent").textContent = "—";
  $("rugRiskPercent").textContent = "—";
  $("riskConfidence").textContent = "—";
  $("safetyStatus").textContent = "WAITING FOR DATA";
  $("safetyStatus").className = "safetyStatus unknown";
  $("safetyDetail").textContent = "Checking authorities, holders, liquidity and independent token-risk data…";
  $("safetyChecks").innerHTML = '<div class="empty">Running safety checks in background…</div>';

  $("chartMode").textContent =
    "LOADING PUMP.FUN 1m HISTORY…";

  $("historyStatus").textContent =
    "loading…";

  $("activePrice").textContent = "—";
  $("activeAge").textContent = "—";

  // Do not use the scanner/asset price as chart OHLC. The live trade stream
  // and native Pump.fun candle feed are the chart's price authority.

  renderCandidates();

  // Open the live stream first so a trade cannot happen while history is
  // loading without being captured.
  connectLiveTrade(mint);
  connectNativePumpFunTrades(mint);
  startCurrentCandleSync();
  startLivePricePoll();
  startLiveTradeCachePoll();
  loadChartMeta(mint);
  setTimeout(() => {
    if (
      selectedMint === mint &&
      !(chartMcFactor > 0)
    ) {
      loadChartMeta(mint);
    }
  }, 1800);

  // Paint real chart data first. The heavier security/risk analysis starts
  // immediately after the first chart request has had a chance to render.
  await fetchInitialHistory();

  if (selectedMint === mint) {
    setTimeout(() => {
      if (selectedMint === mint) analyzeSelected();
    }, 0);
  }
}

function startPumpFeed() {
  if (pumpSocket) return;

  try {
    pumpSocket =
      new WebSocket(
        "wss://pumpportal.fun/api/data"
      );

    pumpSocket.addEventListener("open",()=>{
      $("pumpState").textContent = "LIVE";

      setSource(
        "dotPump",
        "pumpState",
        "LIVE",
        ["LIVE"]
      );

      pumpSocket.send(
        JSON.stringify({
          method:"subscribeNewToken"
        })
      );
    });

    pumpSocket.addEventListener("message",ev=>{
      try {
        const raw = JSON.parse(ev.data);
        const item = normalizePumpEvent(raw);

        if (!item) return;

        const old =
          pumpEvents.find(
            x=>x.mint === item.mint
          );

        if (old) {
          Object.assign(old,item,{
            ts:old.ts
          });
        } else {
          pumpEvents.unshift(item);
        }

        pumpEvents = pumpEvents.slice(0,80);
        renderCandidates();
      } catch {}
    });

    pumpSocket.addEventListener("close",()=>{
      $("pumpState").textContent =
        "RECONNECTING";

      setTimeout(
        startPumpFeed,
        3000
      );
    });

    pumpSocket.addEventListener("error",()=>{
      $("pumpState").textContent =
        "UNAVAILABLE";
    });
  } catch {
    $("pumpState").textContent =
      "UNAVAILABLE";
  }
}

async function refreshMarketCandidates() {
  try {
    const r = await fetch(
      "/api/discover?t=" +
      Date.now(),
      {cache:"no-store"}
    );

    const j = await r.json();

    marketCandidates =
      j.candidates || [];

    renderCandidates();
  } catch {}
}

document.addEventListener("DOMContentLoaded",()=>{
  initChart();

  $("openMint").addEventListener("click",()=>{
    const mint =
      $("mintInput").value.trim();

    if (mint) selectToken(mint);
  });

  $("mintInput").addEventListener(
    "keydown",
    e=>{
      if (e.key === "Enter") {
        $("openMint").click();
      }
    }
  );

  $("analyzeButton").addEventListener(
    "click",
    analyzeSelected
  );

  $("scanNow").addEventListener(
    "click",
    async()=>{
      await refreshMarketCandidates();
      renderCandidates();
    }
  );

  document
    .querySelectorAll(".tf")
    .forEach(btn=>{
      btn.addEventListener(
        "click",
        ()=>setTimeframe(
          btn.dataset.tf
        )
      );
    });

  $("chartAll").addEventListener(
    "click",
    showAll
  );

  $("chartLive").addEventListener(
    "click",
    showLive
  );

  $("chartPrice").addEventListener(
    "click",
    ()=>setChartDisplayMode("PRICE")
  );

  $("chartMc").addEventListener(
    "click",
    ()=>setChartDisplayMode("MC")
  );

  health();
  refreshMarketCandidates();
  startPumpFeed();

  setInterval(health,15000);
  setInterval(
    refreshMarketCandidates,
    30000
  );

  window.addEventListener("resize",()=>{
    if (chart) chart.applyOptions({});
  });
});
