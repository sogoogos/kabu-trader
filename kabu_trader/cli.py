"""CLI interface for Kabu Trader."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .data_fetcher import DataFetcher
from .strategy import SwingCompositeStrategy
from .backtester import Backtester
from .monitor import Monitor
from .llm_sentiment import LLMSentimentAnalyzer


console = Console()

DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "default.json"


def load_config(config_path: Optional[str] = None) -> dict:
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    with open(path) as f:
        return json.load(f)


def cmd_backtest(args):
    """Run backtest on historical data."""
    config = load_config(args.config)
    tickers = args.tickers or config["watchlist"]
    days = args.days or config["backtest"]["lookback_days"]
    names = config.get("watchlist_names", {})

    console.print(Panel(
        f"Running backtest on {len(tickers)} stocks over {days} days",
        title="Backtest",
        border_style="bold blue",
    ))

    fetcher = DataFetcher()
    strategy = SwingCompositeStrategy(config["strategy"]["params"])
    backtester = Backtester(config["backtest"])

    # Load ML model if available
    from .ml_model import MLPredictor
    ml = MLPredictor()
    if ml.load():
        strategy.set_ml_model(ml)
        console.print("[bold green]ML model loaded[/bold green]")

    console.print("[bold]Fetching historical data...[/bold]")
    nikkei_df = fetcher.fetch_nikkei225(days=days)
    strategy.set_nikkei_data(nikkei_df)
    data = fetcher.fetch_multiple(tickers, days=days)

    if not data:
        console.print("[red]No data fetched. Check your tickers.[/red]")
        return

    results = backtester.run_multiple(data, strategy)

    # Summary table
    table = Table(title="Backtest Results", show_header=True, header_style="bold cyan")
    table.add_column("Ticker", style="bold")
    table.add_column("Name", style="dim")
    table.add_column("Return", justify="right")
    table.add_column("Trades", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("Profit Factor", justify="right")
    table.add_column("Max DD", justify="right")
    table.add_column("Sharpe", justify="right")

    total_return = 0
    for ticker, result in results.items():
        s = result.summary()
        name = names.get(ticker, "")
        ret = result.total_return_pct
        total_return += ret

        ret_str = f"[green]{ret:+.2f}%[/green]" if ret > 0 else f"[red]{ret:+.2f}%[/red]"

        table.add_row(
            ticker, name, ret_str, str(s["total_trades"]),
            s["win_rate"], s["profit_factor"], s["max_drawdown"], s["sharpe_ratio"],
        )

    console.print()
    console.print(table)

    avg_return = total_return / len(results) if results else 0
    console.print(f"\n[bold]Average return: {avg_return:+.2f}%[/bold]")

    # Detailed trade log if requested
    if args.verbose:
        for ticker, result in results.items():
            name = names.get(ticker, ticker)
            console.print(f"\n[bold]Trade log for {name} ({ticker}):[/bold]")
            trade_table = Table(show_header=True, header_style="dim")
            trade_table.add_column("Entry Date")
            trade_table.add_column("Exit Date")
            trade_table.add_column("Entry Price", justify="right")
            trade_table.add_column("Exit Price", justify="right")
            trade_table.add_column("Shares", justify="right")
            trade_table.add_column("P&L", justify="right")
            trade_table.add_column("P&L %", justify="right")
            trade_table.add_column("Reason")

            for trade in result.trades:
                pnl = trade.pnl
                pnl_str = f"[green]¥{pnl:+,.0f}[/green]" if pnl > 0 else f"[red]¥{pnl:+,.0f}[/red]"
                pnl_pct = trade.pnl_pct
                pnl_pct_str = f"[green]{pnl_pct:+.2f}%[/green]" if pnl_pct > 0 else f"[red]{pnl_pct:+.2f}%[/red]"

                trade_table.add_row(
                    str(trade.entry_date.date()),
                    str(trade.exit_date.date()) if trade.exit_date else "OPEN",
                    f"¥{trade.entry_price:,.0f}",
                    f"¥{trade.exit_price:,.0f}" if trade.exit_price else "-",
                    str(trade.shares),
                    pnl_str,
                    pnl_pct_str,
                    trade.exit_reason,
                )

            console.print(trade_table)


def cmd_monitor(args):
    """Start real-time monitoring."""
    config = load_config(args.config)
    names = config.get("watchlist_names", {})
    monitor = Monitor(config, names)

    # Load ML model if available
    from .ml_model import MLPredictor
    ml = MLPredictor()
    if ml.load():
        monitor.strategy.set_ml_model(ml)
        console.print("[bold green]ML model loaded[/bold green]")

    # Load LLM sentiment if configured
    sentiment_config = config.get("llm_sentiment", {})
    llm = LLMSentimentAnalyzer(sentiment_config)
    if llm.enabled:
        console.print("[bold]Analyzing news sentiment via Claude...[/bold]")
        sentiment_data = llm.analyze_multiple(
            config["watchlist"], names,
        )
        monitor.strategy.set_sentiment_data(sentiment_data)
        console.print(f"[bold green]Sentiment analyzed for {len(sentiment_data)} stocks[/bold green]")

    if args.once:
        monitor.run_once()
    else:
        monitor.run_continuous()


def cmd_sentiment(args):
    """Analyze news sentiment for stocks using Claude."""
    config = load_config(args.config)
    tickers = args.tickers or config["watchlist"]
    names = config.get("watchlist_names", {})
    sentiment_config = config.get("llm_sentiment", {})

    llm = LLMSentimentAnalyzer(sentiment_config)
    if not llm.enabled:
        console.print("[red]LLM sentiment not configured. Add 'llm_sentiment' section to config "
                      "with 'enabled: true' and your API key.[/red]")
        return

    console.print(Panel(
        f"Analyzing news sentiment for {len(tickers)} stocks via Claude",
        title="News Sentiment",
        border_style="bold magenta",
    ))

    results = llm.analyze_multiple(tickers, names)

    table = Table(title="News Sentiment Analysis", show_header=True, header_style="bold cyan")
    table.add_column("Ticker", style="bold")
    table.add_column("Name", style="dim")
    table.add_column("Score", justify="center")
    table.add_column("Conf", justify="right")
    table.add_column("Reasoning")
    table.add_column("Key Factors")

    for ticker in tickers:
        result = results.get(ticker)
        if not result:
            continue

        name = names.get(ticker, "")
        score = result.get("score", 0)

        if score >= 2:
            score_str = f"[bold green]{score:+d}[/bold green]"
        elif score <= -2:
            score_str = f"[bold red]{score:+d}[/bold red]"
        elif score != 0:
            score_str = f"[yellow]{score:+d}[/yellow]"
        else:
            score_str = f"[dim]{score:+d}[/dim]"

        confidence = result.get("confidence", 0)
        reasoning = result.get("reasoning", "")[:60]
        factors = ", ".join(result.get("key_factors", [])[:2])

        table.add_row(
            ticker, name, score_str,
            f"{confidence:.0%}", reasoning, factors,
        )

    console.print()
    console.print(table)


def cmd_train(args):
    """Train ML model on historical data."""
    from .ml_model import train_final_model, walk_forward_evaluate, MLPredictor

    config = load_config(args.config)
    tickers = args.tickers or config["watchlist"]
    days = args.days or 730
    ml_config = config.get("ml", {})

    console.print(Panel(
        f"Training ML model on {len(tickers)} stocks over {days} days",
        title="ML Training",
        border_style="bold magenta",
    ))

    fetcher = DataFetcher()
    params = config["strategy"]["params"]

    console.print("[bold]Fetching historical data...[/bold]")
    nikkei_df = fetcher.fetch_nikkei225(days=days)
    data = fetcher.fetch_multiple(tickers, days=days)

    if not data:
        console.print("[red]No data fetched.[/red]")
        return

    console.print(f"[bold]Fetched {len(data)} stocks[/bold]")

    # Walk-forward evaluation first
    console.print("\n[bold]Running walk-forward evaluation (5 folds)...[/bold]")
    eval_result = walk_forward_evaluate(
        data, params, nikkei_df,
        n_splits=5,
        forward_days=ml_config.get("forward_days", 5),
        threshold=ml_config.get("threshold", 0.03),
        ml_params=ml_config.get("model_params"),
    )

    if "error" in eval_result:
        console.print(f"[red]{eval_result['error']}[/red]")
        return

    # Display evaluation results
    eval_table = Table(title="Walk-Forward Evaluation", show_header=True, header_style="bold cyan")
    eval_table.add_column("Fold", justify="center")
    eval_table.add_column("Train", justify="right")
    eval_table.add_column("Test", justify="right")
    eval_table.add_column("Accuracy", justify="right")
    eval_table.add_column("Precision", justify="right")
    eval_table.add_column("Recall", justify="right")
    eval_table.add_column("F1", justify="right")
    eval_table.add_column("AUC-ROC", justify="right")

    for fold in eval_result["folds"]:
        eval_table.add_row(
            str(fold["fold"]),
            str(fold["train_size"]),
            str(fold["test_size"]),
            f"{fold['accuracy']:.3f}",
            f"{fold['precision']:.3f}",
            f"{fold['recall']:.3f}",
            f"{fold['f1']:.3f}",
            f"{fold['auc_roc']:.3f}",
        )

    avg = eval_result["average"]
    eval_table.add_row(
        "[bold]AVG[/bold]", "", "",
        f"[bold]{avg['accuracy']:.3f}[/bold]",
        f"[bold]{avg['precision']:.3f}[/bold]",
        f"[bold]{avg['recall']:.3f}[/bold]",
        f"[bold]{avg['f1']:.3f}[/bold]",
        f"[bold]{avg['auc_roc']:.3f}[/bold]",
    )

    console.print()
    console.print(eval_table)
    console.print(f"\n[dim]Total samples: {eval_result['total_samples']} | "
                  f"Positive rate: {eval_result['positive_rate']}[/dim]")

    # Train final model on all data
    console.print("\n[bold]Training final model on all data...[/bold]")
    model, metrics = train_final_model(
        data, params, nikkei_df,
        forward_days=ml_config.get("forward_days", 5),
        threshold=ml_config.get("threshold", 0.03),
        ml_params=ml_config.get("model_params"),
    )

    console.print(f"[green]Model saved to models/default.pkl[/green]")
    console.print(f"Training accuracy: {metrics['accuracy']:.3f} | "
                  f"AUC-ROC: {metrics['auc_roc']:.3f}")

    # Feature importance
    console.print("\n[bold]Top 15 Most Important Features:[/bold]")
    imp_table = Table(show_header=True, header_style="dim")
    imp_table.add_column("Feature")
    imp_table.add_column("Importance", justify="right")

    for name, importance in model.get_feature_importance(15):
        bar = "█" * int(importance * 200)
        imp_table.add_row(name, f"{importance:.4f} {bar}")

    console.print(imp_table)


def cmd_scan(args):
    """Scan watchlist for current trading signals."""
    config = load_config(args.config)
    tickers = args.tickers or config["watchlist"]
    names = config.get("watchlist_names", {})

    console.print(Panel(
        f"Scanning {len(tickers)} stocks for signals",
        title="Signal Scanner",
        border_style="bold blue",
    ))

    fetcher = DataFetcher()
    strategy = SwingCompositeStrategy(config["strategy"]["params"])

    # Load ML model if available
    from .ml_model import MLPredictor
    ml = MLPredictor()
    if ml.load():
        strategy.set_ml_model(ml)
        console.print("[bold green]ML model loaded[/bold green]")

    # Load LLM sentiment if configured
    sentiment_config = config.get("llm_sentiment", {})
    llm = LLMSentimentAnalyzer(sentiment_config)
    if llm.enabled:
        console.print("[bold]Analyzing news sentiment via Claude...[/bold]")
        sentiment_data = llm.analyze_multiple(tickers, names)
        strategy.set_sentiment_data(sentiment_data)
        console.print(f"[bold green]Sentiment analyzed for {len(sentiment_data)} stocks[/bold green]")

    console.print("[bold]Fetching data...[/bold]")
    nikkei_df = fetcher.fetch_nikkei225(days=60)
    strategy.set_nikkei_data(nikkei_df)
    data = fetcher.fetch_multiple(tickers, days=60)

    table = Table(title="Current Signals", show_header=True, header_style="bold cyan")
    table.add_column("Ticker", style="bold")
    table.add_column("Name", style="dim")
    table.add_column("Price", justify="right")
    table.add_column("Signal", justify="center")
    table.add_column("Score", justify="right")
    table.add_column("Reasons")

    for ticker, df in data.items():
        if len(df) < 30:
            continue
        signal = strategy.get_latest_signal(df, ticker)
        name = names.get(ticker, "")

        sig_name = signal.signal.name
        if "BUY" in sig_name:
            sig_str = f"[bold green]{sig_name}[/bold green]"
        elif "SELL" in sig_name:
            sig_str = f"[bold red]{sig_name}[/bold red]"
        else:
            sig_str = f"[dim]{sig_name}[/dim]"

        reasons_str = "; ".join(signal.reasons[:3]) if signal.reasons else "-"

        table.add_row(
            ticker, name, f"¥{signal.price:,.0f}",
            sig_str, str(signal.score), reasons_str,
        )

    console.print()
    console.print(table)


def main():
    parser = argparse.ArgumentParser(
        description="Kabu Trader - Japanese Stock Swing Trading System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s sentiment                    Analyze news sentiment via LLM
  %(prog)s sentiment -t 7203.T         Analyze specific stocks
  %(prog)s train                       Train ML model on all watchlist stocks
  %(prog)s train -d 1000               Train with more history
  %(prog)s backtest                    Backtest all watchlist stocks
  %(prog)s backtest -t 7203.T 6758.T   Backtest specific stocks
  %(prog)s backtest -d 730 -v          Backtest 2 years with trade details
  %(prog)s scan                        Scan for current signals
  %(prog)s monitor                     Start real-time monitor
  %(prog)s monitor --once              Run single monitoring cycle
        """,
    )
    parser.add_argument("-c", "--config", help="Path to config file")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Sentiment
    se = subparsers.add_parser("sentiment", help="Analyze news sentiment via LLM")
    se.add_argument("-t", "--tickers", nargs="+", help="Tickers to analyze")
    se.set_defaults(func=cmd_sentiment)

    # Train
    tr = subparsers.add_parser("train", help="Train ML model on historical data")
    tr.add_argument("-t", "--tickers", nargs="+", help="Tickers to train on")
    tr.add_argument("-d", "--days", type=int, help="Days of history (default: 730)")
    tr.set_defaults(func=cmd_train)

    # Backtest
    bt = subparsers.add_parser("backtest", help="Run backtest on historical data")
    bt.add_argument("-t", "--tickers", nargs="+", help="Tickers to backtest")
    bt.add_argument("-d", "--days", type=int, help="Days of history")
    bt.add_argument("-v", "--verbose", action="store_true", help="Show trade details")
    bt.set_defaults(func=cmd_backtest)

    # Scan — already defined above, remove duplicate
    sc = subparsers.add_parser("scan", help="Scan for current trading signals")
    sc.add_argument("-t", "--tickers", nargs="+", help="Tickers to scan")
    sc.set_defaults(func=cmd_scan)

    # Monitor
    mo = subparsers.add_parser("monitor", help="Real-time price monitor")
    mo.add_argument("--once", action="store_true", help="Run single cycle")
    mo.set_defaults(func=cmd_monitor)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
