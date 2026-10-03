const $ = (id) => document.getElementById(id);

let pumpSocket = null;
let liveTradeSocket = null;
let fallbackTimer = null;
let reconnectTimer = null;
let pricePollTimer = null;
let currentCandleSyncTimer = null;
let currentCandleSyncInFlight = false;
let currentCandleSyncQueued = false;

let pumpEvents = [];
let marketCandidates = [];
let selectedMint = "";
let selectedInfo = {};
let selectedCandles = [];
let selectedTrades = [];
let analysisBusy = false;

let chart = null;
let candleSeries = null;
let volumeSeries = null;
let ema9Series = null;
let ema21Series = null;
let markersApi = null;
let chartInitialized = false;

let chartTimeframe = 1;
let chartDataSource = "PUMP.FUN";
let historyGeneration = 0;
let historyBusy = false;
let historyBusyGeneration = 0;
let initialHistoryBusy = false;
let initialHistoryGeneration = 0;
let historyHasMore = true;
let historyNextOffset = 0;
let historyBarsLoaded = 0;

let renderScheduled = false;
let liveTradeBackoff = 500;

const PAGE_SIZE = 30;
const MAX_HISTORY_BARS = 120;

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

function timeframeLabel(tf = chartTimeframe) {
  return tf === 1 ? "1m" : tf === 5 ? "5m" : tf === 15 ? "15m" : "1h";
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

function signalFromCandles(candles, endIndex = candles.length - 1) {
  if (!candles.length || endIndex < 0) {
    return {
      state:"WAIT",
      reason:"Waiting for live on-chain trades…",
      rsi:null,
      ema9:null,
      ema21:null,
      mean:null,
      pressure:null,
      score:0
    };
  }

  const closes = candles
    .slice(0, endIndex + 1)
    .map(x=>Number(x.c))
    .filter(Number.isFinite);

  if (closes.length < 21) {
    return {
      state:"WAIT",
      reason:"Building 21 candles for confirmation…",
      rsi:null,
      ema9:null,
      ema21:null,
      mean:null,
      pressure:null,
      score:0
    };
  }

  const p = closes[closes.length - 1];
  const e9 = ema(closes,9);
  const e21 = ema(closes,21);
  const r = rsi(closes,14);

  const tail = closes.slice(-30);
  const m = tail.reduce((a,b)=>a+b,0) / tail.length;

  const recent = closes.slice(-12);

  let up = 0;
  let down = 0;

  for (let i=1;i<recent.length;i++) {
    if (recent[i] > recent[i-1]) up++;
    else if (recent[i] < recent[i-1]) down++;
  }

  const moves = up + down;
  const pressure = moves ? up / moves : 0.5;
  const momentum = closes.length >= 5
    ? (p / closes[closes.length - 5] - 1) * 100
    : 0;

  let score = 50;

  if (e9 != null && e21 != null) score += e9 > e21 ? 16 : -16;

  if (r != null) {
    if (r >= 50 && r <= 72) score += 12;
    else if (r < 42) score -= 12;
    else if (r > 78) score -= 8;
  }

  score += p >= m ? 9 : -9;

  if (pressure >= 0.60) score += 9;
  if (pressure <= 0.40) score -= 9;

  if (momentum > 1.0) score += 8;
  if (momentum < -1.0) score -= 8;

  score = Math.round(clamp(score,0,100));

  let state = "WAIT";

  if (e9 != null && e21 != null && r != null) {
    if (
      score >= 65 &&
      e9 > e21 &&
      p > m &&
      r >= 50 &&
      r <= 72 &&
      pressure >= 0.55
    ) {
      state = "BUY";
    } else if (
      score <= 35 &&
      e9 < e21 &&
      p < m &&
      r <= 50 &&
      pressure <= 0.45
    ) {
      state = "SELL";
    }
  }

  let reason = "Mixed conditions — wait for confirmation.";

  if (state === "BUY") {
    reason =
      "Bullish " + timeframeLabel() +
      " trend: EMA 9 > EMA 21, price is above the mean, and upside pressure leads.";
  }

  if (state === "SELL") {
    reason =
      "Bearish " + timeframeLabel() +
      " trend: EMA 9 < EMA 21, price is below the mean, and downside pressure leads.";
  }

  return {
    state,
    reason,
    rsi:r,
    ema9:e9,
    ema21:e21,
    mean:m,
    pressure,
    momentum,
    score
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
}

function initChart() {
  if (chartInitialized) return true;

  if (!window.LightweightCharts) {
    $("chartMode").textContent = "Chart library failed to load.";
    return false;
  }

  const container = $("chart");

  chart = LightweightCharts.createChart(container,{
    autoSize:true,

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
      mode:LightweightCharts.CrosshairMode.Normal
    },

    rightPriceScale:{
      borderColor:"#203140",
      scaleMargins:{top:0.06,bottom:0.18},
      autoScale:true,
      alignLabels:true,
      mode: LightweightCharts.PriceScaleMode?.Logarithmic ?? 0
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
      priceFormatter:(price)=>safe(price,12)
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
      priceScaleId:""
    }
  );

  chart.priceScale("").applyOptions({
    scaleMargins:{top:0.82,bottom:0},
    borderVisible:false
  });

  markersApi = LightweightCharts.createSeriesMarkers(
    candleSeries,
    []
  );

  chartInitialized = true;
  return true;
}

function normalizeCandle(x) {
  if (!x) return null;

  const ts = Number(x.ts ?? x.timestamp ?? x.time);
  const o = Number(x.o ?? x.open);
  const h = Number(x.h ?? x.high);
  const l = Number(x.l ?? x.low);
  const c = Number(x.c ?? x.close);
  const v = Number(x.v ?? x.volume ?? 0);

  if (!Number.isFinite(ts) ||
      ![o,h,l,c].every(Number.isFinite)) {
    return null;
  }

  const time = Math.floor(
    ts > 2e10 ? ts / 1000 : ts
  );

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

function aggregateCandles(source, tfMinutes) {
  if (tfMinutes === 1) {
    return source.map(x=>({...x}));
  }

  const span = tfMinutes * 60;
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
    signal.mean == null || !selectedCandles.length
      ? "—"
      : pct(
          (selectedCandles[selectedCandles.length - 1].c /
          signal.mean - 1) * 100
        );

  $("metricPressure").textContent =
    signal.pressure == null
      ? "—"
      : pct(signal.pressure * 100,0);

  $("metricConfirm").textContent =
    signal.score + "/100";
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
  $("activePrice").textContent = safe(price,12);
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
      '<b>' + safe(x.price,12) + '</b>' +
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
  if (!initChart() || !candles.length) return;

  candleSeries.setData(
    candles.map(x=>({
      time:x.time,
      open:x.o,
      high:x.h,
      low:x.l,
      close:x.c
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

  ema9Series.setData(ind.ema9);
  ema21Series.setData(ind.ema21);
  markersApi.setMarkers(buildMarkers(candles));

  const signal = signalFromCandles(candles);
  renderSignal(signal);

  if (fit) {
    chart.timeScale().fitContent();
  }

  const last = candles[candles.length - 1];

  updateActivePrice(last.c);
  $("chartMode").textContent =
    chartDataSource + " · " + timeframeLabel() +
    " CANDLES · " + candles.length + " BARS";

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
    open:candle.o,
    high:candle.h,
    low:candle.l,
    close:candle.c
  });

  volumeSeries.update({
    time:candle.time,
    value:Math.max(0,candle.v || 0),
    color:candle.c >= candle.o
      ? "rgba(57,220,137,.22)"
      : "rgba(255,101,117,.22)"
  });
}

function scheduleLiveRender() {
  if (renderScheduled) return;

  renderScheduled = true;

  requestAnimationFrame(()=>{
    renderScheduled = false;

    if (!selectedCandles.length || !candleSeries) return;

    const last = selectedCandles[selectedCandles.length - 1];

    updateLiveCandleOnSeries(last);

    const ind = indicatorSeries(selectedCandles);
    ema9Series.setData(ind.ema9);
    ema21Series.setData(ind.ema21);

    markersApi.setMarkers(buildMarkers(selectedCandles));

    const signal = signalFromCandles(selectedCandles);
    renderSignal(signal);

    updateActivePrice(
      last.c,
      Date.now()
    );

    $("historyStatus").textContent =
      historyBarsLoaded.toLocaleString() + " bars";

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
async function fetchPage(offset, generation) {
  if (
    !selectedMint ||
    generation !== historyGeneration
  ) {
    return {candles:[],hasMore:false};
  }

  try {
    const controller = new AbortController();
    const timer = setTimeout(()=>controller.abort(), 20000);

    const r = await fetch(
      "/api/chart?mint=" +
      encodeURIComponent(selectedMint) +
      "&limit=" + PAGE_SIZE +
      "&offset=" + offset +
      "&timeframe=" + chartTimeframe +
      "&t=" + Date.now(),
      {
        cache:"no-store",
        signal:controller.signal
      }
    );

    clearTimeout(timer);

    if (!r.ok) {
      return {
        candles:[],
        hasMore:false,
        source:"ERROR",
        error:"HTTP " + r.status
      };
    }

    const j = await readJsonResponse(r);

    const candles = (j.candles || [])
      .map(normalizeCandle)
      .filter(Boolean);

    return {
      candles,
      hasMore:Boolean(j.has_more) && candles.length >= PAGE_SIZE,
      source:String(j.source || "MARKET DATA"),
      diagnostics:j.diagnostics || null
    };
  } catch (err) {
    return {
      candles:[],
      hasMore:false,
      source:"ERROR",
      error:err?.name === "AbortError"
        ? "CHART_REQUEST_TIMEOUT"
        : String(err?.message || err)
    };
  }
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

  try {
    $("chartMode").textContent =
      "LOADING REAL MARKET HISTORY…";

    $("historyStatus").textContent = "loading…";

    const page = await fetchPage(0,generation);

    if (
      generation !== historyGeneration ||
      !selectedMint
    ) {
      return false;
    }

    chartDataSource = page.source || "MARKET DATA";

    if (page.diagnostics?.sources) {
      const parts = Object.entries(page.diagnostics.sources)
        .map(([name,info]) => name + ": " + Number(info.bars || 0) + " bars")
        .join(" · ");
      $("chartMode").textContent =
        (page.source || "MARKET DATA") +
        " · " +
        parts;
    }

    if (!page.candles.length) {
      $("chartMode").textContent =
        page.error
          ? "CHART HISTORY ERROR · " + page.error
          : "NO HISTORICAL CANDLES · WAITING FOR REAL LIVE TRADES";
      $("historyStatus").textContent = "0 bars";
      $("chartState").textContent =
        page.error ? "ERROR" : "WAITING";
      return false;
    }

    if (selectedCandles.length) {
      mergePage(page.candles);
    } else {
      selectedCandles = page.candles
        .sort((a,b)=>a.time-b.time)
        .slice(-MAX_HISTORY_BARS);
    }

    historyBarsLoaded = selectedCandles.length;
    historyNextOffset = PAGE_SIZE;
    historyHasMore = page.hasMore;

    renderChart(selectedCandles,true);

    // Older native Pump.fun pages are loaded without blocking the visible chart.
    loadOlderHistory(generation);

    return true;
  } finally {
    if (initialHistoryGeneration === generation) {
      initialHistoryBusy = false;
    }
  }
}
async function loadOlderHistory(generation) {
  const busyForSameGeneration =
    historyBusy &&
    historyBusyGeneration === generation;

  if (
    busyForSameGeneration ||
    !historyHasMore ||
    generation !== historyGeneration
  ) return;

  historyBusy = true;
  historyBusyGeneration = generation;

  try {
    while (
      historyHasMore &&
      selectedCandles.length < MAX_HISTORY_BARS &&
      generation === historyGeneration
    ) {
      const offsets = [
        historyNextOffset,
        historyNextOffset + PAGE_SIZE,
        historyNextOffset + PAGE_SIZE * 2
      ];

      const results = await Promise.all(
        offsets.map(x=>fetchPage(x,generation))
      );

      let received = 0;
      let anyMore = false;

      for (const result of results) {
        if (generation !== historyGeneration) break;

        if (result.candles.length) {
          mergePage(result.candles);
          received += result.candles.length;
        }

        if (result.hasMore) {
          anyMore = true;
        }
      }

      if (generation !== historyGeneration) break;

      historyNextOffset += PAGE_SIZE * 3;
      historyHasMore = anyMore && received > 0;

      $("historyStatus").textContent =
        historyBarsLoaded.toLocaleString() +
        (historyHasMore ? "+ bars" : " bars");

      renderChart(selectedCandles,false);

      if (!received) break;

      await new Promise(requestAnimationFrame);
    }
  } finally {
    if (historyBusyGeneration === generation) {
      historyBusy = false;
    }
  }
}
function applyLivePrice(price, timestampMs = Date.now(), recordTrade = null) {
  price = Number(price);
  if (!Number.isFinite(price) || price <= 0 || !selectedMint) return;

  // Live websocket trades are used for immediate UI/tape updates. They are
  // NOT used to fabricate/modify OHLC because Pump.fun's native candle feed
  // defines the chart price exactly.
  updateActivePrice(
    price,
    Number(timestampMs || Date.now())
  );

  if (recordTrade) {
    if (!selectedTrades.some(x => x.id === recordTrade.id)) {
      selectedTrades.push(recordTrade);
      if (selectedTrades.length > 500) {
        selectedTrades.shift();
      }
    }

    $("lastUpdate").textContent =
      new Date(recordTrade.time * 1000)
        .toLocaleTimeString();
  }

  scheduleLiveRender();
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

  // Show the trade instantly in the tape/header, then pull the authoritative
  // Pump.fun candle so the displayed OHLC matches the live chart.
  applyLivePrice(
    t.price,
    t.time * 1000,
    record ? t : null
  );

  requestCurrentCandleSync();
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
      updateActivePrice(
        price,
        Date.now()
      );
    }
  } catch {}
}

async function syncCurrentPumpCandle() {
  if (!selectedMint) return;

  if (currentCandleSyncInFlight) {
    currentCandleSyncQueued = true;
    return;
  }

  currentCandleSyncInFlight = true;

  try {
    const generation = historyGeneration;

    const r = await fetch(
      "/api/chart?mint=" +
      encodeURIComponent(selectedMint) +
      "&limit=30&offset=0&timeframe=" +
      chartTimeframe +
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

    mergePage(candles);
    chartDataSource = String(
      j.source || chartDataSource
    );

    renderChart(
      selectedCandles,
      false
    );
  } catch {
    // Keep the live websocket/tape alive if the reconciliation request
    // temporarily fails. The one-second safety sync will try again.
  } finally {
    currentCandleSyncInFlight = false;

    if (currentCandleSyncQueued) {
      currentCandleSyncQueued = false;
      requestCurrentCandleSync(0);
    }
  }
}

function requestCurrentCandleSync(delay = 75) {
  if (!selectedMint) return;

  if (currentCandleSyncTimer) {
    clearTimeout(currentCandleSyncTimer);
  }

  currentCandleSyncTimer = setTimeout(() => {
    currentCandleSyncTimer = null;
    syncCurrentPumpCandle();
  }, Math.max(0,delay));
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

  const tick = () => {
    if (!selectedMint) return;

    syncCurrentPumpCandle();

    currentCandleSyncTimer = setTimeout(
      tick,
      1000
    );
  };

  tick();
}
function disconnectLiveTrade() {
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

  if (currentCandleSyncTimer) {
    clearTimeout(currentCandleSyncTimer);
    currentCandleSyncTimer = null;
  }

  if (liveTradeSocket) {
    try {
      liveTradeSocket.close();
    } catch {}
    liveTradeSocket = null;
  }
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
  disconnectLiveTrade();

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
  tf = Number(tf);

  if (![1,5,15,60].includes(tf)) return;
  if (tf === chartTimeframe && selectedCandles.length) return;

  chartTimeframe = tf;
  historyGeneration++;

  historyBusy = false;
  historyHasMore = true;
  historyNextOffset = 0;
  historyBarsLoaded = 0;
  selectedCandles = [];

  document.querySelectorAll(".tf").forEach(btn=>{
    btn.classList.toggle(
      "active",
      Number(btn.dataset.tf) === chartTimeframe
    );
  });

  $("chartMode").textContent =
    "LOADING " + timeframeLabel() + " HISTORY…";
  $("historyStatus").textContent = "loading…";

  await fetchInitialHistory();
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
  selectedCandles = [];
  selectedTrades = [];
  selectedInfo =
    mergedCandidates().find(x=>x.mint===mint) ||
    {mint};

  historyGeneration++;
  historyBusy = false;
  historyHasMore = true;
  historyNextOffset = 0;
  historyBarsLoaded = 0;
  chartTimeframe = 1;

  document.querySelectorAll(".tf").forEach(btn=>{
    btn.classList.toggle(
      "active",
      Number(btn.dataset.tf) === 1
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

  if (Number(selectedInfo.price) > 0) {
    updateActivePrice(Number(selectedInfo.price), Date.now());
  }

  renderCandidates();

  // Open the live stream first so a trade cannot happen while history is
  // loading without being captured.
  connectLiveTrade(mint);
  startCurrentCandleSync();

  // Risk/security runs independently of chart history so a slow market-data
  // request can never prevent the rug-risk panel from rendering.
  analyzeSelected();

  await fetchInitialHistory();
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
          Number(btn.dataset.tf)
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
