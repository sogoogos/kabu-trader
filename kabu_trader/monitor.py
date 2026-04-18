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
        self._sent_signals: set = set()  # track sent alerts to avoid duplicates

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

    def _analyze_signals(self):
        """Fetch recent data and analyze signals for all watchlist stocks."""
        self.alerts = []
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

    def run_once(self):
        """Run a single monitoring cycle and print results."""
        self.console.print("\n[bold]Fetching data...[/bold]")
        self._analyze_signals()
        self._send_line_alerts()

        prices = self.fetcher.fetch_current_prices(self.watchlist)

        self.console.print()
        self.console.print(self._build_price_table(prices))
        self.console.print()
        self.console.print(self._build_alerts_panel())

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
                    now = datetime.now(self.tz).strftime("%H:%M:%S")
                    self.console.print(
                        f"[dim][{now}] Market closed. Waiting...[/dim]"
                    )

                time.sleep(interval)
        except KeyboardInterrupt:
            self.console.print("\n[bold]Monitor stopped.[/bold]")
