const $ = (id) => document.getElementById(id);

let pumpSocket = null;
let pumpEvents = [];
let marketCandidates = [];
let selectedMint = "";
let selectedInfo = {};
let selectedHistory = [];
let selectedSignals = [];
let liveTimer = null;
let analyzeTimer = null;
let busy = false;

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

function setSource(dotId, textId, state, goodStates) {
  const dot = $(dotId);
  const label = $(textId);
  if (!dot || !label) return;
  const good = goodStates.includes(state);
  dot.className = "dot " + (good ? "ok" : "bad");
  label.textContent = state || "UNKNOWN";
}

async function health() {
  try {
    const r = await fetch("/api/health?t=" + Date.now(), {cache:"no-store"});
    const j = await r.json();
    $("health").textContent = j.status || "ONLINE";
    setSource("dotPump","pumpState","LIVE",["LIVE"]);
    setSource("dotMarket","marketState",j.sources?.DexScreener?.state || "UNKNOWN",["READY"]);
    setSource("dotHelius","securityState",j.sources?.Security?.state || "UNKNOWN",["READY"]);
    setSource("dotX","xState",j.sources?.X?.state || "UNKNOWN",["READY","CONFIGURED"]);
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
  const rs = avgGain / avgLoss;
  return 100 - (100 / (1 + rs));
}

function mean(values) {
  if (!values.length) return null;
  return values.reduce((a,b)=>a+b,0) / values.length;
}

function deriveSignal(history) {
  const prices = history.map(x=>x.price);
  if (prices.length < 8) {
    return {
      state:"WAIT", reason:"Building live price history…", rsi:null,
      ema9:null, ema21:null, mean:null, pressure:null, score:0
    };
  }

  const p = prices[prices.length - 1];
  const e9 = ema(prices, 9);
  const e21 = ema(prices, 21);
  const r = rsi(prices, 14);
  const m = mean(prices.slice(-30));
  const recent = prices.slice(-12);
  let up = 0;
  for (let i=1;i<recent.length;i++) if (recent[i] > recent[i-1]) up++;
  const pressure = recent.length > 1 ? up / (recent.length - 1) : 0.5;
  const momentum = p && prices[0] ? (p / prices[Math.max(0, prices.length-5)] - 1) * 100 : 0;

  let score = 50;
  if (e9 != null && e21 != null) score += e9 > e21 ? 18 : -18;
  if (r != null) {
    if (r >= 52 && r <= 70) score += 12;
    if (r < 45) score -= 14;
    if (r > 78) score -= 8;
  }
  if (m != null) score += p > m ? 10 : -10;
  if (pressure >= 0.65) score += 10;
  if (pressure <= 0.35) score -= 10;
  if (momentum > 1) score += 8;
  if (momentum < -1) score -= 8;
  score = clamp(score,0,100);

  let state = "WAIT";
  if (e9 != null && e21 != null && r != null) {
    if (score >= 72 && e9 > e21 && p > m && r >= 52 && r <= 70 && pressure >= 0.58) {
      state = "BUY";
    } else if (score <= 32 && e9 < e21 && p < m && r <= 48 && pressure <= 0.42) {
      state = "SELL";
    }
  }

  let reason = "Mixed conditions — wait for confirmation.";
  if (state === "BUY") reason = "EMA 9 is above EMA 21, price is above the mean, momentum is positive, and upside ticks dominate.";
  if (state === "SELL") reason = "EMA 9 is below EMA 21, price is below the mean, momentum is negative, and downside ticks dominate.";

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
    symbol: raw.symbol || raw.tokenSymbol || "NEW",
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
    const row = {...e, score:launchScore(e)};
    map.set(e.mint,row);
  }

  for (const x of marketCandidates) {
    const row = {
      mint:x.address,
      symbol:x.symbol || "TOKEN",
      name:x.name || "",
      score:Math.round(Number(x.researchScore || 0)),
      source:x.pumpLane ? "PUMPSWAP" : "MARKET",
      price:Number(x.priceUsd || 0),
      liquidity:Number(x.liquidityUsd || 0),
      change5m:Number(x.priceChange5m || 0),
      age: x.pairCreatedAt ? Date.now() - Number(x.pairCreatedAt) : null
    };
    const old = map.get(row.mint);
    if (!old || row.score > old.score) map.set(row.mint,row);
  }

  return [...map.values()]
    .sort((a,b)=>b.score-a.score)
    .slice(0,8);
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
      '<div class="candidateMain"><b>$'+esc(x.symbol)+'</b><span>'+esc(x.mint)+'</span></div>' +
      '<div class="candidateStats"><b>'+x.score+'</b><span>'+esc(stat)+'</span></div>' +
      '<div class="candidateTag">'+esc(x.source || "PUMP.FUN")+'</div>' +
      (progress != null ? '<div class="progress"><i style="width:'+progress+'%"></i></div>' : '') +
      '</button>';
  }).join("");

  el.querySelectorAll("[data-mint]").forEach(btn=>{
    btn.addEventListener("click",()=>selectToken(btn.getAttribute("data-mint")));
  });
}

function renderSignal(signal) {
  const state = signal.state || "WAIT";
  $("signalBadge").textContent = state;
  $("signalBadge").className = "signal " + state.toLowerCase();
  $("signalText").textContent = state;
  $("signalText").className = "signalText " + state.toLowerCase();
  $("signalReason").textContent = signal.reason || "Waiting for more data.";

  $("liveRsi").textContent = signal.rsi == null ? "—" : Number(signal.rsi).toFixed(1);
  $("liveEma").textContent = safe(signal.ema9,8) + " / " + safe(signal.ema21,8);
  $("metricRsi").textContent = signal.rsi == null ? "—" : Number(signal.rsi).toFixed(1);
  $("metricEma9").textContent = safe(signal.ema9,8);
  $("metricEma21").textContent = safe(signal.ema21,8);
  $("metricMean").textContent = signal.mean == null || !selectedHistory.length
    ? "—"
    : pct((selectedHistory[selectedHistory.length-1].price / signal.mean - 1) * 100);
  $("metricPressure").textContent = signal.pressure == null ? "—" : pct(signal.pressure*100,0);
  $("metricConfirm").textContent = signal.score + "/100";
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
    '<div class="row"><span>Mint authority</span><b>'+ (s.mint_authority ? "ACTIVE" : "OFF") +'</b></div>' +
    '<div class="row"><span>Freeze authority</span><b>'+ (s.freeze_authority ? "ACTIVE" : "OFF") +'</b></div>';
}

function renderTape() {
  const rows = selectedHistory.slice(-12).reverse();
  if (!rows.length) {
    $("tape").innerHTML = '<div class="empty">No price updates yet.</div>';
    return;
  }
  $("tape").innerHTML = rows.map((x,i)=>{
    const prev = selectedHistory[selectedHistory.length-1-i-1];
    const up = !prev || x.price >= prev.price;
    return '<div class="tick '+(up?'up':'down')+'"><span>'+new Date(x.ts).toLocaleTimeString()+'</span><b>'+safe(x.price,8)+'</b><i>'+ (up ? "▲" : "▼") +'</i></div>';
  }).join("");
}

function updateChart(signal) {
  const canvas = $("chart");
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(320,Math.floor(rect.width));
  const h = 420;
  canvas.width = w*dpr;
  canvas.height = h*dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);

  ctx.fillStyle = "#071019";
  ctx.fillRect(0,0,w,h);

  if (!selectedHistory.length) {
    ctx.fillStyle = "#7f90a5";
    ctx.font = "13px system-ui";
    ctx.fillText("Waiting for live price data…",20,34);
    return;
  }

  const values = selectedHistory.map(x=>x.price);
  const ema9s = [];
  const ema21s = [];
  for(let i=0;i<values.length;i++){
    ema9s.push(ema(values.slice(0,i+1),9));
    ema21s.push(ema(values.slice(0,i+1),21));
  }

  const meanV = signal.mean;
  const all = values.concat(ema9s.filter(v=>v!=null),ema21s.filter(v=>v!=null),meanV || []);
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = Math.max((hi-lo)*0.12,hi*0.001 || 1);
  lo -= pad; hi += pad;

  const left=48,right=w-15,top=20,bottom=h-42;
  const x = i=>left+(i/(Math.max(1,values.length-1)))*(right-left);
  const y = p=>bottom-((p-lo)/(hi-lo))*(bottom-top);

  ctx.strokeStyle="#172634";
  ctx.lineWidth=1;
  for(let i=0;i<5;i++){
    const yy=top+i*(bottom-top)/4;
    ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(right,yy);ctx.stroke();
  }

  function drawSeries(series, lineWidth, dash) {
    ctx.save();
    ctx.lineWidth=lineWidth;
    ctx.strokeStyle="#a9b7c8";
    if(dash) ctx.setLineDash(dash);
    let started=false;
    series.forEach((v,i)=>{
      if(v==null) return;
      if(!started){ctx.beginPath();ctx.moveTo(x(i),y(v));started=true;}
      else ctx.lineTo(x(i),y(v));
    });
    if(started)ctx.stroke();
    ctx.restore();
  }

  ctx.strokeStyle="#e7eef7";
  ctx.lineWidth=2;
  ctx.beginPath();
  values.forEach((v,i)=>i===0?ctx.moveTo(x(i),y(v)):ctx.lineTo(x(i),y(v)));
  ctx.stroke();

  ctx.strokeStyle="#39dc89";
  ctx.lineWidth=1.5;
  ctx.beginPath();
  ema9s.forEach((v,i)=>{if(v==null)return;i===0?ctx.moveTo(x(i),y(v)):ctx.lineTo(x(i),y(v));});
  ctx.stroke();

  ctx.strokeStyle="#ffbd54";
  ctx.lineWidth=1.5;
  ctx.beginPath();
  ema21s.forEach((v,i)=>{if(v==null)return;i===0?ctx.moveTo(x(i),y(v)):ctx.lineTo(x(i),y(v));});
  ctx.stroke();

  if(meanV!=null){
    ctx.save();ctx.strokeStyle="#6da8ff";ctx.setLineDash([6,5]);ctx.beginPath();ctx.moveTo(left,y(meanV));ctx.lineTo(right,y(meanV));ctx.stroke();ctx.restore();
  }

  selectedSignals.forEach(sig=>{
    const idx=selectedHistory.findIndex(v=>v.ts===sig.ts);
    if(idx<0)return;
    const yy=y(sig.price),xx=x(idx);
    const isBuy=sig.state==="BUY";
    ctx.fillStyle=isBuy?"#39dc89":"#ff6575";
    ctx.beginPath();ctx.arc(xx,yy,5,0,Math.PI*2);ctx.fill();
    ctx.fillStyle=isBuy?"#39dc89":"#ff6575";
    ctx.font="bold 11px system-ui";
    ctx.fillText(isBuy?"BUY":"SELL",Math.min(right-34,xx+7),Math.max(14,yy-7));
  });

  ctx.fillStyle="#7f90a5";
  ctx.font="10px system-ui";
  ctx.fillText("PRICE",left,12);
  ctx.fillText(safe(hi,8),left+4,top+10);
  ctx.fillText(safe(lo,8),left+4,bottom);

  $("chartMode").textContent = selectedHistory.length >= 21
    ? "LIVE 5s PRICE · EMA 9/21 · RSI 14"
    : "BUILDING LIVE HISTORY";
}

function applyLivePoint(price, timestamp) {
  if (!Number.isFinite(Number(price))) return;

  const point={price:Number(price),ts:Number(timestamp||Date.now())* (Number(timestamp||0)>2e10?1:1000)};
  if (selectedHistory.length && point.ts <= selectedHistory[selectedHistory.length-1].ts) return;

  selectedHistory.push(point);
  if (selectedHistory.length > 180) selectedHistory.shift();

  const signal=deriveSignal(selectedHistory);
  const prior=selectedSignals.length ? selectedSignals[selectedSignals.length-1].state : "WAIT";
  if (signal.state !== "WAIT" && signal.state !== prior) {
    selectedSignals.push({state:signal.state,price:point.price,ts:point.ts});
    if(selectedSignals.length>12)selectedSignals.shift();
  }

  $("livePrice").textContent=safe(point.price,10);
  const first=selectedHistory[0]?.price;
  $("liveChange").textContent=first?pct((point.price/first-1)*100):"—";
  $("lastUpdate").textContent=new Date(point.ts).toLocaleTimeString();

  renderSignal(signal);
  renderTape();
  updateChart(signal);
}

async function pollLivePrice() {
  if (!selectedMint) return;
  try {
    const r=await fetch("/api/live/price?mint="+encodeURIComponent(selectedMint)+"&t="+Date.now(),{cache:"no-store"});
    const j=await r.json();
    if(j.price!=null) applyLivePoint(j.price,j.timestamp);
  } catch {}
}

async function analyzeSelected() {
  if (!selectedMint || busy) return;
  busy=true;
  $("analyzeButton").textContent="ANALYZING…";
  try {
    const r=await fetch("/api/analyze",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({mint:selectedMint,include_x:false})
    });
    const data=await r.json();
    if(!r.ok) throw new Error(data.detail || "Analysis failed");

    renderSecurity(data);
    const overview = data.overview && typeof data.overview === "object" ? data.overview : {};
    const asset = data.asset && typeof data.asset === "object" ? data.asset : {};
    const meta = asset.token_info || {};
    selectedInfo.symbol = overview.symbol || meta.symbol || selectedInfo.symbol || "TOKEN";
    selectedInfo.name = overview.name || selectedInfo.name || "";
    $("selectedTitle").textContent="$"+selectedInfo.symbol;
    $("selectedMint").textContent=selectedMint;

    if(Array.isArray(data.candles) && data.candles.length) {
      selectedHistory = data.candles.map(c=>({price:Number(c.c),ts:Number(c.ts)*1000})).slice(-120);
      selectedSignals=[];
      const s=deriveSignal(selectedHistory);
      $("livePrice").textContent=safe(selectedHistory[selectedHistory.length-1].price,10);
      renderSignal(s);
      updateChart(s);
      renderTape();
    }

    $("securityState").textContent=(data.security_gate?.label || "UNKNOWN");
  } catch(e) {
    $("signalReason").textContent=e.message || "Analysis failed.";
  } finally {
    busy=false;
    $("analyzeButton").textContent="ANALYZE";
  }
}

async function selectToken(mint) {
  selectedMint=mint;
  selectedHistory=[];
  selectedSignals=[];
  const all=mergedCandidates();
  selectedInfo=all.find(x=>x.mint===mint) || {mint};
  $("selectedTitle").textContent="$"+(selectedInfo.symbol || "TOKEN");
  $("selectedMint").textContent=mint;
  $("securityRows").innerHTML='<div class="empty">Checking security…</div>';
  $("securityBadge").textContent="CHECKING";
  $("securityBadge").className="miniBadge";
  $("signalText").textContent="WAIT";
  $("signalText").className="signalText wait";
  $("signalBadge").textContent="WAIT";
  $("signalBadge").className="signal wait";
  $("signalReason").textContent="Building live price history…";
  renderCandidates();
  await analyzeSelected();
  if(liveTimer)clearInterval(liveTimer);
  await pollLivePrice();
  liveTimer=setInterval(pollLivePrice,5000);
}

function startPumpFeed() {
  if (pumpSocket) return;
  try {
    pumpSocket = new WebSocket("wss://pumpportal.fun/api/data");
    pumpSocket.addEventListener("open",()=>{
      $("pumpState").textContent="LIVE";
      setSource("dotPump","pumpState","LIVE",["LIVE"]);
      pumpSocket.send(JSON.stringify({method:"subscribeNewToken"}));
    });
    pumpSocket.addEventListener("message",ev=>{
      try{
        const raw=JSON.parse(ev.data);
        const item=normalizePumpEvent(raw);
        if(!item)return;
        const old=pumpEvents.find(x=>x.mint===item.mint);
        if(old) Object.assign(old,item,{ts:old.ts});
        else pumpEvents.unshift(item);
        pumpEvents=pumpEvents.slice(0,60);
        renderCandidates();
        if(!selectedMint && pumpEvents.length>=3){
          const top=mergedCandidates()[0];
          if(top)selectToken(top.mint);
        }
      }catch{}
    });
    pumpSocket.addEventListener("close",()=>{
      $("pumpState").textContent="RECONNECTING";
      setTimeout(startPumpFeed,5000);
    });
    pumpSocket.addEventListener("error",()=>{
      $("pumpState").textContent="UNAVAILABLE";
    });
  }catch{
    $("pumpState").textContent="UNAVAILABLE";
  }
}

async function refreshMarketCandidates() {
  try{
    const r=await fetch("/api/discover?t="+Date.now(),{cache:"no-store"});
    const j=await r.json();
    marketCandidates=j.candidates || [];
    renderCandidates();
  }catch{}
}

function setSource(dotId,textId,state,goodStates){const dot=$(dotId);const lab=$(textId);if(!dot||!lab)return;dot.className="dot "+(goodStates.includes(state)?"ok":"bad");lab.textContent=state||"UNKNOWN";}

document.addEventListener("DOMContentLoaded",()=>{
  $("openMint").addEventListener("click",()=>{
    const mint=$("mintInput").value.trim();
    if(mint)selectToken(mint);
  });
  $("mintInput").addEventListener("keydown",e=>{
    if(e.key==="Enter")$("openMint").click();
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
    const s=deriveSignal(selectedHistory);
    updateChart(s);
  });
});
