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
    import os
    path = Path(config_path) if config_path else Path(
        os.environ.get("KABU_CONFIG", DEFAULT_CONFIG)
    )
    with open(path) as f:
        return json.load(f)


def get_market_settings(config: dict) -> dict:
    """Extract market-level settings (benchmark, currency, lot size, etc.)."""
    market = config.get("market", {})
    return {
        "currency_symbol": market.get("currency_symbol", "¥"),
        "currency_code": market.get("currency_code", "JPY"),
        "benchmark_ticker": market.get("benchmark_ticker", "^N225"),
        "benchmark_name": market.get("benchmark_name", "Nikkei 225"),
        "market_name": market.get("name", "JP"),
        "state_dir": market.get("state_dir"),
    }


def cmd_backtest(args):
    """Run backtest on historical data."""
    config = load_config(args.config)
    market = get_market_settings(config)
    tickers = args.tickers or config["watchlist"]
    days = args.days or config["backtest"]["lookback_days"]
    names = config.get("watchlist_names", {})
    model_name = config.get("ml", {}).get("model_name", "default")

    console.print(Panel(
        f"Running backtest on {len(tickers)} [{market['market_name']}] stocks over {days} days",
        title="Backtest",
        border_style="bold blue",
    ))

    fetcher = DataFetcher(benchmark_ticker=market["benchmark_ticker"])
    strategy = SwingCompositeStrategy(config["strategy"]["params"], market["benchmark_name"])
    backtester = Backtester(config["backtest"], currency_symbol=market["currency_symbol"])

    # Load ML model if available
    from .ml_model import MLPredictor
    ml = MLPredictor()
    if ml.load(model_name):
        strategy.set_ml_model(ml)
        console.print(f"[bold green]ML model '{model_name}' loaded[/bold green]")

    console.print("[bold]Fetching historical data...[/bold]")
    benchmark_df = fetcher.fetch_benchmark(days=days)
    strategy.set_benchmark_data(benchmark_df)
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

            sym = market["currency_symbol"]
            for trade in result.trades:
                pnl = trade.pnl
                pnl_str = f"[green]{sym}{pnl:+,.2f}[/green]" if pnl > 0 else f"[red]{sym}{pnl:+,.2f}[/red]"
                pnl_pct = trade.pnl_pct
                pnl_pct_str = f"[green]{pnl_pct:+.2f}%[/green]" if pnl_pct > 0 else f"[red]{pnl_pct:+.2f}%[/red]"

                trade_table.add_row(
                    str(trade.entry_date.date()),
                    str(trade.exit_date.date()) if trade.exit_date else "OPEN",
                    f"{sym}{trade.entry_price:,.2f}",
                    f"{sym}{trade.exit_price:,.2f}" if trade.exit_price else "-",
                    str(trade.shares),
                    pnl_str,
                    pnl_pct_str,
                    trade.exit_reason,
                )

            console.print(trade_table)


def cmd_monitor(args):
    """Start real-time monitoring."""
    config = load_config(args.config)
    market = get_market_settings(config)
    names = config.get("watchlist_names", {})
    aliases = config.get("watchlist_aliases", {})
    model_name = config.get("ml", {}).get("model_name", "default")
    monitor = Monitor(config, names, aliases)

    # Load ML model if available
    from .ml_model import MLPredictor
    ml = MLPredictor()
    if ml.load(model_name):
        monitor.strategy.set_ml_model(ml)
        console.print(f"[bold green]ML model '{model_name}' loaded[/bold green]")

    # Enable paper trading
    if args.paper:
        from .paper_trader import PaperTrader
        state_dir = Path(market["state_dir"]) if market["state_dir"] else None

        # Optional live broker. Off by default — must be explicitly enabled in config.
        live_broker = None
        broker_cfg = config.get("broker", {})
        if broker_cfg.get("enabled", False):
            broker_type = broker_cfg.get("type", "ibkr").lower()
            if broker_type == "ibkr":
                from .brokers.ibkr import IBKRBroker
                live_broker = IBKRBroker(
                    host=broker_cfg.get("host", "127.0.0.1"),
                    port=broker_cfg.get("port", 4002),
                    client_id=broker_cfg.get("client_id", 1),
                    paper=broker_cfg.get("paper", True),
                    readonly=broker_cfg.get("readonly", False),
                )
                mode = "PAPER" if broker_cfg.get("paper", True) else "LIVE"
                console.print(
                    f"[bold red]IBKR live broker ENABLED ({mode}) — "
                    f"orders will be submitted to {broker_cfg.get('host','127.0.0.1')}:"
                    f"{broker_cfg.get('port',4002)}[/bold red]"
                )
            else:
                console.print(f"[red]Unknown broker.type: {broker_type}[/red]")

        monitor.paper_trader = PaperTrader(
            config["backtest"], state_dir=state_dir, live_broker=live_broker,
        )
        monitor.line.paper_mode = True if live_broker is None else False
        summary = monitor.paper_trader.get_summary()
        sym = market["currency_symbol"]
        is_live = live_broker is not None
        mode_label = "Live" if is_live else "Paper"
        console.print(Panel(
            f"{mode_label} trading enabled [{market['market_name']}]\n"
            f"Capital: {sym}{summary['initial_capital']:,.2f} | "
            f"Current: {sym}{summary['total_value']:,.2f} ({summary['total_return_pct']:+.2f}%)\n"
            f"Open positions: {summary['open_positions']} | "
            f"Closed trades: {summary['total_closed_trades']}",
            title=f"{mode_label} Trading",
            border_style="bold red" if is_live else "bold cyan",
        ))

    if args.once:
        monitor.run_once()
    else:
        monitor.run_continuous()


def _name_lookup(config: dict):
    """Return a function that maps a ticker to a company name.

    Handles both our local convention ("7203.T") and IBKR's bare-symbol form
    ("7203") that comes from execution records.
    """
    names = config.get("watchlist_names", {})

    def fn(ticker: str) -> str:
        if ticker in names:
            return names[ticker]
        # IBKR fills give bare symbols ("7203"); try with .T suffix
        if f"{ticker}.T" in names:
            return names[f"{ticker}.T"]
        return ""
    return fn


def cmd_broker(args):
    """Show the live broker account state (positions, orders, fills, cash)."""
    config = load_config(args.config)
    market = get_market_settings(config)
    broker_cfg = config.get("broker", {})
    if not broker_cfg.get("enabled"):
        console.print("[yellow]broker not enabled in config[/yellow]")
        return

    from .brokers.ibkr import IBKRBroker
    broker = IBKRBroker(
        host=broker_cfg.get("host", "127.0.0.1"),
        port=broker_cfg.get("port", 4002),
        client_id=97,
        paper=broker_cfg.get("paper", True),
        readonly=True,
    )
    sym = market["currency_symbol"]
    mode = "PAPER" if broker_cfg.get("paper", True) else "LIVE"
    name_of = _name_lookup(config)

    try:
        broker.connect()

        summary = broker.get_account_summary()
        sum_table = Table(title=f"Account [{mode}]")
        sum_table.add_column("Tag"); sum_table.add_column("Value", justify="right")
        for tag in ("NetLiquidation", "TotalCashValue", "AvailableFunds", "BuyingPower"):
            v = summary.get(tag)
            sum_table.add_row(tag, f"{sym}{v:,.2f}" if v is not None else "—")
        console.print(sum_table)

        pos = broker.get_positions()
        if pos:
            pt = Table(title="Positions")
            pt.add_column("Ticker"); pt.add_column("Name")
            pt.add_column("Shares", justify="right"); pt.add_column("Avg cost", justify="right")
            for p in pos:
                pt.add_row(p["ticker"], name_of(p["ticker"]), str(p["shares"]),
                           f"{sym}{p['avg_cost']:,.2f}")
            console.print(pt)
        else:
            console.print("[dim]No open positions.[/dim]")

        orders = broker.get_orders()
        if orders:
            ot = Table(title="Open orders")
            for c in ("Order", "Ticker", "Name", "Side", "Shares", "Status", "Filled"):
                ot.add_column(c)
            for o in orders:
                ot.add_row(
                    str(o["order_id"]), o["ticker"], name_of(o["ticker"]),
                    o["side"], str(o["shares"]), o["status"], str(o["filled"]),
                )
            console.print(ot)
        else:
            console.print("[dim]No working orders.[/dim]")

        # Recent fills (today only, by IBKR's wall clock)
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date()
        fills = [f for f in broker._ib.fills() if f.execution.time.date() == today]
        if fills:
            ft = Table(title=f"Today's fills ({today})")
            for c in ("Time", "Ticker", "Name", "Side", "Shares", "Price"):
                ft.add_column(c)
            for f in fills:
                ft.add_row(
                    f.execution.time.strftime("%H:%M:%S"),
                    f.contract.symbol,
                    name_of(f.contract.symbol),
                    f.execution.side,
                    f"{int(f.execution.shares)}",
                    f"{sym}{float(f.execution.price):,.2f}",
                )
            console.print(ft)
        else:
            console.print("[dim]No fills today.[/dim]")
    finally:
        broker.disconnect()


def cmd_reconcile(args):
    """Diff local PaperTrader positions against the live broker."""
    from .paper_trader import PaperTrader
    from .notifier import LineNotifier

    config = load_config(args.config)
    market = get_market_settings(config)
    broker_cfg = config.get("broker", {})
    if not broker_cfg.get("enabled"):
        console.print("[yellow]broker not enabled in config — nothing to reconcile[/yellow]")
        return

    state_dir = Path(market["state_dir"]) if market["state_dir"] else None
    trader = PaperTrader(config["backtest"], state_dir=state_dir)
    local = {t: p.shares for t, p in trader.positions.items()}

    from .brokers.ibkr import IBKRBroker
    broker = IBKRBroker(
        host=broker_cfg.get("host", "127.0.0.1"),
        port=broker_cfg.get("port", 4002),
        client_id=broker_cfg.get("reconcile_client_id", 98),
        paper=broker_cfg.get("paper", True),
        readonly=True,
    )
    try:
        broker.connect()
        broker_positions = broker.get_positions()
    finally:
        broker.disconnect()
    broker_map = {p["ticker"]: p["shares"] for p in broker_positions}

    both = set(local) & set(broker_map)
    matched = {t for t in both if local[t] == broker_map[t]}
    mismatched = {t: (local[t], broker_map[t]) for t in both if local[t] != broker_map[t]}
    local_only = set(local) - set(broker_map)
    broker_only = set(broker_map) - set(local)

    name_of = _name_lookup(config)
    table = Table(title=f"Reconcile [{market['market_name']}]")
    table.add_column("Ticker")
    table.add_column("Name")
    table.add_column("Local shares", justify="right")
    table.add_column("Broker shares", justify="right")
    table.add_column("Status")
    for t in sorted(matched):
        table.add_row(t, name_of(t), str(local[t]), str(broker_map[t]), "[green]match[/green]")
    for t in sorted(mismatched):
        ls, bs = mismatched[t]
        table.add_row(t, name_of(t), str(ls), str(bs), "[red]MISMATCH[/red]")
    for t in sorted(local_only):
        table.add_row(t, name_of(t), str(local[t]), "—", "[yellow]local only[/yellow]")
    for t in sorted(broker_only):
        table.add_row(t, name_of(t), "—", str(broker_map[t]), "[red]BROKER ONLY[/red]")
    console.print(table)

    drift = bool(mismatched or broker_only)
    if drift:
        notifier = LineNotifier(
            config.get("line", {}),
            currency_symbol=market["currency_symbol"],
            market_name=market["market_name"],
        )
        lines = [f"⚠️ Reconcile drift [{market['market_name']}]"]
        for t, (ls, bs) in mismatched.items():
            n = name_of(t)
            lines.append(f"  {t} {n}: local={ls}, broker={bs}".rstrip())
        for t in broker_only:
            n = name_of(t)
            lines.append(f"  {t} {n}: broker={broker_map[t]} (not in local)".rstrip())
        notifier.send("\n".join(lines))
        console.print("[red]Drift detected — LINE alert sent.[/red]")
        sys.exit(1)
    if local_only:
        console.print(
            f"[yellow]{len(local_only)} local-only position(s) "
            "(likely pre-broker — not alerting)[/yellow]"
        )


def cmd_report(args):
    """Show paper trading report."""
    from .paper_trader import PaperTrader

    config = load_config(args.config)
    market = get_market_settings(config)
    names = config.get("watchlist_names", {})
    state_dir = Path(market["state_dir"]) if market["state_dir"] else None
    trader = PaperTrader(config["backtest"], state_dir=state_dir)
    sym = market["currency_symbol"]

    if args.reset:
        trader.reset()
        console.print("[bold]Paper trading state reset.[/bold]")
        return

    # Fetch current prices for open positions
    fetcher = DataFetcher(benchmark_ticker=market["benchmark_ticker"])
    price_dict = {}
    if trader.positions:
        tickers = list(trader.positions.keys())
        for p in fetcher.fetch_current_prices(tickers):
            price_dict[p["ticker"]] = p["price"]

    summary = trader.get_summary(price_dict)

    # Header. Use the config's broker section to label the report as Live or
    # Paper — the cli command can be pointed at either kind of state file.
    broker_cfg = config.get("broker", {})
    is_live = broker_cfg.get("enabled", False) and not broker_cfg.get("paper", True)
    mode_label = "Live" if is_live else "Paper"
    ret = summary["total_return_pct"]
    ret_color = "green" if ret > 0 else "red"
    console.print(Panel(
        f"Initial: {sym}{summary['initial_capital']:,.2f}\n"
        f"Current: {sym}{summary['total_value']:,.2f} [{ret_color}]({ret:+.2f}%)[/{ret_color}]\n"
        f"Cash: {sym}{summary['cash']:,.2f}\n"
        f"Days running: {summary['days_running']}\n"
        f"Closed trades: {summary['total_closed_trades']} "
        f"(W: {summary['winning_trades']} / L: {summary['losing_trades']} | "
        f"Win rate: {summary['win_rate']:.0f}%)\n"
        f"Total realized P&L: {sym}{summary['total_pnl']:+,.2f}",
        title=f"{mode_label} Trading Report [{market['market_name']}]",
        border_style="bold red" if is_live else "bold cyan",
    ))

    # Open positions
    if trader.positions:
        pos_table = Table(title="Open Positions", show_header=True, header_style="bold cyan")
        pos_table.add_column("Ticker", style="bold")
        pos_table.add_column("Name", style="dim")
        pos_table.add_column("Shares", justify="right")
        pos_table.add_column("Entry", justify="right")
        pos_table.add_column("Current", justify="right")
        pos_table.add_column("P&L", justify="right")
        pos_table.add_column("P&L %", justify="right")
        pos_table.add_column("Held Since")

        for ticker, pos in trader.positions.items():
            price = price_dict.get(ticker, pos.entry_price)
            pnl = pos.pnl(price)
            pnl_pct = pos.pnl_pct(price)
            color = "green" if pnl > 0 else "red"

            pos_table.add_row(
                ticker, pos.name, str(pos.shares),
                f"{sym}{pos.entry_price:,.2f}", f"{sym}{price:,.2f}",
                f"[{color}]{sym}{pnl:+,.2f}[/{color}]",
                f"[{color}]{pnl_pct:+.1f}%[/{color}]",
                pos.entry_date[:10],
            )

        console.print()
        console.print(pos_table)

    # Trade log
    if trader.trade_log:
        log_table = Table(title="Trade Log", show_header=True, header_style="bold cyan")
        log_table.add_column("Date", style="dim")
        log_table.add_column("Action", justify="center")
        log_table.add_column("Ticker", style="bold")
        log_table.add_column("Name")
        log_table.add_column("Price", justify="right")
        log_table.add_column("Shares", justify="right")
        log_table.add_column("P&L", justify="right")
        log_table.add_column("Reason")

        for trade in trader.trade_log[-20:]:  # Last 20 trades
            action = trade["action"]
            action_str = f"[green]{action}[/green]" if action == "BUY" else f"[red]{action}[/red]"
            pnl_str = ""
            reason = ""

            if action == "SELL":
                pnl = trade.get("pnl", 0)
                pnl_pct = trade.get("pnl_pct", 0)
                color = "green" if pnl > 0 else "red"
                pnl_str = f"[{color}]{sym}{pnl:+,.2f} ({pnl_pct:+.1f}%)[/{color}]"
                reason = trade.get("reason", "")
            else:
                reason = f"score: {trade.get('score', '')}"

            log_table.add_row(
                trade["timestamp"][:16], action_str,
                trade["ticker"], trade["name"],
                f"{sym}{trade['price']:,.2f}", str(trade["shares"]),
                pnl_str, reason,
            )

        console.print()
        console.print(log_table)

    # Daily equity curve
    if trader.daily_snapshots:
        console.print("\n[bold]Daily Equity:[/bold]")
        for snap in trader.daily_snapshots[-14:]:  # Last 2 weeks
            ret_val = snap["return_pct"]
            bar_len = int(abs(ret_val) * 5)
            if ret_val > 0:
                bar = f"[green]{'█' * bar_len} {ret_val:+.2f}%[/green]"
            elif ret_val < 0:
                bar = f"[red]{'█' * bar_len} {ret_val:+.2f}%[/red]"
            else:
                bar = f"{ret_val:+.2f}%"
            console.print(f"  {snap['date'][:10]} {sym}{snap['total']:>12,.2f} {bar}")


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
        f"Analyzing news sentiment for {len(tickers)} stocks via GPT",
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
    market = get_market_settings(config)
    tickers = args.tickers or config["watchlist"]
    days = args.days or 730
    ml_config = config.get("ml", {})
    model_name = ml_config.get("model_name", "default")

    console.print(Panel(
        f"Training ML model '{model_name}' on {len(tickers)} [{market['market_name']}] stocks over {days} days",
        title="ML Training",
        border_style="bold magenta",
    ))

    fetcher = DataFetcher(benchmark_ticker=market["benchmark_ticker"])
    params = config["strategy"]["params"]

    console.print("[bold]Fetching historical data...[/bold]")
    benchmark_df = fetcher.fetch_benchmark(days=days)
    data = fetcher.fetch_multiple(tickers, days=days)

    if not data:
        console.print("[red]No data fetched.[/red]")
        return

    console.print(f"[bold]Fetched {len(data)} stocks[/bold]")

    # Walk-forward evaluation first
    console.print("\n[bold]Running walk-forward evaluation (5 folds)...[/bold]")
    eval_result = walk_forward_evaluate(
        data, params, benchmark_df,
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
        data, params, benchmark_df,
        forward_days=ml_config.get("forward_days", 5),
        threshold=ml_config.get("threshold", 0.03),
        ml_params=ml_config.get("model_params"),
        save_name=model_name,
    )

    console.print(f"[green]Model saved to models/{model_name}.pkl[/green]")
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
    market = get_market_settings(config)
    tickers = args.tickers or config["watchlist"]
    names = config.get("watchlist_names", {})
    model_name = config.get("ml", {}).get("model_name", "default")
    sym = market["currency_symbol"]

    console.print(Panel(
        f"Scanning {len(tickers)} [{market['market_name']}] stocks for signals",
        title="Signal Scanner",
        border_style="bold blue",
    ))

    fetcher = DataFetcher(benchmark_ticker=market["benchmark_ticker"])
    strategy = SwingCompositeStrategy(config["strategy"]["params"], market["benchmark_name"])

    # Load ML model if available
    from .ml_model import MLPredictor
    ml = MLPredictor()
    if ml.load(model_name):
        strategy.set_ml_model(ml)
        console.print(f"[bold green]ML model '{model_name}' loaded[/bold green]")

    # Load LLM sentiment if configured
    sentiment_config = config.get("llm_sentiment", {})
    llm = LLMSentimentAnalyzer(sentiment_config)
    if llm.enabled:
        console.print("[bold]Analyzing news sentiment via GPT...[/bold]")
        sentiment_data = llm.analyze_multiple(tickers, names)
        strategy.set_sentiment_data(sentiment_data)
        console.print(f"[bold green]Sentiment analyzed for {len(sentiment_data)} stocks[/bold green]")

    console.print("[bold]Fetching data...[/bold]")
    benchmark_df = fetcher.fetch_benchmark(days=60)
    strategy.set_benchmark_data(benchmark_df)
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
            ticker, name, f"{sym}{signal.price:,.2f}",
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
  %(prog)s monitor --paper              Start monitor with paper trading
  %(prog)s monitor --paper --once      Run single cycle with paper trading
  %(prog)s report                      Show paper trading results
  %(prog)s report --reset              Reset paper trading state
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
    mo.add_argument("--paper", action="store_true", help="Enable paper trading (dry run)")
    mo.set_defaults(func=cmd_monitor)

    # Report
    rp = subparsers.add_parser("report", help="Show paper trading report")
    rp.add_argument("--reset", action="store_true", help="Reset paper trading state")
    rp.set_defaults(func=cmd_report)

    # Reconcile
    rc = subparsers.add_parser(
        "reconcile",
        help="Diff local positions vs broker; LINE-alert on drift",
    )
    rc.set_defaults(func=cmd_reconcile)

    # Broker status
    br = subparsers.add_parser(
        "broker",
        help="Show live broker account state (positions, orders, fills, cash)",
    )
    br.set_defaults(func=cmd_broker)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
