const $ = (id) => document.getElementById(id);

async function health() {
  try {
    const r = await fetch("/api/health?x=" + Date.now(), {
      cache: "no-store"
    });

    const j = await r.json();

    $("health").textContent = j.status || "ONLINE";

    const sources = j.sources || {};

    setSource("X", "dotX", "srcX", sources.X);
    setSource("Birdeye", "dotBE", "srcBE", sources.Birdeye);
    setSource("Helius", "dotHE", "srcHE", sources.Helius);
  } catch (err) {
    console.error(err);
    $("health").textContent = "BACKEND ERROR";
  }
}

function setSource(name, dotId, textId, source) {
  if (!source) return;

  const ok = source.configured === true;

  const dot = $(dotId);
  dot.className = "dot " + (ok ? "ok" : "bad");

  $(textId).textContent = source.state || "UNKNOWN";
}

async function analyze() {
  const mint = $("mint").value.trim();

  if (!mint) {
    alert("Paste a Solana token contract address first.");
    return;
  }

  $("setupState").textContent = "LOADING...";
  $("setupWhy").textContent = "Fetching live data...";
  $("score").textContent = "LOADING...";
  $("riskLevel").textContent = "LOADING...";

  try {
    const response = await fetch("/api/analyze", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        mint: mint,
        x_query: $("query").value.trim()
      })
    });

    const data = await response.json();

    $("raw").textContent = JSON.stringify(data, null, 2);

    if (!response.ok) {
      throw new Error(data.detail || "Analyze request failed");
    }

    renderSetup(data.setup);
    renderScore(data.score);
    renderRisk(data.risk);
    renderSocial(data.social);
    renderToken(data.overview);
    renderTweets(data);

    console.log("ANALYZE RESULT:", data);

  } catch (error) {
    console.error("ANALYZE ERROR:", error);

    $("setupState").textContent = "ERROR";
    $("setupWhy").textContent = error.message;

    $("raw").textContent =
      "ANALYZE ERROR\n\n" + error.stack;
  }
}

function renderSetup(s) {
  s = s || {};

  $("setupState").textContent =
    s.state || "DATA NOT AVAILABLE";

  $("prevHigh").textContent =
    formatNumber(s.prev_high);

  $("higherLow").textContent =
    formatNumber(s.higher_low);

  $("stop").textContent =
    formatNumber(s.stop);

  $("setupWhy").textContent =
    s.last_reason || "No setup detected.";
}

function renderScore(s) {
  if (!s) {
    $("score").textContent = "DATA NOT AVAILABLE";
    return;
  }

  $("score").textContent =
    s.score == null ? "—" : s.score + "/100";

  const components = s.components || {};

  $("scoreRows").innerHTML =
    Object.entries(components)
      .map(([name, value]) => {
        return `
          <div class="row">
            <span>${pretty(name)}</span>
            <b>${value.value}/100</b>
          </div>
        `;
      })
      .join("");
}

function renderRisk(r) {
  if (!r) {
    $("riskLevel").textContent = "DATA NOT AVAILABLE";
    return;
  }

  $("riskLevel").textContent =
    r.overall || "UNKNOWN";

  $("riskRows").innerHTML =
    (r.flags || [])
      .map(flag => {
        return `
          <div class="row">
            <span>${pretty(flag.code)}</span>
            <b>${flag.level}</b>
          </div>
          <div class="muted">${flag.reason}</div>
        `;
      })
      .join("") ||
    `<div class="row"><span>No returned risk flags</span><b>—</b></div>`;
}

function renderSocial(s) {
  s = s || {};

  $("posts").textContent =
    s.post_count ?? "—";

  $("vel").textContent =
    s.mention_velocity == null
      ? "DATA NOT AVAILABLE"
      : Number(s.mention_velocity).toFixed(2);

  $("sentiment").textContent =
    s.sentiment == null
      ? "DATA NOT AVAILABLE"
      : s.sentiment;

  $("domination").textContent =
    s.domination == null
      ? "DATA NOT AVAILABLE"
      : (s.domination * 100).toFixed(1) + "%";

  $("dupes").textContent =
    s.duplicates ?? "—";

  $("socialState").textContent =
    s.state || "X";
}

function renderToken(o) {
  if (!o || typeof o !== "object") {
    $("tokenName").textContent = "DATA NOT AVAILABLE";
    $("tokenRows").innerHTML =
      `<div class="muted">No market overview returned.</div>`;
    return;
  }

  $("tokenName").textContent =
    o.symbol || o.name || "TOKEN";

  $("tokenRows").innerHTML = `
    ${row("Price", money(o.price))}
    ${row("Liquidity", moneyUSD(o.liquidity))}
    ${row("Market Cap", moneyUSD(o.marketCap))}
    ${row("24h Volume", moneyUSD(o.v24hUSD))}
  `;
}

function renderTweets(data) {
  const social = data.social || {};

  $("tweetCount").textContent =
    social.post_count
      ? social.post_count + " returned"
      : "X unavailable";

  $("tweets").innerHTML = `
    <div class="tweet">
      <div class="meta">
        X status: ${data.sources?.X || "UNKNOWN"}
      </div>
      <p>
        ${
          data.social?.state === "READY"
            ? "X data received."
            : "X data is unavailable or API credits are exhausted."
        }
      </p>
    </div>
  `;
}

function row(name, value) {
  return `
    <div class="row">
      <span>${name}</span>
      <b>${value}</b>
    </div>
  `;
}

function money(value) {
  if (value == null) return "DATA NOT AVAILABLE";

  const n = Number(value);

  if (!Number.isFinite(n)) return "DATA NOT AVAILABLE";

  return n.toLocaleString(undefined, {
    maximumFractionDigits: 8
  });
}

function moneyUSD(value) {
  if (value == null) return "DATA NOT AVAILABLE";

  const n = Number(value);

  if (!Number.isFinite(n)) return "DATA NOT AVAILABLE";

  return "$" + n.toLocaleString(undefined, {
    maximumFractionDigits: 2
  });
}

function formatNumber(value) {
  if (value == null) return "—";

  const n = Number(value);

  if (!Number.isFinite(n)) return "—";

  return n.toLocaleString(undefined, {
    maximumFractionDigits: 8
  });
}

function pretty(value) {
  return String(value)
    .replaceAll("_", " ")
    .replace(/\b\w/g, c => c.toUpperCase());
}

document.addEventListener("DOMContentLoaded", () => {

  console.log("MEME INTEL JAVASCRIPT LOADED");

  const button = $("analyze");

  if (!button) {
    console.error("ANALYZE BUTTON NOT FOUND");
    return;
  }

  button.addEventListener("click", analyze);

  health();

  setInterval(health, 15000);
});