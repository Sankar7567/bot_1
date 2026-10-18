"""
Streamlit Web Dashboard for Binance Futures ML Crypto Research Platform.

Features:
- Real-time disk storage meter and system health guardrails (< 20GB limit).
- Risk Sizing Optimizer: Side-by-side comparison of 2%, 3%, and 5% account risk compounding from $100.
- Global Multi-Symbol Universe Scanner: Simultaneous 500+ symbol scan with strict global 1-trade-at-a-time execution.
- Decoupled Live Signal Monitor: Displays signals/latest_signal.json and real-time streaming audit log.
- Interactive Plotly visualizations for compounding equity curves, underwater drawdowns, and feature importance.
"""

import os
import json
from pathlib import Path
from datetime import datetime, timezone

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px

from data_pipeline import DataPipeline, desanitize_filename_to_symbol, sanitize_symbol_for_filename
from features import FeatureEngineer
from model import ModelTrainer
from backtester import BacktestEngine, compare_risk_profiles
from scanner import GlobalUniverseScanner
from live_signals import LiveSignalGenerator, LATEST_SIGNAL_PATH, ACTIVE_TRADE_PATH, AUDIT_LOG_PATH

# Page configuration
st.set_page_config(
    page_title="Binance Futures ML Research Platform",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Dark theme styling
st.markdown("""
<style>
    .main { background-color: #0e1117; }
    .stMetric {
        background-color: #1e222d;
        border: 1px solid #2e3346;
        border-radius: 8px;
        padding: 12px;
    }
    .signal-card {
        background-color: #1a1e29;
        border-radius: 8px;
        padding: 16px;
        border-left: 5px solid #00c087;
        margin-bottom: 12px;
    }
    .audit-box {
        background-color: #121620;
        border: 1px solid #262c3d;
        border-radius: 6px;
        padding: 12px;
        font-family: monospace;
        font-size: 13px;
        max-height: 400px;
        overflow-y: scroll;
    }
</style>
""", unsafe_allow_html=True)


@st.cache_resource
def get_platform_components():
    """Initializes and caches platform singletons."""
    pipeline = DataPipeline()
    feature_engineer = FeatureEngineer(pipeline)
    trainer = ModelTrainer(pipeline=pipeline)
    trainer.load_model()
    scanner = GlobalUniverseScanner(pipeline=pipeline)
    return pipeline, feature_engineer, trainer, scanner


pipeline, fe, trainer, scanner = get_platform_components()

# --- SIDEBAR CONFIGURATION ---
st.sidebar.title("⚡ Control Panel")
st.sidebar.markdown("---")

# Storage & System Guardrail Meter
storage_gb = pipeline.get_storage_usage_gb()
free_gb = pipeline.get_system_free_disk_gb()
storage_pct = min(100.0, (storage_gb / 20.0) * 100.0)

st.sidebar.subheader("💾 Disk Usage Guardrail (< 20GB)")
st.sidebar.progress(storage_pct / 100.0)
st.sidebar.caption(f"Data Dir: **{storage_gb:.3f} GB** / 20.0 GB Limit ({storage_pct:.1f}%)")
if storage_gb >= 18.0:
    st.sidebar.warning("⚠️ Storage approaching 18GB threshold! Pruning will trigger.")
st.sidebar.caption(f"Host Free Disk: **{free_gb:.1f} GB**")

st.sidebar.markdown("---")
st.sidebar.subheader("💰 Capital & Compounding")
initial_capital = st.sidebar.number_input("Starting Capital ($)", value=100.0, step=25.0)

st.sidebar.subheader("🎯 Risk & Strategy Parameters")
sl_atr_mult = st.sidebar.slider("Stop-Loss (x ATR)", min_value=0.5, max_value=3.0, value=1.0, step=0.1)
tp_atr_mult = st.sidebar.slider("Take-Profit (x ATR)", min_value=1.0, max_value=6.0, value=3.0, step=0.1)
rr_ratio = tp_atr_mult / sl_atr_mult
st.sidebar.info(f"**Risk-Reward Ratio:** 1 : {rr_ratio:.2f}")

confidence_threshold = st.sidebar.slider(
    "ML Probability Threshold",
    min_value=0.20,
    max_value=0.70,
    value=0.30,
    step=0.01
)

max_horizon_bars = st.sidebar.slider("Max Holding Bars", min_value=6, max_value=72, value=24, step=6)

# Available symbols
available_files = list(pipeline.features_dir.rglob("*.parquet"))
if not available_files:
    available_files = list(pipeline.raw_dir.rglob("*.parquet"))
symbol_choices = [desanitize_filename_to_symbol(f.name) for f in available_files]
if not symbol_choices:
    symbol_choices = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]

selected_symbol = st.sidebar.selectbox("Focal Symbol", symbol_choices, index=0)
timeframe = st.sidebar.selectbox("Base Timeframe", ["5m", "1m", "15m", "1h"], index=0)

st.sidebar.markdown("---")
st.sidebar.subheader("💸 Realistic Frictions")
taker_fee_pct = st.sidebar.number_input("Taker Fee %", value=0.05, step=0.01) / 100.0
maker_fee_pct = st.sidebar.number_input("Maker Fee %", value=0.02, step=0.01) / 100.0
funding_rate_8h_pct = st.sidebar.number_input("8h Funding Rate %", value=0.01, step=0.005) / 100.0

# --- MAIN CONTENT AREA ---
st.title("⚡ Binance Futures ML Crypto Research Platform")
st.markdown("Multi-Symbol Rolling-Batch Trained Quantitative Platform with 1:3 RR and Compounding Risk Sizing.")

# Quick Status Bar
top_cols = st.columns(4)
top_cols[0].metric("Focal Asset", selected_symbol)
top_cols[1].metric("Starting Balance", f"${initial_capital:.2f}")
top_cols[2].metric("Risk-Reward Setup", f"1 : {rr_ratio:.1f}")
top_cols[3].metric("Model State", "Active & Loaded" if trainer.model else "Unloaded")

st.markdown("---")

# Main Navigation Tabs
tab_optimizer, tab_global, tab_live, tab_single = st.tabs([
    "🎯 Risk Sizing Optimizer (2% vs 3% vs 5%)",
    "🌐 Global Universe Scanner",
    "📡 Decoupled Live Signals & Audit",
    "📊 Single Symbol Drilldown"
])

# -------------------------------------------------------------
# TAB 1: RISK SIZING OPTIMIZER (2% vs 3% vs 5% Compounding)
# -------------------------------------------------------------
with tab_optimizer:
    st.subheader(f"Compounding Risk Sizing Comparison: {selected_symbol}")
    st.markdown("Tests and compares **2%, 3%, and 5%** account equity risk per trade starting with **$100** capital.")

    if st.button("🚀 Run Risk Profile Optimization", type="primary"):
        with st.spinner(f"Simulating compounding equity curves for 2%, 3%, and 5% risk on {selected_symbol}..."):
            df_feat = fe.load_feature_dataset(timeframe, selected_symbol)
            if df_feat.empty:
                df_feat = fe.build_aligned_multi_timeframe_features(selected_symbol, base_timeframe=timeframe)
                if not df_feat.empty:
                    fe.save_feature_dataset(df_feat, timeframe, selected_symbol)

            if not df_feat.empty:
                opt_res = compare_risk_profiles(
                    df_feat,
                    symbol=selected_symbol,
                    risks=[0.02, 0.03, 0.05],
                    initial_capital=initial_capital,
                    confidence_threshold=confidence_threshold,
                    model_trainer=trainer
                )
                st.session_state["risk_opt_res"] = opt_res
            else:
                st.error(f"No feature data available for {selected_symbol}.")

    opt_res = st.session_state.get("risk_opt_res")
    if opt_res:
        comp_table = opt_res["comparison_table"]
        runs = opt_res["runs"]
        optimal_risk = opt_res["optimal_risk"]

        st.success(f"🏆 **Optimal Risk Configuration: {int(optimal_risk * 100)}% Risk per Trade** (Superior Risk-Adjusted Calmar & Sharpe)")

        # Side-by-Side KPI Cards
        cols = st.columns(3)
        risk_labels = ["2% Risk", "3% Risk", "5% Risk"]
        colors = ["#17a2b8", "#28a745", "#ffc107"]

        for idx, r_pct in enumerate([0.02, 0.03, 0.05]):
            row = comp_table[comp_table["risk_pct"] == r_pct].iloc[0]
            with cols[idx]:
                st.markdown(
                    f"<div class='metric-card' style='border-left-color:{colors[idx]};'>"
                    f"<h4>{int(r_pct*100)}% Risk per Trade</h4>"
                    f"Final Equity: <b>${row['final_equity']:.2f}</b><br>"
                    f"Compounding Return: <b>{row['compounding_return_pct']:.2f}%</b><br>"
                    f"Max Drawdown: <b>{row['max_drawdown_pct']:.2f}%</b><br>"
                    f"Win Rate: <b>{row['win_rate_pct']:.2f}%</b> ({row['tp_hits']} TP / {row['sl_hits']} SL)<br>"
                    f"Profit Factor: <b>{row['profit_factor']:.2f}</b><br>"
                    f"Sharpe Ratio: <b>{row['sharpe_ratio']:.2f}</b><br>"
                    f"Calmar Ratio: <b>{row['calmar_ratio']:.2f}</b>"
                    f"</div>",
                    unsafe_allow_html=True
                )

        # Comparative Interactive Equity Curve
        fig_comp = go.Figure()
        for idx, (label, run_data) in enumerate(runs.items()):
            eq_curve = run_data["equity_curve"]
            fig_comp.add_trace(go.Scatter(
                x=eq_curve["timestamp"],
                y=eq_curve["equity"],
                mode="lines",
                name=label,
                line=dict(width=2.5, color=colors[idx])
            ))

        fig_comp.update_layout(
            title=f"Compounding Equity Growth ($100 Starting Balance) - {selected_symbol}",
            xaxis_title="Timestamp",
            yaxis_title="Equity (USD, Log Scale)",
            yaxis_type="log",
            template="plotly_dark",
            height=480,
            hovermode="x unified"
        )
        st.plotly_chart(fig_comp, use_container_width=True)

        # Drawdown Profiles Comparison
        fig_dd_comp = go.Figure()
        for idx, (label, run_data) in enumerate(runs.items()):
            eq_curve = run_data["equity_curve"]
            if "drawdown" in eq_curve.columns:
                fig_dd_comp.add_trace(go.Scatter(
                    x=eq_curve["timestamp"],
                    y=eq_curve["drawdown"] * 100.0,
                    mode="lines",
                    name=f"{label} Drawdown",
                    line=dict(width=1.5, color=colors[idx])
                ))

        fig_dd_comp.update_layout(
            title="Underwater Drawdown Profiles Comparison (%)",
            xaxis_title="Timestamp",
            yaxis_title="Drawdown (%)",
            template="plotly_dark",
            height=280
        )
        st.plotly_chart(fig_dd_comp, use_container_width=True)

# -------------------------------------------------------------
# TAB 2: GLOBAL MULTI-SYMBOL UNIVERSE SCANNER
# -------------------------------------------------------------
with tab_global:
    st.subheader("Global Universe Scanner (Simultaneous Multi-Symbol Scan)")
    st.markdown(
        "Simulates scanning the entire universe simultaneously across time. "
        "Enforces **Strict Global Single Position Invariance**: only **one trade at a time globally** "
        "on the setup with highest model confidence."
    )

    g_col1, g_col2 = st.columns([1, 3])
    with g_col1:
        global_risk = st.selectbox("Global Risk per Trade", [0.02, 0.03, 0.05], index=1)
        run_global_btn = st.button("🌐 Execute Universe Scan Backtest", type="primary", use_container_width=True)

    if run_global_btn or "global_backtest_res" not in st.session_state:
        with st.spinner("Executing simultaneous universe scan across all available symbols..."):
            g_res = scanner.run_global_backtest(
                timeframe=timeframe,
                confidence_threshold=confidence_threshold,
                risk_per_trade=global_risk,
                sl_atr_mult=sl_atr_mult,
                tp_atr_mult=tp_atr_mult,
                max_holding_bars=max_horizon_bars
            )
            st.session_state["global_backtest_res"] = g_res

    g_res = st.session_state.get("global_backtest_res", {})
    if g_res and "metrics" in g_res:
        g_metrics = g_res["metrics"]
        g_trades = g_res["trades"]
        g_equity = g_res["equity_curve"]

        # Global KPIs
        gkpi = st.columns(6)
        gkpi[0].metric("Global Final Equity", f"${g_metrics['final_equity']:.2f}", delta=f"${g_metrics['net_profit']:.2f}")
        gkpi[1].metric("Compounding Return", f"{g_metrics['compounding_return_pct']:.2f}%")
        gkpi[2].metric("Win Rate", f"{g_metrics['win_rate_pct']:.2f}%", f"{g_metrics['tp_hits']} TP / {g_metrics['sl_hits']} SL")
        gkpi[3].metric("Profit Factor", f"{g_metrics['profit_factor']:.2f}")
        gkpi[4].metric("Max Drawdown", f"{g_metrics['max_drawdown_pct']:.2f}%")
        gkpi[5].metric("Total Global Trades", str(g_metrics["total_trades"]))

        # Global Equity Curve
        fig_g = go.Figure()
        fig_g.add_trace(go.Scatter(
            x=g_equity["timestamp"],
            y=g_equity["equity"],
            mode="lines",
            name="Global Portfolio Equity",
            line=dict(color="#00c087", width=2)
        ))
        fig_g.update_layout(
            title="Global Portfolio Equity Curve (Strict 1-Trade Globally across Universe)",
            xaxis_title="Timestamp",
            yaxis_title="Equity (USD, Log Scale)",
            yaxis_type="log",
            template="plotly_dark",
            height=450
        )
        st.plotly_chart(fig_g, use_container_width=True)

        # Cross-Symbol Trade Distribution
        dist = g_res.get("symbol_trade_distribution", {})
        if dist:
            st.markdown("##### Global Trade Distribution by Symbol")
            dist_df = pd.DataFrame(list(dist.items()), columns=["Symbol", "Trades"])
            fig_dist = px.bar(
                dist_df,
                x="Symbol",
                y="Trades",
                title="Number of High-Conviction Trades Triggered per Asset",
                color="Trades",
                color_continuous_scale="Teal"
            )
            fig_dist.update_layout(template="plotly_dark", height=300)
            st.plotly_chart(fig_dist, use_container_width=True)

# -------------------------------------------------------------
# TAB 3: DECOUPLED LIVE SIGNALS & AUDIT
# -------------------------------------------------------------
with tab_live:
    st.subheader("Decoupled Live Signal Monitor & Audit Trail")
    st.markdown("Displays real-time JSON signals (`signals/latest_signal.json`) and audit logs (`logs/platform_audit.log`).")

    col_btn1, col_btn2 = st.columns([1, 3])
    with col_btn1:
        if st.button("📡 Scan Universe Now", type="primary", use_container_width=True):
            with st.spinner("Polling live candles and running LightGBM inference..."):
                gen = LiveSignalGenerator(
                    symbols=symbol_choices,
                    confidence_threshold=confidence_threshold,
                    risk_pct=0.03
                )
                sig = gen.scan_universe_and_emit_signal()
                if sig:
                    st.success(f"Signal emitted for {sig['symbol']}!")
                else:
                    st.info("No symbol met the confidence threshold.")

    # Display Latest Signal JSON
    col_sig, col_state = st.columns(2)
    with col_sig:
        st.markdown("#### Latest Generated Signal (`latest_signal.json`)")
        if LATEST_SIGNAL_PATH.exists():
            try:
                sig_content = json.loads(LATEST_SIGNAL_PATH.read_text())
                st.markdown(
                    f"<div class='signal-card'>"
                    f"<h3>{sig_content.get('direction', 'LONG')} {sig_content.get('symbol')}</h3>"
                    f"Timestamp: <b>{sig_content.get('timestamp')}</b><br>"
                    f"Model Confidence: <b style='color:#00c087;'>{sig_content.get('confidence', 0.0)*100:.1f}%</b><br>"
                    f"Entry Price: <b>${sig_content.get('entry_price'):.4f}</b><br>"
                    f"Stop-Loss: <b style='color:#ff4d4f;'>${sig_content.get('stop_loss'):.4f}</b><br>"
                    f"Take-Profit: <b style='color:#00c087;'>${sig_content.get('take_profit'):.4f}</b> (1:3 RR)<br>"
                    f"Risk %: <b>{sig_content.get('risk_pct')}%</b><br>"
                    f"Suggested Position Size: <b>{sig_content.get('suggested_position_size')}</b><br>"
                    f"Status: <b>{sig_content.get('status')}</b>"
                    f"</div>",
                    unsafe_allow_html=True
                )
                with st.expander("Raw Signal JSON Payload"):
                    st.json(sig_content)
            except Exception as e:
                st.error(f"Error reading signal JSON: {e}")
        else:
            st.info("No signals generated yet. Click 'Scan Universe Now'.")

    with col_state:
        st.markdown("#### Global Active Trade State (`active_trade_state.json`)")
        if ACTIVE_TRADE_PATH.exists():
            try:
                state_content = json.loads(ACTIVE_TRADE_PATH.read_text())
                status = state_content.get("status", "CLOSED")
                badge_color = "#00c087" if status == "OPEN" else "#6c757d"
                st.markdown(
                    f"<div style='background-color:#1a1e29; padding:15px; border-radius:8px; border-left:4px solid {badge_color};'>"
                    f"<h4>Global Trade Status: <span style='color:{badge_color};'>{status}</span></h4>"
                    f"Active Symbol: <b>{state_content.get('symbol', 'None')}</b><br>"
                    f"Entry Price: <b>{state_content.get('entry_price', 'N/A')}</b><br>"
                    f"TP Price: <b>{state_content.get('take_profit', 'N/A')}</b><br>"
                    f"SL Price: <b>{state_content.get('stop_loss', 'N/A')}</b>"
                    f"</div>",
                    unsafe_allow_html=True
                )
                with st.expander("Raw State JSON"):
                    st.json(state_content)
            except Exception as e:
                st.error(f"Error reading trade state: {e}")
        else:
            st.info("No active trade state recorded.")

    # Audit Trail Log Viewer
    st.markdown("#### Platform Audit Trail (`platform_audit.log`)")
    if AUDIT_LOG_PATH.exists():
        log_lines = AUDIT_LOG_PATH.read_text().strip().split("\n")
        recent_lines = log_lines[-30:][::-1]  # Most recent first
        log_text = "\n".join(recent_lines)
        st.markdown(f"<div class='audit-box'><pre>{log_text}</pre></div>", unsafe_allow_html=True)
    else:
        st.info("Audit log is empty.")

# -------------------------------------------------------------
# TAB 4: SINGLE SYMBOL DRILLDOWN & FEATURE IMPORTANCE
# -------------------------------------------------------------
with tab_single:
    st.subheader(f"Detailed Analysis: {selected_symbol}")
    feat_imp = trainer.get_feature_importance()
    if feat_imp:
        top_feats = list(feat_imp.items())[:25]
        feat_df = pd.DataFrame(top_feats, columns=["Feature", "Importance"]).sort_values("Importance", ascending=True)

        fig_feat = px.bar(
            feat_df,
            x="Importance",
            y="Feature",
            orientation="h",
            title="LightGBM Top 25 Feature Importances",
            color="Importance",
            color_continuous_scale="Viridis"
        )
        fig_feat.update_layout(template="plotly_dark", height=500)
        st.plotly_chart(fig_feat, use_container_width=True)
    else:
        st.info("Model not trained yet.")
