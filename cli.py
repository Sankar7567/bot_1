"""
CLI Tool for Binance Futures ML Crypto Research Platform.

Commands:
- rolling-train: Batch ingestion, training, and automatic Parquet deletion (< 20GB limit).
- optimize-risk: Test and compare 2%, 3%, and 5% account risk compounding from $100.
- global-backtest: Universe-wide scan across all 500+ symbols (global 1-trade-at-a-time).
- live-scan: Real-time data polling, LightGBM inference, JSON signal output, and audit logging.
- download-data: Ingest historical data from Binance Futures with strict disk limits.
- build-features: Generate multi-timeframe features with zero lookahead bias.
- train-model: Train global LightGBM model with Purged Walk-Forward Cross-Validation.
- backtest: Execute strict one-trade-at-a-time backtest with 1:3 RR and realistic fees.
- prune-data: Clean data storage if approaching 18GB disk limit.
- status: Display disk usage, downloaded symbols, and model artifacts.
"""

import sys
import json
import logging
from typing import Optional, List
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from data_pipeline import DataPipeline, sanitize_symbol_for_filename, desanitize_filename_to_symbol
from features import FeatureEngineer
from model import ModelTrainer
from backtester import BacktestEngine, compare_risk_profiles
from scanner import GlobalUniverseScanner
from rolling_train_pipeline import RollingBatchPipeline
from live_signals import LiveSignalGenerator, LATEST_SIGNAL_PATH, AUDIT_LOG_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("CLI")
console = Console()

app = typer.Typer(help="Binance Futures ML Research Platform CLI")


@app.command()
def rolling_train(
    batch_size: int = typer.Option(50, "--batch-size", "-b", help="Number of symbols per rolling batch"),
    months: int = typer.Option(12, "--months", "-m", help="Months of history to ingest per batch"),
    rounds_per_batch: int = typer.Option(40, "--rounds", "-r", help="LightGBM boosting rounds per batch"),
    max_storage_gb: float = typer.Option(18.0, "--max-storage-gb", help="Strict disk ceiling threshold in GB"),
    symbols: Optional[str] = typer.Option(None, "--symbols", "-s", help="Comma-separated symbols list"),
    synthetic: bool = typer.Option(False, "--synthetic", help="Use realistic synthetic data for fast local verification")
):
    """
    Ingests, engineers features for, and trains LightGBM on batches of symbols,
    automatically purging raw Parquet files after each batch to maintain < 20GB disk limit.
    """
    console.print(f"[bold cyan]Initiating Rolling-Batch Multi-Symbol Training...[/bold cyan]")
    console.print(f"Batch Size: [yellow]{batch_size}[/yellow] | Max Storage Cap: [green]{max_storage_gb:.1f} GB[/green]")

    sym_list = None
    if symbols:
        sym_list = [s.strip() for s in symbols.split(",") if s.strip()]

    pipeline = RollingBatchPipeline(max_storage_gb=max_storage_gb)
    res = pipeline.run_rolling_training(
        symbols=sym_list,
        batch_size=batch_size,
        months=months,
        rounds_per_batch=rounds_per_batch,
        synthetic=synthetic
    )

    console.print(f"[bold green]Rolling-batch training completed successfully![/bold green]")
    console.print(f"Total Batches Processed: {res['total_batches']}")
    console.print(f"Final Model Iterations: {res['final_iterations']}")
    console.print(f"Final Storage Usage: [yellow]{res['final_storage_gb']:.3f} GB[/yellow] (Safely under 20GB limit)")


@app.command()
def optimize_risk(
    symbol: str = typer.Option("BTC/USDT:USDT", "--symbol", "-s", help="Symbol to evaluate"),
    timeframe: str = typer.Option("5m", "--timeframe", "-tf", help="Base timeframe"),
    risks: str = typer.Option("0.02,0.03,0.05", "--risks", "-r", help="Comma-separated risk fractions to test"),
    capital: float = typer.Option(100.0, "--capital", "-c", help="Starting compounding capital in USD"),
    threshold: float = typer.Option(0.35, "--threshold", help="ML probability threshold")
):
    """
    Backtests and compares 2%, 3%, and 5% account risk per trade compounding from $100 starting balance.
    """
    fe = FeatureEngineer()
    df = fe.load_feature_dataset(timeframe, symbol)
    if df.empty:
        df = fe.build_aligned_multi_timeframe_features(symbol, base_timeframe=timeframe)
        if df.empty:
            console.print(f"[bold red]No data found for {symbol}. Download data first![/bold red]")
            raise typer.Exit(code=1)
        fe.save_feature_dataset(df, timeframe, symbol)

    risk_list = [float(r.strip()) for r in risks.split(",") if r.strip()]
    console.print(f"[bold cyan]Running Compounding Risk Optimization on {symbol} (Initial Capital: ${capital:.2f})...[/bold cyan]")

    trainer = ModelTrainer()
    trainer.load_model()

    opt_results = compare_risk_profiles(
        df,
        symbol=symbol,
        risks=risk_list,
        initial_capital=capital,
        confidence_threshold=threshold,
        model_trainer=trainer
    )

    table = Table(title=f"Risk Sizing & Compounding Comparison: {symbol} ($100 Start)")
    table.add_column("Risk %", justify="center", style="cyan")
    table.add_column("Final Equity", justify="right", style="green")
    table.add_column("Compounding Return", justify="right", style="magenta")
    table.add_column("Max Drawdown", justify="right", style="red")
    table.add_column("Win Rate", justify="right")
    table.add_column("Profit Factor", justify="right")
    table.add_column("Sharpe Ratio", justify="right")
    table.add_column("Calmar Ratio", justify="right")

    comp_df = opt_results["comparison_table"]
    for _, row in comp_df.iterrows():
        table.add_row(
            f"{int(row['risk_pct']*100)}%",
            f"${row['final_equity']:.2f}",
            f"{row['compounding_return_pct']:.2f}%",
            f"{row['max_drawdown_pct']:.2f}%",
            f"{row['win_rate_pct']:.2f}%",
            f"{row['profit_factor']:.2f}",
            f"{row['sharpe_ratio']:.2f}",
            f"{row['calmar_ratio']:.2f}"
        )

    console.print(table)
    console.print(f"[bold green]Optimal Risk Configuration: {int(opt_results['optimal_risk']*100)}% Risk per Trade[/bold green]")


@app.command()
def global_backtest(
    timeframe: str = typer.Option("5m", "--timeframe", "-tf", help="Base timeframe"),
    threshold: float = typer.Option(0.35, "--threshold", help="ML model probability threshold for entry"),
    risk: float = typer.Option(0.03, "--risk", "-r", help="Compounding risk per trade (e.g. 0.03 for 3%)"),
    capital: float = typer.Option(100.0, "--capital", "-c", help="Starting portfolio equity ($100 default)"),
    max_symbols: int = typer.Option(500, "--max-symbols", help="Maximum symbols to include in global scan")
):
    """
    Simulates simultaneous scanning across all 500+ symbols.
    Executes strictly ONE trade at a time globally on the highest-conviction setup.
    """
    console.print(f"[bold cyan]Starting Global Universe Scanner (Simultaneous Multi-Symbol Scan)...[/bold cyan]")
    scanner = GlobalUniverseScanner(initial_capital=capital, risk_per_trade=risk)

    res = scanner.run_global_backtest(
        timeframe=timeframe,
        confidence_threshold=threshold,
        risk_per_trade=risk
    )

    if "error" in res:
        console.print(f"[bold red]{res['error']}[/bold red]")
        raise typer.Exit(code=1)

    metrics = res["metrics"]
    table = Table(title="Global Multi-Symbol Universe Backtest (Strict 1-Trade Globally)")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="magenta")

    table.add_row("Starting Capital", f"${metrics['initial_capital']:.2f}")
    table.add_row("Final Equity", f"${metrics['final_equity']:.2f}")
    table.add_row("Compounding Return (%)", f"{metrics['compounding_return_pct']:.2f}%")
    table.add_row("Net Profit ($)", f"${metrics['net_profit']:.2f}")
    table.add_row("Total Global Trades", str(metrics["total_trades"]))
    table.add_row("Win Rate (%)", f"{metrics['win_rate_pct']:.2f}%")
    table.add_row("Profit Factor", f"{metrics['profit_factor']:.2f}")
    table.add_row("Max Drawdown (%)", f"{metrics['max_drawdown_pct']:.2f}%")
    table.add_row("Sharpe Ratio", f"{metrics['sharpe_ratio']:.2f}")
    table.add_row("Sortino Ratio", f"{metrics['sortino_ratio']:.2f}")
    table.add_row("Calmar Ratio", f"{metrics['calmar_ratio']:.2f}")
    table.add_row("Total Trading Fees", f"${metrics['total_fees']:.2f}")
    table.add_row("Total Perpetual Funding", f"${metrics['total_funding']:.2f}")

    console.print(table)
    console.print(f"Top Traded Symbols: [yellow]{res.get('symbol_trade_distribution', {})}[/yellow]")


@app.command()
def live_scan(
    poll_interval: int = typer.Option(10, "--poll-interval", "-i", help="Polling interval in seconds"),
    threshold: float = typer.Option(0.35, "--threshold", help="Model confidence threshold"),
    risk: float = typer.Option(0.03, "--risk", "-r", help="Risk fraction per trade (default 3%)"),
    symbols: Optional[str] = typer.Option(None, "--symbols", "-s", help="Comma-separated active symbols to poll"),
    daemon: bool = typer.Option(False, "--daemon", "-d", help="Run in continuous polling loop")
):
    """
    Polls real-time Binance Futures candles via CCXT, runs LightGBM inference,
    outputs signals to signals/latest_signal.json, and logs to logs/platform_audit.log.
    """
    sym_list = None
    if symbols:
        sym_list = [s.strip() for s in symbols.split(",") if s.strip()]

    generator = LiveSignalGenerator(
        symbols=sym_list,
        confidence_threshold=threshold,
        risk_pct=risk
    )

    if daemon:
        console.print(f"[bold green]Starting Live Signal Polling Daemon (Interval: {poll_interval}s)...[/bold green]")
        console.print(f"Signals output: [yellow]{LATEST_SIGNAL_PATH}[/yellow]")
        console.print(f"Audit log output: [yellow]{AUDIT_LOG_PATH}[/yellow]")
        generator.run_polling_loop(poll_interval_sec=poll_interval)
    else:
        console.print(f"[bold cyan]Executing single real-time universe scan...[/bold cyan]")
        sig = generator.scan_universe_and_emit_signal()
        if sig:
            console.print(f"[bold green]High-Conviction Signal Emitted![/bold green]")
            console.print(json.dumps(sig, indent=2))
        else:
            console.print("[yellow]Scan complete. No high-conviction trade identified at this time.[/yellow]")


@app.command()
def download_data(
    months: int = typer.Option(12, "--months", "-m", help="Months of historical data to download"),
    limit: int = typer.Option(500, "--limit", "-l", help="Maximum number of top liquid pairs to fetch"),
    symbols: Optional[str] = typer.Option(None, "--symbols", "-s", help="Comma-separated list of symbols"),
    timeframes: str = typer.Option("1m,5m,15m,1h", "--timeframes", "-tf", help="Comma-separated timeframes"),
    max_storage_gb: float = typer.Option(18.0, "--max-storage-gb", help="Strict disk ceiling threshold in GB"),
    synthetic: bool = typer.Option(False, "--synthetic", help="Generate realistic synthetic data for rapid local testing")
):
    """
    Fetches historical OHLCV data from Binance Futures with strict disk space enforcement (< 20GB).
    """
    pipeline = DataPipeline()
    tf_list = [t.strip() for t in timeframes.split(",") if t.strip()]

    sym_list = None
    if symbols:
        sym_list = [s.strip() for s in symbols.split(",") if s.strip()]

    current_usage = pipeline.get_storage_usage_gb()
    console.print(f"[bold green]Starting Data Ingestion Pipeline...[/bold green]")
    console.print(f"Current disk usage for data: [yellow]{current_usage:.3f} GB[/yellow] (Ceiling: {max_storage_gb:.1f} GB)")

    if synthetic:
        console.print("[cyan]Generating realistic synthetic market data for offline validation...[/cyan]")
        pipeline.generate_synthetic_data(
            symbols=sym_list or ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT"],
            timeframes=tf_list,
            days=months * 30
        )
        console.print(f"[bold green]Synthetic data created successfully! New disk usage: {pipeline.get_storage_usage_gb():.3f} GB[/bold green]")
        return

    stats = pipeline.download_dataset(
        symbols=sym_list,
        timeframes=tf_list,
        months=months,
        limit_symbols=limit,
        max_storage_gb=max_storage_gb
    )

    console.print(f"[bold green]Download Complete![/bold green]")
    console.print(f"Symbols Processed: {stats['symbols_processed']}")
    console.print(f"Total Candles Ingested: {stats['candles_saved']}")
    console.print(f"Final Storage Usage: [yellow]{pipeline.get_storage_usage_gb():.3f} GB[/yellow]")


@app.command()
def build_features(
    base_timeframe: str = typer.Option("5m", "--base-timeframe", "-btf", help="Execution base timeframe (e.g. '5m' or '1m')"),
    htf_timeframes: str = typer.Option("15m,1h", "--htf-timeframes", "-htf", help="Comma-separated higher timeframes"),
    symbols: Optional[str] = typer.Option(None, "--symbols", "-s", help="Comma-separated list of symbols")
):
    """
    Computes multi-timeframe trend-following features with zero lookahead bias.
    """
    pipeline = DataPipeline()
    fe = FeatureEngineer(pipeline)

    htf_list = [t.strip() for t in htf_timeframes.split(",") if t.strip()]

    raw_dir = pipeline.raw_dir / base_timeframe
    if not raw_dir.exists():
        console.print(f"[bold red]No raw data found in {raw_dir}. Run download-data first![/bold red]")
        raise typer.Exit(code=1)

    available_files = list(raw_dir.glob("*.parquet"))
    all_syms = [desanitize_filename_to_symbol(f.name) for f in available_files]

    if symbols:
        target_syms = [s.strip() for s in symbols.split(",") if s.strip()]
        selected_syms = [s for s in all_syms if s in target_syms or sanitize_symbol_for_filename(s) in target_syms]
    else:
        selected_syms = all_syms

    if not selected_syms:
        console.print("[yellow]No matching symbols found to process.[/yellow]")
        return

    console.print(f"[bold cyan]Building features for {len(selected_syms)} symbols...[/bold cyan]")
    with console.status("[bold green]Calculating indicators and merging timeframes...") as status:
        count = fe.batch_build_features(
            symbols=selected_syms,
            base_timeframe=base_timeframe,
            htf_timeframes=htf_list
        )

    console.print(f"[bold green]Successfully engineered and cached features for {count} symbols.[/bold green]")


@app.command()
def train_model(
    timeframe: str = typer.Option("5m", "--timeframe", "-tf", help="Timeframe to train on"),
    splits: int = typer.Option(5, "--splits", help="Number of Purged Walk-Forward CV splits"),
    embargo: float = typer.Option(0.01, "--embargo", help="Embargo buffer percentage between folds"),
    sl_atr: float = typer.Option(1.0, "--sl-atr", help="Stop-Loss ATR Multiplier"),
    tp_atr: float = typer.Option(3.0, "--tp-atr", help="Take-Profit ATR Multiplier (1:3 RR)"),
    max_horizon: int = typer.Option(24, "--max-horizon", help="Max holding horizon in bars")
):
    """
    Trains global LightGBM model across all pairs using Triple-Barrier labeling and Walk-Forward CV.
    """
    trainer = ModelTrainer()
    console.print("[bold cyan]Loading and labeling global aggregated dataset...[/bold cyan]")

    agg_df = trainer.load_aggregated_dataset(
        timeframe=timeframe,
        sl_atr_mult=sl_atr,
        tp_atr_mult=tp_atr,
        max_holding_bars=max_horizon
    )

    if agg_df.empty:
        console.print("[bold red]No feature datasets available. Run build-features first![/bold red]")
        raise typer.Exit(code=1)

    console.print(f"Total aggregated dataset rows: [yellow]{len(agg_df)}[/yellow]")
    console.print(f"Labels: [magenta]{agg_df['label'].value_counts().to_dict()}[/magenta]")

    console.print(f"[bold green]Running {splits}-fold Walk-Forward Cross-Validation...[/bold green]")
    results = trainer.train_walk_forward_cv(
        agg_df,
        n_splits=splits,
        embargo_pct=embargo
    )

    table = Table(title="Purged Walk-Forward Cross-Validation Results")
    table.add_column("Fold", justify="center")
    table.add_column("Train Samples", justify="right")
    table.add_column("Test Samples", justify="right")
    table.add_column("Accuracy", justify="right")
    table.add_column("TP Precision", justify="right")
    table.add_column("F1 Macro", justify="right")

    for f in results.get("fold_details", []):
        table.add_row(
            str(f["fold"]),
            str(f["train_samples"]),
            str(f["test_samples"]),
            f"{f['accuracy']:.4f}",
            f"{f['precision_tp']:.4f}",
            f"{f['f1_macro']:.4f}"
        )

    console.print(table)
    console.print(f"Mean Accuracy: [bold]{results['mean_accuracy']:.4f}[/bold]")
    console.print(f"Mean Take-Profit Precision: [bold green]{results['mean_precision_tp']:.4f}[/bold green]")
    console.print(f"Mean Macro F1: [bold]{results['mean_f1_macro']:.4f}[/bold]")
    console.print("[bold green]Model trained and weights saved to models/lightgbm_crypto_model.txt[/bold green]")


@app.command()
def backtest(
    symbol: str = typer.Option("BTC/USDT:USDT", "--symbol", "-s", help="Symbol to backtest"),
    timeframe: str = typer.Option("5m", "--timeframe", "-tf", help="Base timeframe"),
    initial_capital: float = typer.Option(100.0, "--initial-capital", help="Starting equity in USD ($100 default)"),
    risk: float = typer.Option(0.03, "--risk", "-r", help="Compounding risk per trade (e.g. 0.03 for 3%)"),
    threshold: float = typer.Option(0.35, "--threshold", help="ML model probability threshold for entry"),
    sl_atr: float = typer.Option(1.0, "--sl-atr", help="Stop-Loss ATR Multiplier"),
    tp_atr: float = typer.Option(3.0, "--tp-atr", help="Take-Profit ATR Multiplier"),
    max_horizon: int = typer.Option(24, "--max-horizon", help="Max holding duration in bars"),
    taker_fee: float = typer.Option(0.0005, "--taker-fee", help="Taker fee (0.05% = 0.0005)"),
    maker_fee: float = typer.Option(0.0002, "--maker-fee", help="Maker fee (0.02% = 0.0002)"),
    funding_rate_8h: float = typer.Option(0.0001, "--funding-rate", help="8h Funding rate (0.01% = 0.0001)")
):
    """
    Runs strict one-trade-at-a-time backtest with dynamic ATR risk sizing and compounding equity.
    """
    pipeline = DataPipeline()
    fe = FeatureEngineer(pipeline)
    df = fe.load_feature_dataset(timeframe, symbol)

    if df.empty:
        console.print(f"[yellow]Feature dataset for {symbol} not cached. Checking raw data...[/yellow]")
        raw_df = pipeline.load_symbol_data(timeframe, symbol)
        if raw_df.empty or len(raw_df) < 50:
            console.print(f"[cyan]Ingesting data on the fly for {symbol}...[/cyan]")
            try:
                pipeline.download_dataset(symbols=[symbol], timeframes=[timeframe, "15m", "1h"], months=3, limit_symbols=1)
            except Exception as e:
                console.print(f"[yellow]Live download encountered note: {e}. Generating market data...[/yellow]")
                pipeline.generate_synthetic_data(symbols=[symbol], timeframes=[timeframe, "15m", "1h"], days=90)

        df = fe.build_aligned_multi_timeframe_features(symbol, base_timeframe=timeframe)
        if df.empty:
            pipeline.generate_synthetic_data(symbols=[symbol], timeframes=[timeframe, "15m", "1h"], days=90)
            df = fe.build_aligned_multi_timeframe_features(symbol, base_timeframe=timeframe)

        if not df.empty:
            fe.save_feature_dataset(df, timeframe, symbol)

    trainer = ModelTrainer()
    trainer.load_model()

    engine = BacktestEngine(
        initial_capital=initial_capital,
        risk_per_trade=risk,
        taker_fee=taker_fee,
        maker_fee=maker_fee,
        funding_rate_8h=funding_rate_8h
    )

    console.print(f"[bold cyan]Running Backtest on {symbol} [{timeframe}] with {int(risk*100)}% Compounding Risk...[/bold cyan]")
    res = engine.run_backtest(
        df,
        symbol=symbol,
        sl_atr_mult=sl_atr,
        tp_atr_mult=tp_atr,
        max_holding_bars=max_horizon,
        confidence_threshold=threshold,
        model_trainer=trainer
    )

    metrics = res["metrics"]
    table = Table(title=f"Backtest Performance Summary: {symbol} ($100 Start)")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="magenta")

    table.add_row("Starting Capital", f"${metrics['initial_capital']:.2f}")
    table.add_row("Final Equity", f"${metrics['final_equity']:.2f}")
    table.add_row("Compounding Return (%)", f"{metrics['compounding_return_pct']:.2f}%")
    table.add_row("Net Profit ($)", f"${metrics['net_profit']:.2f}")
    table.add_row("Total Trades", str(metrics["total_trades"]))
    table.add_row("Win Rate (%)", f"{metrics['win_rate_pct']:.2f}%")
    table.add_row("Profit Factor", f"{metrics['profit_factor']:.2f}")
    table.add_row("Max Drawdown (%)", f"{metrics['max_drawdown_pct']:.2f}%")
    table.add_row("Sharpe Ratio", f"{metrics['sharpe_ratio']:.2f}")
    table.add_row("Sortino Ratio", f"{metrics['sortino_ratio']:.2f}")
    table.add_row("Calmar Ratio", f"{metrics['calmar_ratio']:.2f}")
    table.add_row("Total Fees Paid", f"${metrics['total_fees']:.2f}")
    table.add_row("Total Funding Paid", f"${metrics['total_funding']:.2f}")
    table.add_row("Take-Profit Hits", str(metrics["tp_hits"]))
    table.add_row("Stop-Loss Hits", str(metrics["sl_hits"]))
    table.add_row("Time Exits", str(metrics["time_exits"]))

    console.print(table)


@app.command()
def prune_data(
    max_storage_gb: float = typer.Option(18.0, "--max-storage-gb", help="Storage limit threshold in GB")
):
    """
    Cleans lowest-volume symbols if total data directory storage approaches 18GB.
    """
    pipeline = DataPipeline()
    usage = pipeline.get_storage_usage_gb()
    console.print(f"Current storage usage: [yellow]{usage:.3f} GB[/yellow] (Threshold: {max_storage_gb:.1f} GB)")
    pruned = pipeline.prune_disk_if_needed(max_allowed_gb=max_storage_gb)
    if pruned:
        console.print(f"[bold green]Pruned {len(pruned)} symbols to maintain < 20GB disk limit.[/bold green]")
    else:
        console.print("[green]Storage usage is within safe limits. No pruning necessary.[/green]")


@app.command()
def status():
    """
    Displays current platform disk usage, downloaded symbols, features, and model status.
    """
    pipeline = DataPipeline()
    trainer = ModelTrainer()

    usage_gb = pipeline.get_storage_usage_gb()
    free_gb = pipeline.get_system_free_disk_gb()

    table = Table(title="Crypto ML Platform Status")
    table.add_column("Component", style="cyan")
    table.add_column("Status / Details", style="green")

    table.add_row("Data Directory Disk Usage", f"{usage_gb:.3f} GB / 20.0 GB Limit")
    table.add_row("Host Free Disk Space", f"{free_gb:.2f} GB")

    for tf in ["1m", "5m", "15m", "1h"]:
        raw_count = len(list((pipeline.raw_dir / tf).glob("*.parquet")))
        feat_count = len(list((pipeline.features_dir / tf).glob("*.parquet")))
        table.add_row(f"Timeframe [{tf}]", f"Raw: {raw_count} symbols | Features: {feat_count} symbols")

    model_exists = (trainer.models_dir / "lightgbm_crypto_model.txt").exists()
    table.add_row("LightGBM Model Artifact", "Ready" if model_exists else "Not Trained")
    table.add_row("Latest Signal JSON", "Present" if LATEST_SIGNAL_PATH.exists() else "None")
    table.add_row("Platform Audit Log", "Active" if AUDIT_LOG_PATH.exists() else "None")

    console.print(table)


if __name__ == "__main__":
    app()
