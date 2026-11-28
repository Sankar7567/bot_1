(function () {
  "use strict";

  let currentReport = null;
  let btSortKey = "fill_timestamp";
  let btSortAsc = false;
  let btPollTimer = null;

  const $ = (id) => document.getElementById(id);

  function fmtTs(ms) {
    if (!ms) return "-";
    return new Date(ms).toISOString().replace("T", " ").slice(0, 19);
  }

  function fmtNum(v, dec = 4) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
    return Number(v).toFixed(dec);
  }

  function fmtDur(ms) {
    if (!ms || ms <= 0) return "0m";
    const mins = Math.round(ms / 60000);
    if (mins < 60) return `${mins}m`;
    const hrs = Math.floor(mins / 60);
    return `${hrs}h ${mins % 60}m`;
  }

  function badgeClass(status) {
    if (status === "Operational") return "badge-ok";
    if (status === "Warning") return "badge-warn";
    if (status === "Failed") return "badge-fail";
    return "badge-muted";
  }

  async function api(url, opts = {}) {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    const text = await res.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch (_) {
      data = { error: text };
    }
    if (!res.ok) {
      throw new Error(data.error || `HTTP ${res.status}`);
    }
    return data;
  }

  // -------------------- Tabs --------------------
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $("tab-" + btn.dataset.tab).classList.add("active");
      if (btn.dataset.tab === "live") {
        refreshLiveStatus();
        refreshLiveTrades();
      }
    });
  });

  // -------------------- Modal --------------------
  $("modal-close").addEventListener("click", () => $("detail-modal").classList.add("hidden"));
  $("detail-modal").addEventListener("click", (e) => {
    if (e.target === $("detail-modal")) $("detail-modal").classList.add("hidden");
  });
  function showModal(title, obj) {
    $("modal-title").textContent = title;
    $("modal-body").textContent = JSON.stringify(obj, null, 2);
    $("detail-modal").classList.remove("hidden");
  }

  // -------------------- 1. System Health --------------------
  function renderHealth(data) {
    if (!data) return;
    $("top-env-badge").textContent = `ENV: ${String(data.environment || "--").toUpperCase()}`;
    $("top-env-badge").className = `badge ${data.environment === "production" ? "badge-fail" : "badge-warn"}`;

    const s = data.summary || {};
    const bar = $("health-summary-bar");
    bar.classList.remove("hidden");
    bar.innerHTML = `
      <span><strong>Overall:</strong> <span class="badge ${badgeClass(data.overall_status)}">${data.overall_status}</span></span>
      <span>Operational: <strong>${s.operational ?? 0}</strong></span>
      <span>Warning: <strong>${s.warning ?? 0}</strong></span>
      <span>Failed: <strong>${s.failed ?? 0}</strong></span>
      <span>Not configured: <strong>${s.not_configured ?? 0}</strong></span>
      <span>Backtest Ready: <strong>${data.can_backtest ? "Yes" : "No"}</strong></span>
      <span>Live Trade Ready: <strong>${data.can_live_trade ? "Yes" : "No"}</strong></span>
    `;

    const groups = { exchange: [], internal: [], configuration: [] };
    (data.checks || []).forEach((c) => {
      if (groups[c.category]) groups[c.category].push(c);
    });

    ["exchange", "internal", "configuration"].forEach((cat) => {
      const el = $("health-" + cat);
      el.classList.remove("muted");
      el.innerHTML = groups[cat]
        .map(
          (c) => `
          <div class="check-item">
            <div class="check-top">
              <strong>${c.name}</strong>
              <span class="badge ${badgeClass(c.status)}">${c.status}</span>
            </div>
            <div class="check-detail">${c.detail || ""}${c.latency_ms != null ? ` (${c.latency_ms} ms)` : ""}</div>
            ${c.error ? `<div class="check-err">Error: ${c.error}</div>` : ""}
          </div>`
        )
        .join("");
    });
  }

  $("btn-run-health").addEventListener("click", async () => {
    const btn = $("btn-run-health");
    btn.disabled = true;
    btn.textContent = "CHECKING...";
    try {
      const data = await api("/api/health/run", { method: "POST", body: "{}" });
      renderHealth(data);
    } catch (err) {
      alert("Health check failed: " + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "RUN SYSTEM CHECK";
    }
  });

  // -------------------- 2. Backtesting --------------------
  async function initBacktestDefaults() {
    try {
      const d = await api("/api/backtest/defaults");
      $("bt-start-date").value = d.default_start_date;
      $("bt-end-date").value = d.default_end_date;
      $("bt-balance").value = d.default_initial_balance;
      $("bt-leverage").value = d.default_assumed_leverage;
      $("bt-mmr").value = d.default_assumed_mmr;
      if (!d.has_credentials) {
        const opt = $("bt-metadata-mode").querySelector('option[value="VERIFIED_METADATA"]');
        if (opt) opt.textContent = "Verified Metadata Mode (Requires API keys — not configured)";
      }
    } catch (err) {
      console.error("Failed loading backtest defaults:", err);
    }
  }

  $("bt-metadata-mode").addEventListener("change", () => {
    const isResearch = $("bt-metadata-mode").value === "RESEARCH_MODE";
    $("bt-lev-wrap").classList.toggle("hidden", !isResearch);
    $("bt-mmr-wrap").classList.toggle("hidden", !isResearch);
  });

  $("backtest-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("btn-run-backtest");
    btn.disabled = true;
    $("bt-progress-card").classList.remove("hidden");
    $("bt-progress-status").textContent = "Starting...";
    $("bt-progress-pct").textContent = "0%";
    $("bt-progress-fill").style.width = "0%";

    try {
      await api("/api/backtest/start", {
        method: "POST",
        body: JSON.stringify({
          strategy_name: $("bt-strategy").value,
          start_date: $("bt-start-date").value,
          end_date: $("bt-end-date").value,
          initial_balance: parseFloat($("bt-balance").value),
          metadata_mode: $("bt-metadata-mode").value,
          assumed_max_leverage: parseInt($("bt-leverage").value, 10),
          assumed_maint_margin_ratio: parseFloat($("bt-mmr").value),
        }),
      });
      pollBacktestStatus();
    } catch (err) {
      btn.disabled = false;
      $("bt-progress-status").textContent = "Error";
      $("bt-progress-msg").textContent = err.message;
    }
  });

  function pollBacktestStatus() {
    if (btPollTimer) clearInterval(btPollTimer);
    btPollTimer = setInterval(async () => {
      try {
        const st = await api("/api/backtest/status");
        $("bt-progress-status").textContent = st.status.toUpperCase();
        $("bt-progress-pct").textContent = `${st.progress_pct || 0}%`;
        $("bt-progress-fill").style.width = `${st.progress_pct || 0}%`;
        $("bt-progress-msg").textContent = st.progress_message || "";

        if (st.status === "completed") {
          clearInterval(btPollTimer);
          btPollTimer = null;
          $("btn-run-backtest").disabled = false;
          const report = await api("/api/backtest/report");
          renderBacktestReport(report);
        } else if (st.status === "failed") {
          clearInterval(btPollTimer);
          btPollTimer = null;
          $("btn-run-backtest").disabled = false;
        }
      } catch (err) {
        console.error("Poll error:", err);
      }
    }, 800);
  }

  function renderBacktestReport(rep) {
    currentReport = rep;
    $("bt-results").classList.remove("hidden");

    const meta = rep.metadata || {};
    const stratLabel = (rep.config && rep.config.strategy_name) || "top_gainers_ha";
    const banner = $("bt-provenance-banner");
    if (meta.mode === "RESEARCH_MODE") {
      banner.className = "notice notice-warn";
      banner.innerHTML = `<strong>Strategy:</strong> <code>${stratLabel}</code> | <strong>RESEARCH-ONLY REPORT:</strong> ${meta.source}. Assumed Max Leverage: ${meta.assumed_max_leverage}x, MMR: ${meta.assumed_maint_margin_ratio}. <em>${meta.listing_limitation_note || ""}</em>`;
    } else {
      banner.className = "notice";
      banner.innerHTML = `<strong>Strategy:</strong> <code>${stratLabel}</code> | <strong>VERIFIED METADATA MODE:</strong> Source: ${meta.source} (retrieved ${fmtTs(meta.retrieved_at_ms)}). <em>${meta.listing_limitation_note || ""}</em>`;
    }

    const p = rep.performance;
    const r = rep.risk;
    const c = rep.costs;
    const eq = rep.equity;
    const st = rep.strategy;
    const dq = rep.data_quality;

    const cards = [
      ["Initial Balance", `${fmtNum(eq.starting_balance, 2)} USDT`],
      ["Final Balance", `${fmtNum(eq.ending_balance, 2)} USDT`],
      ["Net Profit / Loss", `${fmtNum(p.net_profit, 2)} USDT`],
      ["Net Return %", `${fmtNum(p.net_return_pct, 2)}%`],
      ["Total Trades", p.total_trades],
      ["Winning Trades", p.winning_trades],
      ["Losing Trades", p.losing_trades],
      ["Win Rate", `${fmtNum(p.win_rate, 2)}%`],
      ["Profit Factor", fmtNum(p.profit_factor, 2)],
      ["Avg Winning Trade", `${fmtNum(p.average_win, 2)} USDT`],
      ["Avg Losing Trade", `${fmtNum(p.average_loss, 2)} USDT`],
      ["Avg Realized R:R", `${fmtNum(p.average_rr_achieved, 2)}R`],
      ["Max Drawdown (USDT)", `${fmtNum(r.max_drawdown_usdt, 2)} USDT`],
      ["Max Drawdown (%)", `${fmtNum(r.max_drawdown_pct, 2)}%`],
      ["Total Trading Fees", `${fmtNum(c.total_fees, 4)} USDT`],
      ["Total Slippage Cost", `${fmtNum(c.estimated_slippage_cost, 4)} USDT`],
      ["Funding Costs", `${fmtNum(c.funding_paid, 4)} USDT`],
      ["Avg Holding Duration", fmtDur(p.average_holding_time_ms)],
      ["Strategy Signals", st.total_signals],
      ["Rejected Signals", st.rejected_signals],
      ["Risk-Validation Failures", st.risk_validation_failures],
      ["Data-Quality Issues", dq.issues_count],
    ];

    $("bt-metrics-grid").innerHTML = cards
      .map(
        ([k, v]) => `
        <div class="metric-box">
          <div class="metric-label">${k}</div>
          <div class="metric-val">${v}</div>
        </div>`
      )
      .join("");

    const db = rep.direction_breakdown || { LONG: {}, SHORT: {} };
    $("bt-direction-stats").innerHTML = `
      <div class="kv-grid">
        <div class="kv-key">LONG</div><div>${db.LONG.trades || 0} trades | ${db.LONG.wins || 0} wins (${fmtNum(db.LONG.win_rate || 0, 1)}%) | Net: ${fmtNum(db.LONG.net_pnl || 0, 2)} USDT</div>
        <div class="kv-key">SHORT</div><div>${db.SHORT.trades || 0} trades | ${db.SHORT.wins || 0} wins (${fmtNum(db.SHORT.win_rate || 0, 1)}%) | Net: ${fmtNum(db.SHORT.net_pnl || 0, 2)} USDT</div>
      </div>
    `;

    renderEquityCurve(eq.realized_equity_curve || []);
    renderBacktestTrades();
  }

  function renderEquityCurve(points) {
    const container = $("bt-equity-chart");
    if (!points || points.length < 2) {
      container.innerHTML = `<p class="muted small">Not enough equity points to plot curve (${points.length} point).</p>`;
      return;
    }
    const vals = points.map((pt) => pt[1]);
    const minV = Math.min(...vals);
    const maxV = Math.max(...vals);
    const span = maxV - minV || 1;
    const w = 500;
    const h = 140;
    const coords = points
      .map((pt, idx) => {
        const x = (idx / (points.length - 1)) * w;
        const y = h - 10 - ((pt[1] - minV) / span) * (h - 20);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
    container.innerHTML = `
      <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
        <polyline fill="none" stroke="#1d4ed8" stroke-width="2" points="${coords}" />
      </svg>
      <div class="small muted">Min: ${fmtNum(minV, 2)} USDT | Max: ${fmtNum(maxV, 2)} USDT</div>
    `;
  }

  function renderBacktestTrades() {
    if (!currentReport) return;
    let trades = [...(currentReport.trades || [])];

    const symQ = $("bt-filter-symbol").value.trim().toUpperCase();
    const dirQ = $("bt-filter-direction").value;
    const outQ = $("bt-filter-outcome").value;
    const exitQ = $("bt-filter-exit").value.trim().toUpperCase();
    const fromQ = $("bt-filter-from").value;
    const toQ = $("bt-filter-to").value;

    const fromMs = fromQ ? Date.parse(fromQ + "T00:00:00Z") : null;
    const toMs = toQ ? Date.parse(toQ + "T23:59:59Z") : null;

    trades = trades.filter((t) => {
      if (symQ && !t.symbol.includes(symQ)) return false;
      if (dirQ && t.direction !== dirQ) return false;
      if (outQ === "WIN" && !(t.net_pnl > 0)) return false;
      if (outQ === "LOSS" && !(t.net_pnl <= 0)) return false;
      if (exitQ && !String(t.exit_reason || "").toUpperCase().includes(exitQ)) return false;
      if (fromMs && t.fill_timestamp < fromMs) return false;
      if (toMs && t.fill_timestamp > toMs) return false;
      return true;
    });

    trades.sort((a, b) => {
      const av = a[btSortKey];
      const bv = b[btSortKey];
      if (av === bv) return 0;
      const cmp = av > bv ? 1 : -1;
      return btSortAsc ? cmp : -cmp;
    });

    $("bt-trade-count").textContent = String(trades.length);
    const tbody = $("bt-trades-table").querySelector("tbody");
    tbody.innerHTML = "";
    trades.forEach((t) => {
      const tr = document.createElement("tr");
      const fees = (t.entry_fee || 0) + (t.exit_fee || 0);
      const pnlCls = t.net_pnl >= 0 ? "pnl-pos" : "pnl-neg";
      tr.innerHTML = `
        <td>${t.trade_id}</td>
        <td><strong>${t.symbol}</strong></td>
        <td>${t.direction}</td>
        <td>#${t.candidate_rank}</td>
        <td>${fmtTs(t.trigger_15m_timestamp)}</td>
        <td>${fmtTs(t.confirmation_1m_timestamp)}</td>
        <td>${fmtTs(t.fill_timestamp)}</td>
        <td>${fmtNum(t.fill_price, 4)}</td>
        <td>${fmtNum(t.sl_price, 4)}</td>
        <td>${fmtNum(t.tp_price, 4)}</td>
        <td>${fmtNum(t.quantity, 4)}</td>
        <td>${t.leverage}x</td>
        <td>${fmtTs(t.exit_timestamp)}</td>
        <td>${fmtNum(t.exit_fill_price, 4)}</td>
        <td>${t.exit_reason}</td>
        <td>${fmtNum(t.gross_pnl, 4)}</td>
        <td>${fmtNum(fees, 4)}</td>
        <td>${fmtNum(t.funding_paid, 4)}</td>
        <td>${fmtNum(t.slippage_cost, 4)}</td>
        <td class="${pnlCls}">${fmtNum(t.net_pnl, 4)}</td>
        <td>${fmtNum(t.equity_after_trade, 2)}</td>
      `;
      tr.addEventListener("click", () => showModal(`Backtest Trade ${t.trade_id}`, t));
      tbody.appendChild(tr);
    });
  }

  ["bt-filter-symbol", "bt-filter-direction", "bt-filter-outcome", "bt-filter-exit", "bt-filter-from", "bt-filter-to"].forEach((id) => {
    $(id).addEventListener("input", renderBacktestTrades);
    $(id).addEventListener("change", renderBacktestTrades);
  });

  $("bt-trades-table").querySelectorAll("th[data-sort]").forEach((th) => {
    th.addEventListener("click", () => {
      const key = th.dataset.sort;
      if (btSortKey === key) btSortAsc = !btSortAsc;
      else {
        btSortKey = key;
        btSortAsc = true;
      }
      renderBacktestTrades();
    });
  });

  $("btn-bt-export-csv").addEventListener("click", () => {
    window.location.href = "/api/backtest/export.csv";
  });

  // -------------------- 3. Live Trading --------------------
  $("live-target-env").addEventListener("change", () => {
    const isProd = $("live-target-env").value === "production";
    $("prod-confirm-box").classList.toggle("hidden", !isProd);
  });

  async function refreshLiveStatus() {
    try {
      const st = await api("/api/live/status");
      const warn = $("live-creds-warning");
      if (!st.credentials_configured) {
        warn.classList.remove("hidden");
        warn.textContent = st.missing_credentials_message || "API credentials not configured.";
      } else {
        warn.classList.add("hidden");
      }

      const running = st.state === "running" || st.state === "starting";
      $("btn-live-start").disabled = !st.live_enabled || running;
      $("btn-live-stop").disabled = !running;
      $("btn-live-emergency").disabled = !st.live_enabled || !st.open_position;

      const pos = st.open_position;
      $("live-status-panel").innerHTML = `
        <div class="kv-key">Bot State</div><div><strong>${String(st.state).toUpperCase()}</strong></div>
        <div class="kv-key">Strategy</div><div><code>${st.strategy_name || "top_gainers_ha"}</code></div>
        <div class="kv-key">Environment</div><div>${String(st.environment).toUpperCase()}</div>
        <div class="kv-key">Exchange Connected</div><div>${st.exchange_connected ? "Yes" : "No"}</div>
        <div class="kv-key">Account Balance</div><div>${st.account_balance != null ? fmtNum(st.account_balance, 2) + " USDT" : "-"}</div>
        <div class="kv-key">Available Balance</div><div>${st.available_balance != null ? fmtNum(st.available_balance, 2) + " USDT" : "-"}</div>
        <div class="kv-key">Open Position</div><div>${pos ? `${pos.symbol} (${pos.direction})` : "None"}</div>
        <div class="kv-key">Entry Price</div><div>${pos ? fmtNum(pos.entry_price, 4) : "-"}</div>
        <div class="kv-key">Stop-Loss / TP</div><div>${pos ? `${fmtNum(pos.stop_loss, 4)} / ${fmtNum(pos.take_profit, 4)}` : "-"}</div>
        <div class="kv-key">Leverage</div><div>${pos ? `${pos.leverage}x` : "-"}</div>
        <div class="kv-key">Unrealized P&amp;L</div><div>${pos && pos.unrealized_pnl != null ? fmtNum(pos.unrealized_pnl, 4) + " USDT" : "-"}</div>
        <div class="kv-key">Protective Orders</div><div>${pos ? pos.protective_status : "-"}</div>
        <div class="kv-key">Last Error</div><div class="pnl-neg">${st.last_error || "-"}</div>
      `;
    } catch (err) {
      console.error("Live status refresh failed:", err);
    }
  }

  $("btn-live-start").addEventListener("click", async () => {
    const msg = $("live-action-msg");
    msg.textContent = "Running preflight checks and starting...";
    try {
      await api("/api/live/start", {
        method: "POST",
        body: JSON.stringify({
          strategy_name: $("live-strategy").value,
          target_env: $("live-target-env").value,
          confirm_production: $("live-confirm-prod").checked,
          confirmation_phrase: $("live-confirm-phrase").value,
        }),
      });
      msg.textContent = "Live trading engine started.";
      await refreshLiveStatus();
    } catch (err) {
      msg.textContent = "Start refused: " + err.message;
    }
  });

  $("btn-live-stop").addEventListener("click", async () => {
    const msg = $("live-action-msg");
    msg.textContent = "Stopping new entries (preserving open position protection)...";
    try {
      await api("/api/live/stop", { method: "POST", body: "{}" });
      msg.textContent = "Strategy stopped. Any open position remains protected by exchange SL/TP.";
      await refreshLiveStatus();
    } catch (err) {
      msg.textContent = "Stop error: " + err.message;
    }
  });

  $("btn-live-emergency").addEventListener("click", async () => {
    if (!confirm("EMERGENCY CLOSE: Immediately submit a reduceOnly market order to close the active position?")) {
      return;
    }
    const msg = $("live-action-msg");
    try {
      const res = await api("/api/live/emergency-close", {
        method: "POST",
        body: JSON.stringify({ confirm_emergency: true }),
      });
      msg.textContent = res.message || "Emergency close submitted.";
      await refreshLiveStatus();
      await refreshLiveTrades();
    } catch (err) {
      msg.textContent = "Emergency close error: " + err.message;
    }
  });

  async function refreshLiveTrades() {
    try {
      const params = new URLSearchParams();
      const sym = $("live-filter-symbol").value.trim().toUpperCase();
      const dir = $("live-filter-direction").value;
      const st = $("live-filter-status").value;
      if (sym) params.set("symbol", sym);
      if (dir) params.set("direction", dir);
      if (st) params.set("status", st);

      const data = await api("/api/live/trades?" + params.toString());
      const tbody = $("live-trades-table").querySelector("tbody");
      tbody.innerHTML = "";
      (data.trades || []).forEach((t) => {
        const tr = document.createElement("tr");
        const fees = (t.entry_fee || 0) + (t.exit_fee || 0);
        const pnlCls = t.net_pnl >= 0 ? "pnl-pos" : "pnl-neg";
        tr.innerHTML = `
          <td>${t.trade_id}</td>
          <td>${t.environment}</td>
          <td><strong>${t.symbol}</strong></td>
          <td>${t.direction}</td>
          <td>${t.status}</td>
          <td>${fmtTs(t.entry_timestamp)}</td>
          <td>${fmtNum(t.entry_price, 4)}</td>
          <td>${fmtNum(t.quantity, 4)}</td>
          <td>${t.leverage}x</td>
          <td>${fmtNum(t.stop_loss, 4)}</td>
          <td>${fmtNum(t.take_profit, 4)}</td>
          <td>${fmtTs(t.exit_timestamp)}</td>
          <td>${t.exit_price != null ? fmtNum(t.exit_price, 4) : "-"}</td>
          <td>${t.exit_reason || "-"}</td>
          <td>${fmtNum(fees, 4)}</td>
          <td class="${pnlCls}">${fmtNum(t.net_pnl, 4)}</td>
        `;
        tr.addEventListener("click", () => showModal(`Live Trade ${t.trade_id}`, t));
        tbody.appendChild(tr);
      });
    } catch (err) {
      console.error("Failed loading live trades:", err);
    }
  }

  $("btn-live-refresh-trades").addEventListener("click", refreshLiveTrades);
  ["live-filter-symbol", "live-filter-direction", "live-filter-status"].forEach((id) => {
    $(id).addEventListener("input", refreshLiveTrades);
    $(id).addEventListener("change", refreshLiveTrades);
  });
  $("btn-live-export-csv").addEventListener("click", () => {
    window.location.href = "/api/live/trades/export.csv";
  });

  async function syncStateOnLoad() {
    try {
      const h = await api("/api/health");
      if (h && h.health) renderHealth(h.health);
    } catch (_) {}

    try {
      const st = await api("/api/backtest/status");
      if (st.strategy_name) $("bt-strategy").value = st.strategy_name;
      if (st.status === "downloading" || st.status === "running") {
        $("btn-run-backtest").disabled = true;
        $("bt-progress-card").classList.remove("hidden");
        $("bt-progress-status").textContent = st.status.toUpperCase();
        $("bt-progress-pct").textContent = `${st.progress_pct || 0}%`;
        $("bt-progress-fill").style.width = `${st.progress_pct || 0}%`;
        $("bt-progress-msg").textContent = st.progress_message || "";
        pollBacktestStatus();
      } else if (st.status === "completed") {
        $("bt-progress-card").classList.remove("hidden");
        $("bt-progress-status").textContent = "COMPLETED";
        $("bt-progress-pct").textContent = "100%";
        $("bt-progress-fill").style.width = "100%";
        $("bt-progress-msg").textContent = st.progress_message || "";
        const report = await api("/api/backtest/report");
        renderBacktestReport(report);
      } else if (st.status === "failed") {
        $("bt-progress-card").classList.remove("hidden");
        $("bt-progress-status").textContent = "FAILED";
        $("bt-progress-pct").textContent = `${st.progress_pct || 0}%`;
        $("bt-progress-fill").style.width = `${st.progress_pct || 0}%`;
        $("bt-progress-msg").textContent = st.error || st.progress_message || "";
      }
    } catch (_) {}
  }

  // Boot
  initBacktestDefaults();
  syncStateOnLoad();
  refreshLiveStatus();
  refreshLiveTrades();
})();
