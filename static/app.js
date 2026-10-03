const $ = (id) => document.getElementById(id);

let pumpSocket = null;
let liveTradeSocket = null;
let fallbackTimer = null;
let liveTradeBackoff = 500;
let pumpEvents = [];
let marketCandidates = [];
let selectedMint = "";
let selectedInfo = {};
let selectedCandles = [];
let selectedTrades = [];
let analysisBusy = false;
let chartInitialized = false;
let firstChartLoad = true;
let chartBusy = false;
let renderScheduled = false;

let chart = null;
let candleSeries = null;
let volumeSeries = null;
let ema9Series = null;
let ema21Series = null;
let markersApi = null;

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

function cleanSymbol(v) {
  return String(v || "TOKEN").replace(/^\$+/, "").slice(0, 24);
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
    const liveState = j.sources?.LiveTrade?.state || (liveTradeSocket?.readyState === WebSocket.OPEN ? "LIVE" : "READY");
    const liveGood = ["LIVE","CONNECTING","READY"].includes(liveState);
    setSource("dotChart", "chartState", liveState, liveGood ? ["LIVE","CONNECTING","READY"] : []);
    setSource("dotHelius", "securityState", j.sources?.Security?.state || "UNKNOWN", ["READY"]);
    setSource("dotX", "xState", j.sources?.X?.state || "UNKNOWN", ["READY","CONFIGURED"]);
  } catch {
    $("health").textContent = "BACKEND ERROR";
  }
}

function ema(values, period) {
  if (values.length < period) return null;
  let out = values.slice(0, period).reduce((a,b)=>a+b,0) / period;
  const alpha = 2 / (period + 1);
  for (let i = period; i < values.length; i++) out = (values[i] - out) * alpha + out;
  return out;
}

function rsi(values, period = 14) {
  if (values.length < period + 1) return null;
  let gain = 0, loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = values[i] - values[i-1];
    gain += Math.max(0, d);
    loss += Math.max(0, -d);
  }
  let avgGain = gain / period;
  let avgLoss = loss / period;
  for (let i = period + 1; i < values.length; i++) {
    const d = values[i] - values[i-1];
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
      rsi:null, ema9:null, ema21:null, mean:null, pressure:null, score:0
    };
  }

  const closes = candles.slice(0, endIndex + 1).map(x=>Number(x.c)).filter(Number.isFinite);
  if (closes.length < 21) {
    return {
      state:"WAIT",
      reason:"Building 21 one-minute candles for confirmation…",
      rsi:null, ema9:null, ema21:null, mean:null, pressure:null, score:0
    };
  }

  const p = closes[closes.length - 1];
  const e9 = ema(closes, 9);
  const e21 = ema(closes, 21);
  const r = rsi(closes, 14);
  const tail = closes.slice(-30);
  const m = tail.reduce((a,b)=>a+b,0) / tail.length;

  const recent = closes.slice(-12);
  let up = 0, down = 0;
  for (let i=1;i<recent.length;i++) {
    if (recent[i] > recent[i-1]) up++;
    else if (recent[i] < recent[i-1]) down++;
  }

  const moves = up + down;
  const pressure = moves ? up / moves : 0.5;
  const momentum = closes.length >= 5 ? (p / closes[closes.length - 5] - 1) * 100 : 0;

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
    if (score >= 65 && e9 > e21 && p > m && r >= 50 && r <= 72 && pressure >= 0.55) {
      state = "BUY";
    } else if (score <= 35 && e9 < e21 && p < m && r <= 50 && pressure <= 0.45) {
      state = "SELL";
    }
  }

  let reason = "Mixed conditions — wait for confirmation.";
  if (state === "BUY") reason = "1m trend is bullish: EMA 9 > EMA 21, price is above the mean, and upside pressure leads.";
  if (state === "SELL") reason = "1m trend is bearish: EMA 9 < EMA 21, price is below the mean, and downside pressure leads.";

  return {state,reason,rsi:r,ema9:e9,ema21:e21,mean:m,pressure,momentum,score};
}

function launchScore(e) {
  const age = Math.max(0, (Date.now() - e.ts) / 1000);
  const freshness = clamp(25 - age / 12, 0, 25);
  const progress = Number(e.vSolInBondingCurve || 0);
  const progressScore = progress > 0 ? clamp(30 - Math.abs(progress - 46) * 0.55, 0, 30) : 10;
  const mcap = Number(e.marketCapSol || 0);
  const mcapScore = clamp(Math.log10(Math.max(1,mcap)) * 8, 0, 25);
  const initialBuy = Number(e.initialBuy || 0);
  const buyScore = initialBuy > 0 ? clamp(Math.log10(initialBuy + 1) * 3, 0, 15) : 5;
  return Math.round(clamp(freshness + progressScore + mcapScore + buyScore,0,100));
}

function normalizePumpEvent(raw) {
  const mint = raw.mint || raw.tokenAddress;
  if (!mint) return null;
  return {
    mint,
    symbol: cleanSymbol(raw.symbol || raw.tokenSymbol || "NEW"),
    name: raw.name || raw.tokenName || "",
    creator: raw.traderPublicKey || raw.creator || "",
    marketCapSol: Number(raw.marketCapSol || 0),
    vSolInBondingCurve: Number(raw.vSolInBondingCurve || 0),
    initialBuy: Number(raw.initialBuy || 0),
    ts: Date.now(),
    source:"PUMP.FUN",
    score:0
  };
}

function mergedCandidates() {
  const map = new Map();

  for (const e of pumpEvents) {
    const row = {...e, symbol:cleanSymbol(e.symbol), score:launchScore(e)};
    map.set(e.mint,row);
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
    if (!old || row.score > old.score) map.set(row.mint,row);
  }

  return [...map.values()].sort((a,b)=>b.score-a.score).slice(0,8);
}

function renderCandidates() {
  const rows = mergedCandidates();
  const el = $("candidateList");

  if (!rows.length) {
    el.innerHTML = '<div class="empty">Waiting for live Pump.fun launches…</div>';
    return;
  }

  el.innerHTML = rows.map((x,i)=>{
    const progress = x.vSolInBondingCurve ? clamp((x.vSolInBondingCurve/85)*100,0,100) : null;
    const stat = x.marketCapSol
      ? "MC " + safe(x.marketCapSol,1) + " SOL"
      : x.liquidity
        ? "Liq " + usd(x.liquidity)
        : "LIVE";

    return '<button class="candidate '+(x.mint===selectedMint?'selected':'')+'" data-mint="'+esc(x.mint)+'">' +
      '<div class="rank">#'+(i+1)+'</div>' +
      '<div class="candidateMain"><b>$'+esc(cleanSymbol(x.symbol))+'</b><span>'+esc(x.mint)+'</span></div>' +
      '<div class="candidateStats"><b>'+x.score+'</b><span>'+esc(stat)+'</span></div>' +
      '<div class="candidateTag">'+esc(x.source || "PUMP.FUN")+'</div>' +
      (progress != null ? '<div class="progress"><i style="width:'+progress+'%"></i></div>' : '') +
      '</button>';
  }).join("");

  el.querySelectorAll("[data-mint]").forEach(btn=>{
    btn.addEventListener("click",()=>selectToken(btn.getAttribute("data-mint")));
  });
}

function initChart() {
  if (chartInitialized) return true;
  if (!window.LightweightCharts) {
    $("chartMode").textContent = "Chart library failed to load.";
    return false;
  }

  const container = $("chart");

  chart = LightweightCharts.createChart(container, {
    autoSize: true,
    layout: {
      background:{type:"solid",color:"#071019"},
      textColor:"#7f90a5",
      fontFamily:"Inter,system-ui,-apple-system,Segoe UI,sans-serif",
    },
    grid:{
      vertLines:{color:"#12212d"},
      horzLines:{color:"#12212d"},
    },
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
    rightPriceScale:{
      borderColor:"#203140",
      scaleMargins:{top:0.08,bottom:0.18},
    },
    timeScale:{
      borderColor:"#203140",
      timeVisible:true,
      secondsVisible:false,
      rightOffset:3,
      barSpacing:7,
    },
    localization:{priceFormatter:(price)=>safe(price,10)},
  });

  candleSeries = chart.addSeries(LightweightCharts.CandlestickSeries, {
    upColor:"#39dc89",
    downColor:"#ff6575",
    borderVisible:false,
    wickUpColor:"#39dc89",
    wickDownColor:"#ff6575",
    priceLineVisible:true,
    lastValueVisible:true,
  });

  ema9Series = chart.addSeries(LightweightCharts.LineSeries, {
    color:"#6ee7a5",
    lineWidth:1,
    crosshairMarkerVisible:false,
    lastValueVisible:false,
    priceLineVisible:false,
  });

  ema21Series = chart.addSeries(LightweightCharts.LineSeries, {
    color:"#ffbd54",
    lineWidth:1,
    crosshairMarkerVisible:false,
    lastValueVisible:false,
    priceLineVisible:false,
  });

  volumeSeries = chart.addSeries(LightweightCharts.HistogramSeries, {
    priceFormat:{type:"volume"},
    priceScaleId:"",
  });

  chart.priceScale("").applyOptions({
    scaleMargins:{top:0.82,bottom:0},
    borderVisible:false,
  });

  markersApi = LightweightCharts.createSeriesMarkers(candleSeries, []);
  chartInitialized = true;

  new ResizeObserver(()=>chart.applyOptions({})).observe(container);
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

  if (!Number.isFinite(ts) || ![o,h,l,c].every(Number.isFinite)) return null;

  const time = Math.floor(ts > 2e10 ? ts / 1000 : ts);
  return {
    time,
    ts:time,
    o,h,l,c,
    v:Math.max(0,v),
  };
}

function normalizeTrade(raw) {
  if (!raw) return null;
  const price = Number(raw.price);
  const ts = Number(raw.timestamp);

  if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(ts)) return null;

  return {
    id:String(raw.id || raw.signature || (Date.now() + ":" + Math.random())),
    time:Math.floor(ts > 2e10 ? ts / 1000 : ts),
    price,
    side:String(raw.side || "BUY").toUpperCase() === "SELL" ? "SELL" : "BUY",
    volumeSol:Number(raw.volume_sol || 0),
    source:raw.source || "ONCHAIN",
    signature:raw.signature || "",
  };
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
    ema9:e9.map((v,i)=>v == null ? null : {time:candles[i].time,value:v}).filter(Boolean),
    ema21:e21.map((v,i)=>v == null ? null : {time:candles[i].time,value:v}).filter(Boolean),
  };
}

function buildMarkers(candles) {
  const markers = [];
  let previous = "WAIT";

  for (let i=0;i<candles.length;i++) {
    const signal = signalFromCandles(candles,i);
    if (signal.state !== "WAIT" && signal.state !== previous) {
      markers.push({
        time:candles[i].time,
        position:signal.state==="BUY" ? "belowBar" : "aboveBar",
        color:signal.state==="BUY" ? "#39dc89" : "#ff6575",
        shape:signal.state==="BUY" ? "arrowUp" : "arrowDown",
        text:signal.state,
        id:signal.state+"-"+candles[i].time,
      });
    }
    previous = signal.state;
  }

  return markers.slice(-24);
}

function updateIndicatorPanel(signal) {
  $("liveRsi").textContent = signal.rsi == null ? "—" : Number(signal.rsi).toFixed(1);
  $("liveEma").textContent = safe(signal.ema9,10) + " / " + safe(signal.ema21,10);
  $("metricRsi").textContent = signal.rsi == null ? "—" : Number(signal.rsi).toFixed(1);
  $("metricEma9").textContent = safe(signal.ema9,10);
  $("metricEma21").textContent = safe(signal.ema21,10);

  $("metricMean").textContent = signal.mean == null || !selectedCandles.length
    ? "—"
    : pct((selectedCandles[selectedCandles.length-1].c / signal.mean - 1) * 100);

  $("metricPressure").textContent = signal.pressure == null ? "—" : pct(signal.pressure * 100,0);
  $("metricConfirm").textContent = signal.score + "/100";
}

function renderSignal(signal) {
  const state = signal.state || "WAIT";

  $("signalBadge").textContent = state;
  $("signalBadge").className = "signal " + state.toLowerCase();
  $("signalText").textContent = state;
  $("signalText").className = "signalText " + state.toLowerCase();
  $("signalReason").textContent = signal.reason || "Waiting for more data.";

  updateIndicatorPanel(signal);
}

function renderTape() {
  const rows = selectedTrades.slice(-12).reverse();

  if (!rows.length) {
    const candles = selectedCandles.slice(-12).reverse();
    if (!candles.length) {
      $("tape").innerHTML = '<div class="empty">No live trades yet.</div>';
      return;
    }

    $("tape").innerHTML = candles.map((x,i)=>{
      const prev = candles[i+1];
      const up = !prev || x.c >= prev.c;
      return '<div class="tick '+(up?'up':'down')+'">' +
        '<span>'+new Date(x.time*1000).toLocaleTimeString()+'</span>' +
        '<b>'+safe(x.c,10)+'</b>' +
        '<i>'+(up?"▲":"▼")+'</i></div>';
    }).join("");
    return;
  }

  $("tape").innerHTML = rows.map(x=>
    '<div class="tick '+(x.side==="BUY"?'up':'down')+'">' +
      '<span>'+new Date(x.time*1000).toLocaleTimeString()+'</span>' +
      '<b>'+safe(x.price,10)+'</b>' +
      '<i>'+(x.side==="BUY"?"BUY":"SELL")+'</i>' +
    '</div>'
  ).join("");

  $("lastUpdate").textContent = new Date(rows[0].time*1000).toLocaleTimeString();
}

function renderSecurity(data) {
  const gate = data?.security_gate || {};
  $("securityBadge").textContent = gate.label || "UNKNOWN";
  $("securityBadge").className = "miniBadge " + String(gate.label || "UNKNOWN").toLowerCase().replace(/[^a-z]+/g,"-");

  const s = data?.security;
  if (!s || typeof s !== "object") {
    $("securityRows").innerHTML = '<div class="empty">Security data unavailable.</div>';
    return;
  }

  $("securityRows").innerHTML =
    '<div class="row"><span>Holder coverage</span><b>'+pct(Number(s.coverage_ratio || 0)*100,0)+'</b></div>' +
    '<div class="row"><span>Top holder</span><b>'+pct(Number(s.top_holder_share || 0)*100,1)+'</b></div>' +
    '<div class="row"><span>Top 10</span><b>'+pct(Number(s.top10_holder_share || 0)*100,1)+'</b></div>' +
    '<div class="row"><span>Mint authority</span><b>'+(s.mint_authority ? "ACTIVE" : "OFF")+'</b></div>' +
    '<div class="row"><span>Freeze authority</span><b>'+(s.freeze_authority ? "ACTIVE" : "OFF")+'</b></div>';
}

function renderChart(candles, fit=false) {
  if (!initChart() || !candles.length) return;

  candleSeries.setData(candles.map(x=>({
    time:x.time,
    open:x.o,
    high:x.h,
    low:x.l,
    close:x.c,
  })));

  volumeSeries.setData(candles.map(x=>({
    time:x.time,
    value:Math.max(0,Number(x.v)||0),
    color:x.c >= x.o ? "rgba(57,220,137,.22)" : "rgba(255,101,117,.22)",
  })));

  const ind = indicatorSeries(candles);
  ema9Series.setData(ind.ema9);
  ema21Series.setData(ind.ema21);
  markersApi.setMarkers(buildMarkers(candles));

  const signal = signalFromCandles(candles);
  renderSignal(signal);

  if (fit) chart.timeScale().fitContent();
  else chart.timeScale().scrollToRealTime();

  const last = candles[candles.length-1];
  $("livePrice").textContent = safe(last.c,10);
  const first = candles[0]?.c;
  $("liveChange").textContent = first ? pct((last.c/first-1)*100) : "—";
  $("chartMode").textContent = "PUMP.FUN 1M OHLC + LIVE ON-CHAIN TRADES · " + candles.length + " BARS";
  setSource("dotChart","chartState","LIVE",["LIVE","READY"]);
  renderTape();
}

function scheduleLiveRender() {
  if (renderScheduled) return;
  renderScheduled = true;

  requestAnimationFrame(()=>{
    renderScheduled = false;

    if (!selectedCandles.length || !candleSeries) return;

    const last = selectedCandles[selectedCandles.length - 1];

    candleSeries.update({
      time:last.time,
      open:last.o,
      high:last.h,
      low:last.l,
      close:last.c,
    });

    volumeSeries.update({
      time:last.time,
      value:Math.max(0,last.v || 0),
      color:last.c >= last.o ? "rgba(57,220,137,.22)" : "rgba(255,101,117,.22)",
    });

    const ind = indicatorSeries(selectedCandles);
    ema9Series.setData(ind.ema9);
    ema21Series.setData(ind.ema21);
    markersApi.setMarkers(buildMarkers(selectedCandles));

    const signal = signalFromCandles(selectedCandles);
    renderSignal(signal);
    $("livePrice").textContent = safe(last.c,10);

    const first = selectedCandles[0]?.c;
    $("liveChange").textContent = first ? pct((last.c/first-1)*100) : "—";
    $("chartMode").textContent = "LIVE ON-CHAIN · 1M CANDLES · " + selectedCandles.length + " BARS";
    setSource("dotChart","chartState","LIVE",["LIVE","READY"]);
    renderTape();
  });
}

function applyLiveTrade(trade, record=true) {
  const t = normalizeTrade(trade);
  if (!t || !selectedMint) return;

  if (record) {
    if (selectedTrades.some(x=>x.id===t.id)) return;
    selectedTrades.push(t);
    if (selectedTrades.length > 200) selectedTrades.shift();
  }

  const minute = Math.floor(t.time / 60) * 60;
  let last = selectedCandles[selectedCandles.length - 1];

  if (!last || minute > last.time) {
    last = {
      time:minute,
      ts:minute,
      o:t.price,
      h:t.price,
      l:t.price,
      c:t.price,
      v:Math.max(0,t.volumeSol),
    };
    selectedCandles.push(last);
  } else if (minute === last.time) {
    last.h = Math.max(last.h, t.price);
    last.l = Math.min(last.l, t.price);
    last.c = t.price;
    last.v = Math.max(0,last.v || 0) + Math.max(0,t.volumeSol);
  } else {
    // Late transaction for an older minute: repair that candle instead of
    // moving the live chart backwards.
    const old = selectedCandles.find(x=>x.time===minute);
    if (old) {
      old.h = Math.max(old.h,t.price);
      old.l = Math.min(old.l,t.price);
      old.c = t.price;
      old.v = Math.max(0,old.v || 0) + Math.max(0,t.volumeSol);
    }
  }

  if (selectedCandles.length > 360) selectedCandles.shift();

  $("lastUpdate").textContent = new Date(t.time*1000).toLocaleTimeString();
  scheduleLiveRender();
}

function mergeBackfill(history) {
  if (!history.length) return;

  const old = selectedCandles.slice();
  const merged = history.map(x=>({...x}));
  const byTime = new Map(merged.map(x=>[x.time,x]));

  for (const live of old) {
    const h = byTime.get(live.time);
    if (h) {
      h.h = Math.max(h.h,live.h);
      h.l = Math.min(h.l,live.l);
      h.c = live.c;
      // Do not sum: the backfill may already include those trades.
      h.v = Math.max(Number(h.v)||0,Number(live.v)||0);
    } else if (live.time > merged[merged.length-1].time) {
      merged.push({...live});
    }
  }

  merged.sort((a,b)=>a.time-b.time);
  selectedCandles = merged.slice(-360);
}

async function fetchChart(initial=false) {
  if (!selectedMint || chartBusy) return false;
  chartBusy = true;

  if (initial) $("chartMode").textContent = "LOADING PUMP.FUN HISTORY…";

  try {
    const r = await fetch(
      "/api/chart?mint="+encodeURIComponent(selectedMint)+"&limit=360&t="+Date.now(),
      {cache:"no-store"}
    );
    const j = await r.json();
    const candles = (j.candles || []).map(normalizeCandle).filter(Boolean);

    if (!candles.length) {
      $("chartMode").textContent = "WAITING FOR FIRST ON-CHAIN TRADE…";
      return false;
    }

    mergeBackfill(candles);
    renderChart(selectedCandles, firstChartLoad);

    firstChartLoad = false;
    return true;
  } catch {
    $("chartMode").textContent = "HISTORY DELAYED — LIVE STREAM STILL ACTIVE";
    return false;
  } finally {
    chartBusy = false;
  }
}

function startFallback() {
  if (fallbackTimer || !selectedMint) return;
  fallbackTimer = setInterval(()=>{
    if (!selectedMint) return;
    if (!liveTradeSocket || liveTradeSocket.readyState !== WebSocket.OPEN) fetchChart(false);
  }, 2500);
}

function stopFallback() {
  if (fallbackTimer) {
    clearInterval(fallbackTimer);
    fallbackTimer = null;
  }
}

function disconnectLiveTrade() {
  stopFallback();
  if (liveTradeSocket) {
    try { liveTradeSocket.close(); } catch {}
    liveTradeSocket = null;
  }
}

function scheduleReconnect() {
  stopFallback();

  const wait = liveTradeBackoff;
  liveTradeBackoff = Math.min(5000, liveTradeBackoff * 2);

  setTimeout(()=>{
    if (selectedMint) connectLiveTrade(selectedMint);
  },wait);

  startFallback();
}

function connectLiveTrade(mint) {
  disconnectLiveTrade();
  if (!mint) return;

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const url = proto + "//" + location.host + "/ws/trades?mint=" + encodeURIComponent(mint);

  setSource("dotChart","chartState","CONNECTING",["CONNECTING","LIVE","READY"]);
  $("chartMode").textContent = "CONNECTING LIVE ON-CHAIN FEED…";

  try {
    const socket = new WebSocket(url);
    liveTradeSocket = socket;

    socket.addEventListener("open",()=>{
      if (socket !== liveTradeSocket) return;
      liveTradeBackoff = 500;
      stopFallback();

      setSource("dotChart","chartState","LIVE",["LIVE","READY"]);
      $("chartMode").textContent = selectedCandles.length
        ? "LIVE ON-CHAIN · 1M CANDLES · WAITING FOR TRADES"
        : "LIVE ON-CHAIN · WAITING FOR FIRST TRADE";
    });

    socket.addEventListener("message",(ev)=>{
      if (socket !== liveTradeSocket) return;

      try {
        const msg = JSON.parse(ev.data);

        if (msg.type === "status") {
          if (msg.state === "LIVE") {
            setSource("dotChart","chartState","LIVE",["LIVE","READY"]);
          } else if (msg.state === "RECONNECTING" || msg.state === "CONNECTING") {
            setSource("dotChart","chartState",msg.state,["LIVE","READY","CONNECTING"]);
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
      setSource("dotChart","chartState","RECONNECTING",["LIVE","READY"]);
      $("chartMode").textContent = "RECONNECTING LIVE FEED…";
      scheduleReconnect();
    });

    socket.addEventListener("error",()=>{
      if (socket !== liveTradeSocket) return;
      setSource("dotChart","chartState","RECONNECTING",["LIVE","READY"]);
    });
  } catch {
    scheduleReconnect();
  }
}

async function analyzeSelected() {
  if (!selectedMint || analysisBusy) return;
  analysisBusy = true;
  $("analyzeButton").textContent = "ANALYZING…";

  try {
    const r = await fetch("/api/analyze",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({mint:selectedMint,include_x:false})
    });

    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Analysis failed");

    renderSecurity(data);

    const overview = data.overview && typeof data.overview === "object" ? data.overview : {};
    const asset = data.asset && typeof data.asset === "object" ? data.asset : {};
    const meta = asset.token_info || {};

    selectedInfo.symbol = cleanSymbol(overview.symbol || meta.symbol || selectedInfo.symbol || "TOKEN");
    selectedInfo.name = overview.name || selectedInfo.name || "";

    $("selectedTitle").textContent = "$" + selectedInfo.symbol;
    $("selectedMint").textContent = selectedMint;
    $("securityState").textContent = data.security_gate?.label || "UNKNOWN";
  } catch(e) {
    $("signalReason").textContent = e.message || "Analysis failed.";
  } finally {
    analysisBusy = false;
    $("analyzeButton").textContent = "ANALYZE";
  }
}

async function selectToken(mint) {
  if (!mint) return;

  disconnectLiveTrade();

  selectedMint = mint;
  selectedCandles = [];
  selectedTrades = [];
  selectedInfo = mergedCandidates().find(x=>x.mint===mint) || {mint};
  firstChartLoad = true;

  $("selectedTitle").textContent = "$" + cleanSymbol(selectedInfo.symbol || "TOKEN");
  $("selectedMint").textContent = mint;
  $("securityRows").innerHTML = '<div class="empty">Checking security in background…</div>';
  $("securityBadge").textContent = "CHECKING";
  $("securityBadge").className = "miniBadge checking";
  $("signalText").textContent = "WAIT";
  $("signalText").className = "signalText wait";
  $("signalBadge").textContent = "WAIT";
  $("signalBadge").className = "signal wait";
  $("signalReason").textContent = "Connecting directly to live on-chain trades…";
  $("chartMode").textContent = "STARTING LIVE ON-CHAIN FEED…";

  renderCandidates();

  // Start the zero-poll live feed immediately. History loads in parallel.
  connectLiveTrade(mint);
  fetchChart(true);
  analyzeSelected();
}

function startPumpFeed() {
  if (pumpSocket) return;

  try {
    pumpSocket = new WebSocket("wss://pumpportal.fun/api/data");

    pumpSocket.addEventListener("open",()=>{
      $("pumpState").textContent = "LIVE";
      setSource("dotPump","pumpState","LIVE",["LIVE"]);
      pumpSocket.send(JSON.stringify({method:"subscribeNewToken"}));
    });

    pumpSocket.addEventListener("message",ev=>{
      try {
        const raw = JSON.parse(ev.data);
        const item = normalizePumpEvent(raw);
        if (!item) return;

        const old = pumpEvents.find(x=>x.mint===item.mint);
        if (old) Object.assign(old,item,{ts:old.ts});
        else pumpEvents.unshift(item);

        pumpEvents = pumpEvents.slice(0,80);
        renderCandidates();
      } catch {}
    });

    pumpSocket.addEventListener("close",()=>{
      $("pumpState").textContent = "RECONNECTING";
      setTimeout(startPumpFeed,3000);
    });

    pumpSocket.addEventListener("error",()=>{
      $("pumpState").textContent = "UNAVAILABLE";
    });
  } catch {
    $("pumpState").textContent = "UNAVAILABLE";
  }
}

async function refreshMarketCandidates() {
  try {
    const r = await fetch("/api/discover?t="+Date.now(),{cache:"no-store"});
    const j = await r.json();
    marketCandidates = j.candidates || [];
    renderCandidates();
  } catch {}
}

document.addEventListener("DOMContentLoaded",()=>{
  initChart();

  $("openMint").addEventListener("click",()=>{
    const mint = $("mintInput").value.trim();
    if (mint) selectToken(mint);
  });

  $("mintInput").addEventListener("keydown",e=>{
    if (e.key==="Enter") $("openMint").click();
  });

  $("analyzeButton").addEventListener("click",analyzeSelected);

  $("scanNow").addEventListener("click",async()=>{
    await refreshMarketCandidates();
    renderCandidates();
  });

  health();
  refreshMarketCandidates();
  startPumpFeed();
  setInterval(health,15000);
  setInterval(refreshMarketCandidates,30000);

  window.addEventListener("resize",()=>{
    if (chart) chart.applyOptions({});
  });
});
