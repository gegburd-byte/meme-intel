
let pumpSocket = null;
let pumpEvents = [];

function setPumpFeedStatus(ok, text) {
  const dot = $("dotPF");
  const label = $("srcPF");
  const state = $("pumpFeedState");
  if (dot) dot.className = "dot " + (ok ? "ok" : "bad");
  if (label) label.textContent = text;
  if (state) state.textContent = text;
}

function renderPumpEvents() {
  const list = pumpEvents.slice(0, 12);

  $("pumpFeedList").innerHTML = list.map(function(e) {
    return "<div class=\"pumpRow\">" +
      "<div class=\"pumpTime\">" + escapeHtml(e.time) + "</div>" +
      "<div class=\"pumpMain\">" +
        "<b>$" + escapeHtml(e.symbol || e.name || "NEW") + "</b>" +
        "<span>" + escapeHtml(e.mint || "") + "</span>" +
      "</div>" +
      "<div class=\"pumpMeta\">" +
        "new token" + (e.creator ? " · " + escapeHtml(e.creator.slice(0, 8)) + "…" : "") +
      "</div>" +
      "<button class=\"secondary smallButton\" data-pump-ca=\"" + escapeHtml(e.mint || "") + "\">ANALYZE</button>" +
    "</div>";
  }).join("") || "<div class=\"muted\">Waiting for live Pump.fun events…</div>";

  document.querySelectorAll("[data-pump-ca]").forEach(function(btn) {
    btn.addEventListener("click", function() {
      $("mint").value = btn.getAttribute("data-pump-ca") || "";
      analyze();
      window.scrollTo({top: 0, behavior: "smooth"});
    });
  });
}

function startPumpFeed() {
  if (pumpSocket) return;

  try {
    pumpSocket = new WebSocket("wss://pumpportal.fun/api/data");

    pumpSocket.addEventListener("open", function() {
      setPumpFeedStatus(true, "LIVE");
      pumpSocket.send(JSON.stringify({method: "subscribeNewToken"}));
    });

    pumpSocket.addEventListener("message", function(event) {
      try {
        const data = JSON.parse(event.data);
        const mint = data.mint || data.tokenAddress;
        if (!mint) return;

        const symbol = data.symbol || data.tokenSymbol || "";
        const name = data.name || data.tokenName || "";
        const creator = data.traderPublicKey || data.creator || "";

        pumpEvents.unshift({
          mint: mint,
          symbol: symbol,
          name: name,
          creator: creator,
          time: new Date().toLocaleTimeString()
        });

        const seen = new Set();
        pumpEvents = pumpEvents.filter(function(x) {
          if (seen.has(x.mint)) return false;
          seen.add(x.mint);
          return true;
        }).slice(0, 30);

        renderPumpEvents();
      } catch (e) {}
    });

    pumpSocket.addEventListener("close", function() {
      setPumpFeedStatus(false, "RECONNECTING…");
      pumpSocket = null;
      setTimeout(startPumpFeed, 5000);
    });

    pumpSocket.addEventListener("error", function() {
      setPumpFeedStatus(false, "UNAVAILABLE");
    });
  } catch (e) {
    setPumpFeedStatus(false, "UNAVAILABLE");
  }
}


let topTimer = null;
let topBusy = false;

function setGate(gate) {
  gate = gate || {};
  const el = $("rugGate");
  el.textContent = gate.label || "SECURITY UNKNOWN";
  el.className = "gate " + String(gate.label || "SECURITY UNKNOWN").toLowerCase().replace(/[^a-z]+/g, "-");
}

function renderTop(data) {
  const top = data && data.top;

  $("topUpdated").textContent = data && data.updated_at
    ? "UPDATED " + new Date(data.updated_at * 1000).toLocaleTimeString()
    : "NO UPDATE";

  if (!top) {
    $("topSymbol").textContent = "NO FULLY CHECKED SETUP";
    $("topMint").textContent = "The scanner did not find a candidate that passed every required gate.";
    setGate({label: "SECURITY UNKNOWN"});
    $("topReason").textContent = "Keep scanning; incomplete security data never counts as a clean pass.";
    $("topGateReasons").innerHTML = "";
    $("topCandidates").innerHTML = "";
    return;
  }

  $("topSymbol").textContent = "$" + (top.symbol || top.name || "UNKNOWN");
  $("topMint").textContent = top.mint || "—";
  setGate(top.security_gate);

  $("topRank").textContent = top.research_rank == null ? "—" : top.research_rank + "/100";
  $("topEntry").textContent = safe(top.decision && top.decision.entry_trigger);
  $("topStop").textContent = safe(top.decision && top.decision.invalidation);
  $("topTargets").textContent =
    safe(top.decision && top.decision.target1) + " / " +
    safe(top.decision && top.decision.target2);
  $("topSocial").textContent = top.social && top.social.sentiment != null
    ? Number(top.social.sentiment).toFixed(0)
    : "—";
  $("topRisk").textContent = top.risk && top.risk.overall
    ? top.risk.overall
    : "—";

  $("topReason").textContent =
    (top.decision && top.decision.reason) ||
    "No current decision text.";

  const reasons = (top.security_gate && top.security_gate.reasons) || [];
  $("topGateReasons").innerHTML = reasons.map(function(r) {
    return "<div class=\"muted gateReason\">• " + escapeHtml(r) + "</div>";
  }).join("");

  const rows = (data.candidates || []).slice(0, 6);
  $("topCandidates").innerHTML = rows.map(function(x, i) {
    const gateLabel = x.security_gate && x.security_gate.label
      ? x.security_gate.label
      : "UNKNOWN";
    const action = x.decision && x.decision.action
      ? x.decision.action
      : "NO DATA";

    return "<div class=\"topRow\">" +
      "<div class=\"topRowRank\">#" + (i + 1) + "</div>" +
      "<div class=\"topRowMain\">" +
        "<b>$" + escapeHtml(x.symbol || x.name || "UNKNOWN") + "</b>" +
        "<span>" + escapeHtml(x.mint || "") + "</span>" +
      "</div>" +
      "<div class=\"topRowAction\">" + escapeHtml(action) + "</div>" +
      "<div class=\"topRowGate\">" + escapeHtml(gateLabel) + "</div>" +
      "<div class=\"topRowScore\">" + Number(x.research_rank || 0).toFixed(0) + "</div>" +
      "<button class=\"secondary smallButton\" data-top-ca=\"" + escapeHtml(x.mint || "") + "\">LOAD</button>" +
    "</div>";
  }).join("");

  document.querySelectorAll("[data-top-ca]").forEach(function(btn) {
    btn.addEventListener("click", function() {
      $("mint").value = btn.getAttribute("data-top-ca") || "";
      analyze();
      window.scrollTo({top: 0, behavior: "smooth"});
    });
  });
}

async function scanTop() {
  if (topBusy) return;

  topBusy = true;
  $("scanTop").textContent = "SCANNING…";
  $("scanTop").disabled = true;

  try {
    const r = await fetch("/api/top?x=" + Date.now(), { cache: "no-store" });
    const data = await r.json();

    if (!r.ok) {
      throw new Error(data.detail || "Top scan failed");
    }

    renderTop(data);
  } catch (e) {
    $("topReason").textContent = "Scanner error: " + (e.message || e);
  } finally {
    $("scanTop").textContent = "SCAN NOW";
    $("scanTop").disabled = false;
    topBusy = false;
  }
}

function startAutopilot() {
  if (topTimer) clearInterval(topTimer);
  scanTop();
  topTimer = setInterval(scanTop, 60000);
}

const $ = (id) => document.getElementById(id);

let live = false;
let timer = null;
let lastData = null;

function safe(v, digits = 6) {
  if (v == null || !Number.isFinite(Number(v))) return "—";
  return Number(v).toLocaleString(undefined, { maximumFractionDigits: digits });
}

function usd(v) {
  if (v == null || !Number.isFinite(Number(v))) return "—";
  return "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function pct(v, digits = 2) {
  if (v == null || !Number.isFinite(Number(v))) return "—";
  return Number(v).toFixed(digits) + "%";
}

function row(name, value) {
  return "<div class=\"row\"><span>" + name + "</span><b>" + value + "</b></div>";
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, function(c) {
    const map = {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"};
    return map[c] || c;
  });
}

function setSource(dotId, textId, source) {
  if (!source) return;
  const ok = source.configured === true || source.state === "READY" || source.state === "CONFIGURED";
  $(dotId).className = "dot " + (ok ? "ok" : "bad");
  $(textId).textContent = source.state || "UNKNOWN";
}

async function health() {
  try {
    const r = await fetch("/api/health?x=" + Date.now(), { cache: "no-store" });
    const j = await r.json();
    $("health").textContent = j.status || "ONLINE";
    setSource("dotX", "srcX", j.sources && j.sources.X);
    setSource("dotDS", "srcDS", j.sources && j.sources.DexScreener);
    setSource("dotGT", "srcGT", j.sources && j.sources.GeckoTerminal);
    setSource("dotHE", "srcHE", j.sources && j.sources.Helius);
  } catch (e) {
    $("health").textContent = "BACKEND ERROR";
  }
}

function renderDecision(d) {
  d = d || {};
  $("action").textContent = d.action || "NO DATA";
  $("exitAction").textContent = d.exit_action || "—";
  $("decisionScore").textContent = d.score == null ? "—" : d.score + "/100";
  $("confidence").textContent = d.confidence || "—";
  $("confirmations").textContent = d.confirmation_count == null ? "—" : d.confirmation_count + "/7";
  $("entryTrigger").textContent = safe(d.entry_trigger);
  $("decisionReason").textContent = d.reason || "—";
  $("entryStyle").textContent = d.entry_style || "—";

  $("targetTrigger").textContent = safe(d.entry_trigger);
  $("targetStop").textContent = safe(d.invalidation);
  $("target1").textContent = safe(d.target1);
  $("target2").textContent = safe(d.target2);

  $("exitRules").innerHTML = (d.exit_rules || []).map(function(x) {
    return "<div class=\"muted rule\">• " + escapeHtml(x) + "</div>";
  }).join("");
}

function renderSetup(s) {
  s = s || {};
  $("setupState").textContent = s.state || "DATA NOT AVAILABLE";
  $("prevHigh").textContent = safe(s.prev_high);
  $("higherLow").textContent = safe(s.higher_low);
  $("stop").textContent = safe(s.stop);
}

function renderMarket(m) {
  m = m || {};
  const p = m.profile || {};

  $("poc").textContent = safe(p.poc);
  $("vah").textContent = safe(p.vah);
  $("val").textContent = safe(p.val);
  $("vwap").textContent = safe(m.vwap);

  $("rsi").textContent = safe(m.rsi14, 2);
  $("ema").textContent = safe(m.ema9) + " / " + safe(m.ema21);
  $("buyRatio").textContent = m.buy_ratio_5m == null ? "—" : pct(Number(m.buy_ratio_5m) * 100, 1);
  $("volSpike").textContent = m.volume_spike == null ? "—" : Number(m.volume_spike).toFixed(2) + "x";
  $("trendState").textContent = m.ema_trend || "—";

  $("profileRows").innerHTML =
    row("Price vs VAH", pct(m.distance_vah_pct)) +
    row("Price vs POC", pct(m.distance_poc_pct)) +
    row("Price vs VAL", pct(m.distance_val_pct)) +
    row("ATR", pct(m.atr_pct));

  $("flowRows").innerHTML =
    row("1m", pct(m.return_1m_pct)) +
    row("5m", pct(m.return_5m_pct)) +
    row("15m", pct(m.return_15m_pct)) +
    row("30m", pct(m.return_30m_pct)) +
    row("Volume / Liquidity", m.volume_liquidity_ratio == null ? "—" : Number(m.volume_liquidity_ratio).toFixed(3));
}

function renderSocial(s, items) {
  s = s || {};

  $("socialState").textContent = s.state || "—";
  $("posts").textContent = s.recent_15m == null ? "—" : s.recent_15m;
  $("vel").textContent = s.mention_velocity == null ? "—" : Number(s.mention_velocity).toFixed(2);
  $("accel").textContent = s.velocity_acceleration == null ? "—" : Number(s.velocity_acceleration).toFixed(2) + "x";
  $("authors").textContent = s.unique_author_count == null ? "—" : s.unique_author_count;
  $("sentiment").textContent = s.sentiment == null ? "—" : Number(s.sentiment).toFixed(0);
  $("authorQuality").textContent = s.author_quality == null ? "—" : Number(s.author_quality).toFixed(0);
  $("engagement").textContent = s.engagement_velocity == null ? "—" : Number(s.engagement_velocity).toFixed(1);
  $("copyRisk").textContent = s.coordination_risk == null ? "—" : Number(s.coordination_risk).toFixed(0);
  $("topAuthor").textContent = s.top_authors && s.top_authors.length ? "@" + s.top_authors[0].username : "—";

  const list = items || [];
  $("tweets").innerHTML = list.slice(0, 12).map(function(t) {
    const age = t.age_seconds == null ? "" : " · " + Math.max(0, Math.round(t.age_seconds / 60)) + "m";
    const verify = t.verified ? " ✓" : "";
    return "<div class=\"tweet\">" +
      "<div class=\"meta\">@" + escapeHtml(t.username || "unknown") + verify +
      " · " + Number(t.followers || 0).toLocaleString() +
      " followers · " + Number(t.engagement || 0).toLocaleString() + " eng" + age + "</div>" +
      "<p>" + escapeHtml(t.text || "") + "</p>" +
      "<a target=\"_blank\" rel=\"noopener\" href=\"" + escapeHtml(t.url || "#") + "\">OPEN ON X ↗</a>" +
      "</div>";
  }).join("") || "<div class=\"muted\">No posts returned for the token-specific query.</div>";
}

function renderRisk(r) {
  r = r || {};
  $("riskLevel").textContent = r.overall || "UNKNOWN";

  $("riskRows").innerHTML = (r.flags || []).map(function(f) {
    return row(escapeHtml(f.code || "FLAG"), escapeHtml(f.level || "UNKNOWN")) +
      "<div class=\"muted\">" + escapeHtml(f.reason || "") + "</div>";
  }).join("") || "<div class=\"muted\">No risk flags returned.</div>";
}

function renderToken(o) {
  if (!o || typeof o !== "object") {
    $("tokenName").textContent = "DATA NOT AVAILABLE";
    $("tokenRows").innerHTML = "";
    return;
  }

  $("tokenName").textContent = o.symbol || o.name || "TOKEN";
  $("tokenRows").innerHTML =
    row("Price", safe(o.price, 10)) +
    row("Liquidity", usd(o.liquidity)) +
    row("Market Cap", usd(o.marketCap)) +
    row("5m Volume", usd(o.v5mUSD)) +
    row("1h Volume", usd(o.v1hUSD)) +
    row("24h Volume", usd(o.v24hUSD)) +
    row("DEX", o.dexId || "—");
}

function renderChart(data) {
  const canvas = $("chart");
  if (!canvas || !data) return;

  const candles = data.candles || [];
  if (!candles.length) {
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    $("chartMeta").textContent = "NO CANDLE DATA";
    return;
  }

  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(320, Math.floor(rect.width));
  const height = 360;

  canvas.width = Math.floor(width * dpr);
  canvas.height = Math.floor(height * dpr);

  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const left = 8;
  const right = width - 8;
  const top = 12;
  const bottom = 305;
  const volumeTop = 315;
  const volumeBottom = 350;

  let lo = Math.min.apply(null, candles.map(c => Number(c.l)));
  let hi = Math.max.apply(null, candles.map(c => Number(c.h)));

  const p = data.market && data.market.profile ? data.market.profile : {};
  const levels = [
    p.poc, p.vah, p.val,
    data.market && data.market.vwap,
    data.decision && data.decision.entry_trigger,
    data.decision && data.decision.invalidation,
    data.decision && data.decision.target1,
    data.decision && data.decision.target2
  ].filter(v => v != null && Number.isFinite(Number(v))).map(Number);

  if (levels.length) {
    lo = Math.min(lo, Math.min.apply(null, levels));
    hi = Math.max(hi, Math.max.apply(null, levels));
  }

  const pad = Math.max((hi - lo) * 0.08, hi * 0.0001);
  lo -= pad;
  hi += pad;

  function y(price) {
    return bottom - ((price - lo) / Math.max(hi - lo, 1e-12)) * (bottom - top);
  }

  function x(i) {
    return left + (i + 0.5) * ((right - left) / candles.length);
  }

  ctx.fillStyle = "#071019";
  ctx.fillRect(0, 0, width, height);

  ctx.strokeStyle = "#182633";
  ctx.lineWidth = 1;

  for (let i = 0; i <= 4; i++) {
    const gy = top + (i / 4) * (bottom - top);
    ctx.beginPath();
    ctx.moveTo(left, gy);
    ctx.lineTo(right, gy);
    ctx.stroke();
  }

  const maxVol = Math.max.apply(null, candles.map(c => Number(c.v) || 0).concat([1]));
  const step = (right - left) / candles.length;
  const candleW = Math.max(1, step * 0.62);

  candles.forEach(function(c, i) {
    const open = Number(c.o);
    const close = Number(c.c);
    const high = Number(c.h);
    const low = Number(c.l);
    const xx = x(i);
    const up = close >= open;

    ctx.strokeStyle = up ? "#35d889" : "#ff6374";
    ctx.fillStyle = up ? "#35d889" : "#ff6374";
    ctx.lineWidth = 1;

    ctx.beginPath();
    ctx.moveTo(xx, y(high));
    ctx.lineTo(xx, y(low));
    ctx.stroke();

    const bodyTop = y(Math.max(open, close));
    const bodyBottom = y(Math.min(open, close));
    ctx.fillRect(
      xx - candleW / 2,
      bodyTop,
      candleW,
      Math.max(1, bodyBottom - bodyTop)
    );

    const volH = ((Number(c.v) || 0) / maxVol) * (volumeBottom - volumeTop);
    ctx.globalAlpha = 0.35;
    ctx.fillRect(xx - candleW / 2, volumeBottom - volH, candleW, volH);
    ctx.globalAlpha = 1;
  });

  function line(value, color, label, dashed) {
    if (value == null || !Number.isFinite(Number(value))) return;
    const yy = y(Number(value));

    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.2;
    if (dashed) ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(left, yy);
    ctx.lineTo(right, yy);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = "10px system-ui";
    ctx.fillText(label + " " + safe(value, 6), left + 6, Math.max(11, yy - 4));
    ctx.restore();
  }

  line(p.poc, "#e8c75f", "POC", false);
  line(p.vah, "#66a9ff", "VAH", true);
  line(p.val, "#66a9ff", "VAL", true);
  line(data.market && data.market.vwap, "#a7b4c3", "VWAP", true);
  line(data.decision && data.decision.entry_trigger, "#39dc89", "TRIGGER", false);
  line(data.decision && data.decision.invalidation, "#ff6575", "STOP", true);
  line(data.decision && data.decision.target1, "#39dc89", "T1", true);
  line(data.decision && data.decision.target2, "#39dc89", "T2", true);

  const last = candles[candles.length - 1];
  $("chartMeta").textContent = candles.length + " closed 1m candles · last " + safe(last.c, 8);
}

function renderAll(data) {
  lastData = data;

  renderDecision(data.decision);
  renderSetup(data.setup);
  renderMarket(data.market);
  renderChart(data);
  renderSocial(data.social, data.social_items);
  renderRisk(data.risk);
  renderToken(data.overview);

  $("queryUsed").textContent = data.x_query_used || "—";
  $("raw").textContent = JSON.stringify(data, null, 2);

  const currentPrice = data.market && data.market.price;
  if (currentPrice != null && $("pEntry").value === "") {
    $("pEntry").value = currentPrice;
  }

  loadTrades();
}

async function analyze() {
  const mint = $("mint").value.trim();

  if (!mint) {
    $("decisionReason").textContent = "Paste a Solana contract address first.";
    return;
  }

  $("action").textContent = "ANALYZING…";
  $("decisionReason").textContent = "Pulling market structure, flow, risk and token-specific X posts…";

  try {
    const r = await fetch("/api/analyze", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        mint: mint,
        x_query: $("query").value.trim()
      })
    });

    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || "Analyze request failed");

    renderAll(data);
  } catch (e) {
    $("action").textContent = "ERROR";
    $("decisionReason").textContent = e.message || "Unknown error";
    $("raw").textContent = String(e && e.stack ? e.stack : e);
  }
}

function toggleLive() {
  live = !live;
  $("liveToggle").textContent = live ? "LIVE ON · 20s" : "LIVE OFF";

  if (timer) {
    clearInterval(timer);
    timer = null;
  }

  if (live) {
    analyze();
    timer = setInterval(analyze, 30000);
  }
}

async function loadTrades() {
  try {
    const r = await fetch("/api/paper/trades?x=" + Date.now());
    const j = await r.json();

    $("paperTrades").innerHTML =
      (j.trades || []).slice(0, 10).map(function(t) {
        return "<div class=\"trade\"><span>#" + t.id + " " +
          String(t.mint || "").slice(0, 8) + "…</span><b>" +
          (t.pnl == null ? "OPEN" : usd(t.pnl)) + "</b></div>";
      }).join("") ||
      "<div class=\"muted\">No paper trades yet.</div>";
  } catch (e) {}
}

async function paperOpen() {
  const mint = $("mint").value.trim();
  const entry = Number($("pEntry").value || (lastData && lastData.market && lastData.market.price));
  const qty = Number($("pQty").value);

  if (!mint || !Number.isFinite(entry) || !Number.isFinite(qty) || qty <= 0) {
    $("decisionReason").textContent = "Enter a mint, entry price, and quantity.";
    return;
  }

  await fetch("/api/paper/open", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify({
      mint: mint,
      entry: entry,
      qty: qty,
      note: $("pNote").value,
      side: "LONG"
    })
  });

  loadTrades();
}

document.addEventListener("DOMContentLoaded", function() {
  $("analyze").addEventListener("click", analyze);
  $("liveToggle").addEventListener("click", toggleLive);
  $("paperOpen").addEventListener("click", paperOpen);
  $("radarRefresh").addEventListener("click", refreshRadar);
  $("scanTop").addEventListener("click", scanTop);

  $("mint").addEventListener("keydown", function(e) {
    if (e.key === "Enter") analyze();
  });

  $("query").addEventListener("keydown", function(e) {
    if (e.key === "Enter") analyze();
  });

  health();
  loadTrades();
  startAutopilot();
  refreshRadar();
  startPumpFeed();
  setInterval(health, 15000);
});

function renderRadar(candidates) {
  const list = candidates || [];
  if (!list.length) {
    $("radarList").innerHTML = "<div class=\"muted\">No fresh contract-address candidates were found.</div>";
    return;
  }

  $("radarList").innerHTML = list.map(function(c, i) {
    const ticker = (c.ticker_hints || []).length ? " · $" + c.ticker_hints[0] : "";
    const top = c.top_author ? "@" + c.top_author : "unknown";
    return "<div class=\"radarRow\">" +
      "<div class=\"radarRank\">#" + (i + 1) + "</div>" +
      "<div class=\"radarMain\">" +
        "<div class=\"radarTitle\"><b>" + escapeHtml(ticker || c.mint.slice(0, 8) + "…") + "</b> <span>" + escapeHtml(c.mint) + "</span></div>" +
        "<div class=\"radarSub\">" + c.posts_15m + " posts · " + c.unique_authors + " authors · " + Number(c.mention_velocity || 0).toFixed(2) + "/min · accel " + Number(c.acceleration || 0).toFixed(2) + "x · copy risk " + Number(c.coordination_risk || 0).toFixed(0) + "</div>" +
      "</div>" +
      "<div class=\"radarScore\">" + Number(c.score || 0).toFixed(0) + "</div>" +
      "<button class=\"secondary smallButton\" data-ca=\"" + escapeHtml(c.mint) + "\">ANALYZE</button>" +
    "</div>";
  }).join("");

  document.querySelectorAll("[data-ca]").forEach(function(btn) {
    btn.addEventListener("click", function() {
      $("mint").value = btn.getAttribute("data-ca") || "";
      analyze();
      window.scrollTo({top: 0, behavior: "smooth"});
    });
  });
}

async function refreshRadar() {
  const button = $("radarRefresh");
  button.textContent = "SCANNING…";
  button.disabled = true;

  try {
    const r = await fetch("/api/x/radar", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({
        query: $("query").value.trim() || "lang:en -is:retweet",
        max_results: 100
      })
    });

    const data = await r.json();
    renderRadar(data.candidates || []);
    if (data.state && data.state !== "READY") {
      $("radarList").innerHTML = "<div class=\"muted\">X radar: " + escapeHtml(data.state) + "</div>";
    }
  } catch (e) {
    $("radarList").innerHTML = "<div class=\"muted\">Radar error: " + escapeHtml(e.message || e) + "</div>";
  } finally {
    button.textContent = "REFRESH RADAR";
    button.disabled = false;
  }
}
