"""Real-time stock price monitor with alert system."""

from __future__ import annotations

import time
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .data_fetcher import DataFetcher
from .strategy import SwingCompositeStrategy, Signal
from .notifier import LineNotifier
from .llm_sentiment import LLMSentimentAnalyzer
from .paper_trader import PaperTrader


class Monitor:
    """Real-time monitor that watches stocks and generates alerts."""

    def __init__(self, config: dict, names: Optional[Dict[str, str]] = None):
        self.watchlist = config["watchlist"]
        self.names = names or {}
        self.strategy_params = config["strategy"]["params"]
        self.monitor_config = config["monitor"]
        self.fetcher = DataFetcher()
        self.strategy = SwingCompositeStrategy(self.strategy_params)
        self.console = Console()
        self.alerts: List[dict] = []
        self.tz = ZoneInfo(self.monitor_config["timezone"])
        self.line = LineNotifier(config.get("line", {}))
        self.llm = LLMSentimentAnalyzer(config.get("llm_sentiment", {}))
        self._sent_signals: set = set()  # track sent alerts to avoid duplicates
        self._last_sentiment_time: float = 0  # timestamp of last sentiment refresh
        self._seen_headlines: set = set()  # track seen news headlines
        self.paper_trader: Optional[PaperTrader] = None

    def _is_trading_hours(self) -> bool:
        now = datetime.now(self.tz)
        start_h, start_m = map(int, self.monitor_config["trading_hours_start"].split(":"))
        end_h, end_m = map(int, self.monitor_config["trading_hours_end"].split(":"))

        start = now.replace(hour=start_h, minute=start_m, second=0)
        end = now.replace(hour=end_h, minute=end_m, second=0)

        # Also check weekday (Mon=0, Sun=6)
        if now.weekday() >= 5:
            return False

        return start <= now <= end

    def _build_price_table(self, prices: List[dict]) -> Table:
        table = Table(title="Stock Prices", show_header=True, header_style="bold cyan")
        table.add_column("Ticker", style="bold")
        table.add_column("Name", style="dim")
        table.add_column("Price", justify="right")
        table.add_column("Change %", justify="right")
        table.add_column("Volume", justify="right")
        table.add_column("Signal", justify="center")

        for p in prices:
            ticker = p["ticker"]
            name = self.names.get(ticker, "")
            change = p["change_pct"]

            if change > 0:
                change_str = f"[green]+{change:.2f}%[/green]"
            elif change < 0:
                change_str = f"[red]{change:.2f}%[/red]"
            else:
                change_str = f"{change:.2f}%"

            # Get signal for this ticker
            signal_str = ""
            for alert in self.alerts:
                if alert["ticker"] == ticker:
                    sig = alert["signal"]
                    if "BUY" in sig:
                        signal_str = f"[bold green]{sig}[/bold green]"
                    elif "SELL" in sig:
                        signal_str = f"[bold red]{sig}[/bold red]"
                    else:
                        signal_str = sig
                    break

            table.add_row(
                ticker,
                name,
                f"¥{p['price']:,.0f}" if p["price"] else "N/A",
                change_str,
                f"{p['volume']:,.0f}" if p.get("volume") else "N/A",
                signal_str,
            )

        return table

    def _build_alerts_panel(self) -> Panel:
        if not self.alerts:
            return Panel("[dim]No active signals[/dim]", title="Alerts", border_style="dim")

        lines = []
        for alert in self.alerts[-10:]:  # Show last 10
            sig = alert["signal"]
            if "BUY" in sig:
                color = "green"
            elif "SELL" in sig:
                color = "red"
            else:
                color = "yellow"

            name = self.names.get(alert["ticker"], alert["ticker"])
            lines.append(
                f"[{color}][{alert['time']}] {sig} {name} ({alert['ticker']}) "
                f"@ ¥{alert['price']:,.0f} (score: {alert['score']})[/{color}]"
            )
            for reason in alert.get("reasons", []):
                lines.append(f"  [dim]- {reason}[/dim]")

        return Panel("\n".join(lines), title="Trading Signals", border_style="bold yellow")

    def _refresh_sentiment(self):
        """Refresh LLM sentiment analysis once per hour."""
        if not self.llm.enabled:
            return

        import time as _time
        now = _time.time()
        if now - self._last_sentiment_time < 3600:
            return

        self.console.print("[bold]Refreshing news sentiment via GPT...[/bold]")
        sentiment_data = self.llm.analyze_multiple(self.watchlist, self.names)
        self.strategy.set_sentiment_data(sentiment_data)
        self._last_sentiment_time = now

        # Seed seen headlines so we don't re-alert on existing news
        from .news_fetcher import fetch_stock_news
        for ticker in self.watchlist:
            for item in fetch_stock_news(ticker, 5):
                self._seen_headlines.add(item["title"])

        self.console.print(
            f"[bold green]Sentiment updated for {len(sentiment_data)} stocks[/bold green]"
        )

    def _check_breaking_news(self):
        """Check for new headlines since last cycle. Analyze and alert if significant."""
        if not self.llm.enabled:
            return

        from .news_fetcher import fetch_stock_news

        for ticker in self.watchlist:
            news = fetch_stock_news(ticker, 3)
            new_headlines = []

            for item in news:
                title = item["title"]
                if title and title not in self._seen_headlines:
                    self._seen_headlines.add(title)
                    new_headlines.append(item)

            if not new_headlines:
                continue

            # New headline(s) detected — analyze just these
            name = self.names.get(ticker, ticker)
            headlines_text = "\n".join(
                f"- {h['title']} ({h['publisher']})" for h in new_headlines
            )

            self.console.print(
                f"[bold yellow]Breaking news for {name}:[/bold yellow] "
                f"{new_headlines[0]['title']}"
            )

            result = self.llm.analyze_stock(
                ticker, company_name=name, price=0,
                performance=f"Breaking: {len(new_headlines)} new headline(s)",
            )

            if not result:
                continue

            score = result.get("score", 0)
            reasoning = result.get("reasoning", "")

            # Update sentiment data in strategy
            sentiment_data = dict(self.strategy.sentiment_data)
            sentiment_data[ticker] = result
            self.strategy.set_sentiment_data(sentiment_data)

            # Alert if significant (score >= 3 or <= -3)
            if abs(score) >= 3 and self.line.enabled:
                direction = "BULLISH" if score > 0 else "BEARISH"
                message = (
                    f"🚨 Breaking News Alert\n"
                    f"\n"
                    f"{'🟢' if score > 0 else '🔴'} {direction} ({score:+d})\n"
                    f"📌 {name} ({ticker})\n"
                    f"\n"
                    f"📰 {new_headlines[0]['title']}\n"
                    f"\n"
                    f"💡 {reasoning}"
                )

                today = datetime.now(self.tz).strftime("%Y-%m-%d")
                key = f"{today}:news:{ticker}:{new_headlines[0]['title'][:50]}"
                if key not in self._sent_signals:
                    if self.line.send(message):
                        self._sent_signals.add(key)
                        self.console.print(
                            f"[bold yellow]LINE breaking news alert sent for {name}[/bold yellow]"
                        )

    def _analyze_signals(self):
        """Fetch recent data and analyze signals for all watchlist stocks."""
        self.alerts = []
        self._refresh_sentiment()
        nikkei_df = self.fetcher.fetch_nikkei225(days=60)
        self.strategy.set_nikkei_data(nikkei_df)
        data = self.fetcher.fetch_multiple(self.watchlist, days=60, interval="1d")

        for ticker, df in data.items():
            if len(df) < 30:
                continue
            signal = self.strategy.get_latest_signal(df, ticker)
            if signal.signal != Signal.HOLD:
                is_strong = signal.signal in (Signal.STRONG_BUY, Signal.STRONG_SELL)
                self.alerts.append({
                    "ticker": ticker,
                    "signal": signal.signal.name,
                    "score": signal.score,
                    "price": signal.price,
                    "reasons": signal.reasons,
                    "time": datetime.now(self.tz).strftime("%H:%M:%S"),
                    "notify": is_strong,
                })

    def _send_line_alerts(self):
        """Send LINE messages for new alerts (avoids duplicates within the same day)."""
        if not self.line.enabled:
            return

        today = datetime.now(self.tz).strftime("%Y-%m-%d")
        for alert in self.alerts:
            if not alert.get("notify", False):
                continue
            key = f"{today}:{alert['ticker']}:{alert['signal']}"
            if key not in self._sent_signals:
                name = self.names.get(alert["ticker"], "")
                message = self.line.format_alert(alert, name)
                if self.line.send(message):
                    self._sent_signals.add(key)
                    self.console.print(f"[bold yellow]LINE sent for {alert['ticker']}[/bold yellow]")

    def _execute_paper_trades(self, prices: list):
        """Execute paper trades based on current signals and prices."""
        if not self.paper_trader:
            return

        now_str = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M:%S")

        # Build price dict
        price_dict = {p["ticker"]: p["price"] for p in prices if p.get("price")}

        # Check stop loss / take profit first
        sl_tp_actions = self.paper_trader.check_stop_loss_take_profit(price_dict, now_str)
        for action in sl_tp_actions:
            pnl = action["pnl"]
            color = "green" if pnl > 0 else "red"
            self.console.print(
                f"[bold {color}]PAPER {action['action']}: {action['name']} ({action['ticker']}) "
                f"@ ¥{action['price']:,.0f} | {action['reason']} | "
                f"P&L: ¥{pnl:+,.0f} ({action['pnl_pct']:+.1f}%)[/bold {color}]"
            )
            # Send LINE for stop loss / take profit
            if self.line.enabled:
                msg = (
                    f"📋 Paper Trade: {action['reason'].upper()}\n"
                    f"{'🟢' if pnl > 0 else '🔴'} SELL {action['name']}\n"
                    f"💰 ¥{action['price']:,.0f} → P&L: ¥{pnl:+,.0f} ({action['pnl_pct']:+.1f}%)"
                )
                self.line.send(msg)

        # Process signals
        for alert in self.alerts:
            ticker = alert["ticker"]
            price = price_dict.get(ticker, 0)
            if price <= 0:
                continue

            name = self.names.get(ticker, ticker)
            action = self.paper_trader.process_signal(
                ticker=ticker, name=name, signal=alert["signal"],
                score=alert["score"], price=price, reasons=alert["reasons"],
                timestamp=now_str,
            )

            if action:
                if action["action"] == "BUY":
                    self.console.print(
                        f"[bold green]PAPER BUY: {name} ({ticker}) "
                        f"{action['shares']} shares @ ¥{price:,.0f} "
                        f"(score: {action['score']})[/bold green]"
                    )
                elif action["action"] == "SELL":
                    pnl = action["pnl"]
                    color = "green" if pnl > 0 else "red"
                    self.console.print(
                        f"[bold {color}]PAPER SELL: {name} ({ticker}) "
                        f"@ ¥{price:,.0f} | P&L: ¥{pnl:+,.0f} "
                        f"({action['pnl_pct']:+.1f}%)[/bold {color}]"
                    )

        # Daily snapshot
        self.paper_trader.take_daily_snapshot(price_dict, now_str)

    def _build_paper_panel(self, prices: list) -> Panel:
        """Build paper trading status panel."""
        if not self.paper_trader:
            return Panel("[dim]Paper trading disabled[/dim]", title="Paper Trading", border_style="dim")

        price_dict = {p["ticker"]: p["price"] for p in prices if p.get("price")}
        summary = self.paper_trader.get_summary(price_dict)

        ret = summary["total_return_pct"]
        ret_color = "green" if ret > 0 else "red" if ret < 0 else "white"

        lines = [
            f"Capital: ¥{summary['initial_capital']:,.0f} → ¥{summary['total_value']:,.0f} "
            f"[{ret_color}]({ret:+.2f}%)[/{ret_color}]",
            f"Cash: ¥{summary['cash']:,.0f} | Positions: {summary['open_positions']}/{self.paper_trader.max_positions}",
            f"Trades: {summary['total_closed_trades']} closed "
            f"(Win: {summary['winning_trades']} / Loss: {summary['losing_trades']} | "
            f"Rate: {summary['win_rate']:.0f}%)",
            f"Running: {summary['days_running']} day(s)",
        ]

        # Show open positions
        if self.paper_trader.positions:
            lines.append("")
            lines.append("[bold]Open positions:[/bold]")
            for ticker, pos in self.paper_trader.positions.items():
                price = price_dict.get(ticker, pos.entry_price)
                pnl = pos.pnl(price)
                pnl_pct = pos.pnl_pct(price)
                color = "green" if pnl > 0 else "red"
                lines.append(
                    f"  [{color}]{pos.name} ({ticker}): "
                    f"{pos.shares} shares @ ¥{pos.entry_price:,.0f} → ¥{price:,.0f} "
                    f"(¥{pnl:+,.0f} / {pnl_pct:+.1f}%)[/{color}]"
                )

        return Panel("\n".join(lines), title="Paper Trading", border_style="bold cyan")

    def run_once(self):
        """Run a single monitoring cycle and print results."""
        self.console.print("\n[bold]Fetching data...[/bold]")
        self._check_breaking_news()
        self._analyze_signals()
        self._send_line_alerts()

        prices = self.fetcher.fetch_current_prices(self.watchlist)

        # Execute paper trades
        self._execute_paper_trades(prices)

        self.console.print()
        self.console.print(self._build_price_table(prices))
        self.console.print()
        self.console.print(self._build_alerts_panel())
        self.console.print()
        self.console.print(self._build_paper_panel(prices))

        now = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        self.console.print(f"\n[dim]Last updated: {now}[/dim]")

    def run_continuous(self):
        """Run continuous monitoring loop."""
        interval = self.monitor_config["interval_seconds"]

        self.console.print(Panel(
            f"Monitoring {len(self.watchlist)} stocks every {interval}s\n"
            f"Trading hours: {self.monitor_config['trading_hours_start']} - "
            f"{self.monitor_config['trading_hours_end']} JST\n"
            f"Press Ctrl+C to stop",
            title="Kabu Trader Monitor",
            border_style="bold blue",
        ))

        try:
            while True:
                if self._is_trading_hours():
                    self.run_once()
                else:
                    # Outside market hours: still check for breaking news
                    self._check_breaking_news()
                    self._refresh_sentiment()
                    now = datetime.now(self.tz).strftime("%H:%M:%S")
                    self.console.print(
                        f"[dim][{now}] Market closed. Watching for news...[/dim]"
                    )

                time.sleep(interval)
        except KeyboardInterrupt:
            self.console.print("\n[bold]Monitor stopped.[/bold]")
