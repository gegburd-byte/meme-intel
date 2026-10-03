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

function renderAll(data) {
  lastData = data;

  renderDecision(data.decision);
  renderSetup(data.setup);
  renderMarket(data.market);
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
  $("liveToggle").textContent = live ? "LIVE ON · 30s" : "LIVE OFF";

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

  $("mint").addEventListener("keydown", function(e) {
    if (e.key === "Enter") analyze();
  });

  $("query").addEventListener("keydown", function(e) {
    if (e.key === "Enter") analyze();
  });

  health();
  loadTrades();
  setInterval(health, 15000);
});