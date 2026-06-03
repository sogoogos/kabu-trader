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
from .email_notifier import EmailNotifier
from .llm_sentiment import LLMSentimentAnalyzer
from .earnings_tracker import EarningsTracker
from .corporate_actions import CorporateActionsTracker
from .paper_trader import PaperTrader


class Monitor:
    """Real-time monitor that watches stocks and generates alerts."""

    def __init__(
        self,
        config: dict,
        names: Optional[Dict[str, str]] = None,
        aliases: Optional[Dict[str, List[str]]] = None,
    ):
        self.watchlist = config["watchlist"]
        self.names = names or {}
        self.aliases = aliases or {}
        self.strategy_params = config["strategy"]["params"]
        self.monitor_config = config["monitor"]
        market_config = config.get("market", {})
        self.currency_symbol = market_config.get("currency_symbol", "¥")
        self.benchmark_name = market_config.get("benchmark_name", "Nikkei 225")
        self.market_name = market_config.get("name", "JP")
        benchmark_ticker = market_config.get("benchmark_ticker", "^N225")
        self.fetcher = DataFetcher(benchmark_ticker=benchmark_ticker)
        self.strategy = SwingCompositeStrategy(self.strategy_params, self.benchmark_name)
        self.console = Console()
        self.alerts: List[dict] = []
        self.tz = ZoneInfo(self.monitor_config["timezone"])
        self.line = LineNotifier(
            config.get("line", {}),
            currency_symbol=self.currency_symbol,
            market_name=self.market_name,
        )
        self.email = EmailNotifier(config.get("email", {}))
        # Persist the sentiment cache under the market's state_dir so a container
        # restart (which is otherwise routine) doesn't trigger a 5-10 minute
        # cold-start refresh of 400+ tickers. Path falls back to None if no
        # state_dir is configured, in which case the cache stays in-memory.
        sentiment_cache_path: Optional[Path] = None
        state_dir_str = market_config.get("state_dir")
        if state_dir_str:
            from .paper_trader import PROJECT_ROOT
            state_dir_path = Path(state_dir_str)
            if not state_dir_path.is_absolute():
                state_dir_path = PROJECT_ROOT / state_dir_path
            sentiment_cache_path = state_dir_path / "sentiment_cache.json"
        self.llm = LLMSentimentAnalyzer(
            config.get("llm_sentiment", {}), cache_path=sentiment_cache_path,
        )
        self.earnings = EarningsTracker()
        self.corporate_actions = CorporateActionsTracker()
        self.config = config
        self._sent_signals: set = set()  # track sent alerts to avoid duplicates
        self._last_sentiment_time: float = 0  # timestamp of last sentiment refresh
        self._last_earnings_time: float = 0  # timestamp of last earnings refresh
        self._last_summary_date: str = ""  # YYYY-MM-DD of last daily summary sent
        self._last_actions_check: float = 0  # timestamp of last corporate-actions check
        self._last_retrain_week: int = -1  # ISO week number of last retrain
        self._seen_headlines: set = set()  # track seen news headlines
        self.paper_trader: Optional[PaperTrader] = None

        # Broker watchdog state. The IBKR Gateway needs nightly 2FA approval;
        # a missed approval leaves the API offline silently for hours. These
        # track when the outage started and when we last alerted so the LINE
        # message fires once shortly after open and then periodically — not
        # every cycle.
        self._broker_down_since: Optional[float] = None
        self._broker_alerted_at: float = 0.0

        # Per-ticker breaking-news cooldown. Different publishers recycle the
        # same story (e.g. Micron at $1T market cap → 7 alerts in one day from
        # different outlets). Title-level dedup doesn't catch that. Once we
        # alert on a ticker we suppress further news alerts for that ticker
        # until the cooldown expires.
        self._last_news_alert_per_ticker: Dict[str, float] = {}

        # Seed headlines at startup so existing news doesn't trigger alerts
        self._seed_headlines()

    def _seed_headlines(self):
        """Load all current headlines so only truly new ones trigger alerts."""
        from .news_fetcher import fetch_market_news
        news_by_ticker = fetch_market_news(self.market_name, self.names, self.aliases)
        for items in news_by_ticker.values():
            for item in items:
                if item["title"]:
                    self._seen_headlines.add(item["title"])

    def _is_live(self) -> bool:
        """True when the broker is wired through to a real-money IBKR account.

        PaperTrader is used for the local ledger in both paper- and live-mode,
        so its presence alone doesn't indicate live trading; the live_broker
        attribute is what distinguishes them.
        """
        return bool(self.paper_trader and getattr(self.paper_trader, "live_broker", None))

    @property
    def _mode_tag(self) -> str:
        return "💹 LIVE" if self._is_live() else "🧪 PAPER"

    @property
    def _mode_label(self) -> str:
        return "Live" if self._is_live() else "Paper"

    def _notify(self, subject: str, message: str, force_email: bool = False) -> bool:
        """Send an alert via LINE; fall back to email if LINE fails.

        LINE's 200 msg/mo free-tier cap means routine alerts (trades, breaking
        news, daily summary) can silently disappear once the quota is hit. The
        email channel has no such cap, so we use it as a safety net.

        - force_email=False (default): email only fires when LINE returns False.
          Conserves email volume during normal operation.
        - force_email=True: send via both channels regardless. Reserved for the
          highest-priority alerts (broker watchdog) where missed delivery is
          unacceptable.

        Returns True if at least one channel succeeded.
        """
        line_ok = self.line.send(message)
        if force_email or not line_ok:
            email_ok = self.email.send(subject, message)
            return line_ok or email_ok
        return line_ok

    def _check_broker_health(self) -> None:
        """Watchdog: alert via LINE if the IBKR Gateway is offline during trading hours.

        Silent outside trading hours so the nightly forced-logout cycle doesn't spam.
        Sends one alert after `alert_after_seconds` of continuous downtime, then
        repeats every `alert_repeat_seconds` so the user can't miss it. Emits a
        recovery alert when the broker comes back.
        """
        broker = getattr(self.paper_trader, "live_broker", None) if self.paper_trader else None
        if broker is None or not hasattr(broker, "is_healthy"):
            return
        if not self._is_trading_hours():
            return

        broker_cfg = self.config.get("broker", {})
        alert_after = broker_cfg.get("health_alert_after_seconds", 300)
        repeat_every = broker_cfg.get("health_alert_repeat_seconds", 1800)

        ok, reason = broker.is_healthy()
        now = time.time()

        if not ok:
            if self._broker_down_since is None:
                self._broker_down_since = now
                self.console.print(f"[yellow]Broker health check failed: {reason}[/yellow]")
            downtime = now - self._broker_down_since
            should_alert = (
                downtime >= alert_after
                and (self._broker_alerted_at == 0.0
                     or now - self._broker_alerted_at >= repeat_every)
            )
            if should_alert:
                mins = int(downtime / 60)
                subject = f"[kabu-trader {self.market_name}] IBKR Gateway DOWN ({mins}m)"
                msg = (
                    f"🚨 IBKR Gateway DOWN [{self.market_name}]\n"
                    f"\n"
                    f"API unreachable for {mins} min during trading hours.\n"
                    f"Reason: {reason}\n"
                    f"\n"
                    f"Likely cause: missed 2FA approval after nightly logout.\n"
                    f"Fix: ssh kabu-ec2 'docker restart ib-gateway' then approve\n"
                    f"the IB Key push on your phone within 30s."
                )
                if self._notify(subject, msg, force_email=True):
                    self.console.print(
                        f"[bold red]Broker down {mins}m alert sent[/bold red]"
                    )
                self._broker_alerted_at = now
        else:
            if self._broker_alerted_at > 0:
                subject = f"[kabu-trader {self.market_name}] IBKR Gateway RECOVERED"
                msg = (
                    f"✅ IBKR Gateway RECOVERED [{self.market_name}]\n"
                    f"\n"
                    f"API is responsive again. Trading resumed."
                )
                self._notify(subject, msg, force_email=True)
                self.console.print(f"[bold green]Broker recovered; recovery alert sent[/bold green]")
            self._broker_down_since = None
            self._broker_alerted_at = 0.0

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

            sym = self.currency_symbol
            table.add_row(
                ticker,
                name,
                f"{sym}{p['price']:,.2f}" if p["price"] else "N/A",
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
            sym = self.currency_symbol
            lines.append(
                f"[{color}][{alert['time']}] {sig} {name} ({alert['ticker']}) "
                f"@ {sym}{alert['price']:,.2f} (score: {alert['score']})[/{color}]"
            )
            for reason in alert.get("reasons", []):
                lines.append(f"  [dim]- {reason}[/dim]")

        return Panel("\n".join(lines), title="Trading Signals", border_style="bold yellow")

    def _refresh_sentiment(self):
        """Refresh LLM sentiment analysis.

        Interval is configurable via config.llm_sentiment.refresh_interval_seconds
        (default 21600 = 6 hours). Breaking-news path handles event-driven alerts
        separately at 60s cadence, so this only affects how stale the sentiment
        score in the composite signal can get.
        """
        if not self.llm.enabled:
            return

        interval = self.config.get("llm_sentiment", {}).get(
            "refresh_interval_seconds", 6 * 3600
        )

        import time as _time
        now = _time.time()
        if now - self._last_sentiment_time < interval:
            return

        self.console.print("[bold]Refreshing news sentiment via GPT...[/bold]")
        sentiment_data = self.llm.analyze_multiple(self.watchlist, self.names)
        self.strategy.set_sentiment_data(sentiment_data)
        self._last_sentiment_time = now
        self.console.print(
            f"[bold green]Sentiment updated for {len(sentiment_data)} stocks[/bold green]"
        )

    def _refresh_earnings(self):
        """Refresh earnings-gap data once a day.

        Yahoo/yfinance rate-limits aggressively. The tracker itself caches tickers
        with no recent earnings for 7 days, so the per-cycle cost is small, but we
        only trigger the iteration once/day to be safe.
        """
        import time as _time
        now = _time.time()
        if now - self._last_earnings_time < 24 * 3600:
            return

        self.console.print("[bold]Refreshing earnings-day gaps...[/bold]")
        earnings_data = self.earnings.refresh_all(self.watchlist)
        self.strategy.set_earnings_data(earnings_data)
        self._last_earnings_time = now
        self.console.print(
            f"[bold green]Earnings data updated for {len(earnings_data)} stocks[/bold green]"
        )

    def _check_corporate_actions(self):
        """Apply any splits/dividends that occurred for held positions.

        Runs weekly. Only iterates positions we actually hold (typically ≤10),
        so the yfinance call volume is trivial.
        """
        if not self.paper_trader or not self.paper_trader.positions:
            return
        import time as _time
        now = _time.time()
        if now - self._last_actions_check < 7 * 24 * 3600:
            return

        self.console.print("[bold]Checking corporate actions for held positions...[/bold]")
        actions_by_ticker = {}
        for ticker, pos in self.paper_trader.positions.items():
            result = self.corporate_actions.get_actions_since(ticker, pos.entry_date)
            if result and (result["splits"] or result["dividends"]):
                actions_by_ticker[ticker] = result

        if actions_by_ticker:
            applied = self.paper_trader.apply_corporate_actions(actions_by_ticker)
            sym = self.currency_symbol
            for entry in applied:
                if entry["action"] == "ADJUST_SPLIT":
                    self.console.print(
                        f"[bold cyan]SPLIT applied to {entry['name']} "
                        f"({entry['ticker']}): {entry['reason']} on {entry['timestamp'][:10]}"
                        f" — entry now {sym}{entry['price']:.2f}, shares {entry['shares']}[/bold cyan]"
                    )
                elif entry["action"] == "DIVIDEND":
                    self.console.print(
                        f"[bold cyan]DIVIDEND credited for {entry['name']} "
                        f"({entry['ticker']}): {sym}{entry['proceeds']:.2f} "
                        f"({entry['reason']})[/bold cyan]"
                    )

        self._last_actions_check = now

    def _send_daily_summary(self):
        """Send a LINE end-of-day summary of paper trading performance.

        Fires once per trading day, shortly after the market closes. Uses the
        data fetcher's cache for current prices — no extra HTTP calls.
        """
        if not self.line.enabled or not self.paper_trader:
            return

        now = datetime.now(self.tz)
        today = now.strftime("%Y-%m-%d")
        if self._last_summary_date == today:
            return

        # Only fire on weekdays after trading_hours_end.
        if now.weekday() > 4:
            return
        end_h, end_m = map(int, self.monitor_config["trading_hours_end"].split(":"))
        end_time = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if now < end_time:
            return

        # Build price dict from cached OHLCV (last close) — no network calls.
        price_dict = {}
        for ticker in self.paper_trader.positions:
            df = self.fetcher.get_cached(ticker)
            if df is not None and not df.empty:
                price_dict[ticker] = float(df["Close"].iloc[-1])

        summary = self.paper_trader.get_summary(price_dict)

        today_trades = [
            t for t in self.paper_trader.trade_log
            if t.get("timestamp", "").startswith(today)
        ]
        buys = [t for t in today_trades if t["action"] == "BUY"]
        sells = [t for t in today_trades if t["action"] == "SELL"]
        day_pnl = sum(t.get("pnl", 0) for t in sells)

        sym = self.currency_symbol
        ret = summary["total_return_pct"]
        msg = (
            f"📊 [{self.market_name}] Daily Summary [{self._mode_tag}]\n"
            f"{'🟢' if ret >= 0 else '🔴'} Total return: {ret:+.2f}%\n"
            f"💰 Value: {sym}{summary['total_value']:,.0f} "
            f"(cash {sym}{summary['cash']:,.0f})\n"
            f"📈 Today: {len(buys)} BUY / {len(sells)} SELL"
            + (f" | realized {sym}{day_pnl:+,.0f}" if sells else "")
            + f"\n"
            f"📌 Positions: {summary['open_positions']}/{self.paper_trader.max_positions} open\n"
            f"🏆 Overall: {summary['total_closed_trades']} closed trades, "
            f"{summary['win_rate']:.0f}% win rate"
        )
        subject = f"[kabu-trader {self.market_name}] Daily summary {today}"
        if self._notify(subject, msg):
            self._last_summary_date = today
            self.console.print(
                f"[bold cyan]Daily summary sent for {self.market_name}[/bold cyan]"
            )

    def _auto_retrain(self):
        """Retrain ML model once per week (Sunday night)."""
        now = datetime.now(self.tz)

        # Only retrain on Sundays after 20:00
        if now.weekday() != 6 or now.hour < 20:
            return

        # Only retrain once per ISO week
        current_week = now.isocalendar()[1]
        if current_week == self._last_retrain_week:
            return

        self.console.print("[bold magenta]Auto-retraining ML model (weekly)...[/bold magenta]")

        try:
            from .ml_model import train_final_model

            params = self.config["strategy"]["params"]
            ml_config = self.config.get("ml", {})

            data = self.fetcher.fetch_multiple(self.watchlist, days=730)
            benchmark_df = self.fetcher.fetch_benchmark(days=730)
            model_name = ml_config.get("model_name", "default")

            model, metrics = train_final_model(
                data, params, benchmark_df,
                forward_days=ml_config.get("forward_days", 5),
                threshold=ml_config.get("threshold", 0.03),
                ml_params=ml_config.get("model_params"),
                save_name=model_name,
            )

            self.strategy.set_ml_model(model)
            self._last_retrain_week = current_week

            self.console.print(
                f"[bold green]ML model retrained — "
                f"Accuracy: {metrics['accuracy']:.3f} | "
                f"AUC-ROC: {metrics['auc_roc']:.3f}[/bold green]"
            )

            self._notify(
                f"[kabu-trader {self.market_name}] ML model retrained",
                (f"🤖 ML Model Retrained [{self._mode_tag}]\n"
                 f"\n"
                 f"Accuracy: {metrics['accuracy']:.3f}\n"
                 f"AUC-ROC: {metrics['auc_roc']:.3f}\n"
                 f"Trained on {len(data)} stocks, 2 years of data"),
            )
        except Exception as e:
            self.console.print(f"[red]Auto-retrain failed: {e}[/red]")

    def _check_breaking_news(self):
        """Check for new headlines since last cycle. Analyze and alert if significant."""
        if not self.llm.enabled:
            return

        from .news_fetcher import fetch_market_news

        news_by_ticker = fetch_market_news(self.market_name, self.names, self.aliases)

        for ticker, news in news_by_ticker.items():
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

            # Alert if significant. Threshold is configurable via
            # config.llm_sentiment.breaking_alert_threshold (default 3).
            alert_threshold = self.config.get("llm_sentiment", {}).get(
                "breaking_alert_threshold", 3
            )
            if abs(score) < alert_threshold:
                self.console.print(
                    f"[dim]  → score {score:+d} below threshold "
                    f"±{alert_threshold}, no LINE alert[/dim]"
                )
                continue
            if not self.line.enabled:
                continue

            cooldown_secs = self.config.get("llm_sentiment", {}).get(
                "alert_cooldown_seconds", 21600  # 6h default
            )
            now_ts = time.time()
            last_ts = self._last_news_alert_per_ticker.get(ticker, 0.0)
            if now_ts - last_ts < cooldown_secs:
                remaining_min = int((cooldown_secs - (now_ts - last_ts)) / 60)
                self.console.print(
                    f"[dim]  → {ticker} cooldown active ({remaining_min}m left), no LINE alert[/dim]"
                )
                continue

            direction = "BULLISH" if score > 0 else "BEARISH"
            from .news_fetcher import shorten_url
            link = shorten_url(new_headlines[0].get("link", ""))
            message = (
                f"🚨 Breaking News Alert [{self._mode_tag}]\n"
                f"\n"
                f"{'🟢' if score > 0 else '🔴'} {direction} ({score:+d})\n"
                f"📌 {name} ({ticker})\n"
                f"\n"
                f"📰 {new_headlines[0]['title']}\n"
                f"\n"
                f"💡 {reasoning}"
            )
            if link:
                message += f"\n\n🔗 {link}"

            today = datetime.now(self.tz).strftime("%Y-%m-%d")
            key = f"{today}:news:{ticker}:{new_headlines[0]['title'][:50]}"
            if key not in self._sent_signals:
                subject = f"[kabu-trader {self.market_name}] Breaking: {name} ({ticker}) {direction.lower()}"
                if self._notify(subject, message):
                    self._sent_signals.add(key)
                    self._last_news_alert_per_ticker[ticker] = now_ts
                    self.console.print(
                        f"[bold yellow]Breaking news alert sent for {name}[/bold yellow]"
                    )

    def _analyze_signals(self):
        """Fetch recent data and analyze signals for all watchlist stocks."""
        self.alerts = []
        self._refresh_sentiment()
        self._refresh_earnings()
        self._check_corporate_actions()
        # Ichimoku Senkou_B is rolling(52).max().shift(26) — needs 78 non-NaN
        # bars before the latest. days=60 (~40 trading days) leaves Senkou_A/B
        # NaN at the tail, which made `_score_ichimoku` silently return 0 in
        # live even though it's the highest-weight scorer (2.5) and contributed
        # normally during backtest/training (which fetch 365-730 days).
        # 180 calendar days ≈ 125 trading days gives ample headroom for the
        # 78-bar window plus weekends and holidays.
        benchmark_df = self.fetcher.fetch_benchmark(days=180)
        self.strategy.set_benchmark_data(benchmark_df)
        data = self.fetcher.fetch_multiple(self.watchlist, days=180, interval="1d")

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
        """Send LINE messages for STRONG_BUY/STRONG_SELL signals across the watchlist.

        Off by default — fires per signal regardless of whether a trade actually
        executes, which can blow through LINE's 200 msg/month free quota fast.
        Enable via config.line.notify_strong_signals: true if you want them.
        """
        if not self.config.get("line", {}).get("notify_strong_signals", False):
            return

        today = datetime.now(self.tz).strftime("%Y-%m-%d")
        for alert in self.alerts:
            if not alert.get("notify", False):
                continue
            key = f"{today}:{alert['ticker']}:{alert['signal']}"
            if key not in self._sent_signals:
                name = self.names.get(alert["ticker"], "")
                message = self.line.format_alert(alert, name)
                subject = f"[kabu-trader {self.market_name}] {alert['signal']} {name or alert['ticker']}"
                if self._notify(subject, message):
                    self._sent_signals.add(key)
                    self.console.print(f"[bold yellow]Strong-signal alert sent for {alert['ticker']}[/bold yellow]")

    def _execute_paper_trades(self, prices: list):
        """Execute paper trades based on current signals and prices."""
        if not self.paper_trader:
            return

        now_str = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M:%S")

        # Build price dict
        price_dict = {p["ticker"]: p["price"] for p in prices if p.get("price")}

        # Check stop loss / take profit first
        sl_tp_actions = self.paper_trader.check_stop_loss_take_profit(price_dict, now_str)
        sym = self.currency_symbol
        for action in sl_tp_actions:
            pnl = action["pnl"]
            color = "green" if pnl > 0 else "red"
            self.console.print(
                f"[bold {color}]PAPER {action['action']}: {action['name']} ({action['ticker']}) "
                f"@ {sym}{action['price']:,.2f} | {action['reason']} | "
                f"P&L: {sym}{pnl:+,.2f} ({action['pnl_pct']:+.1f}%)[/bold {color}]"
            )
            msg = (
                f"📋 [{self.market_name}] Trade: {action['reason'].upper()} [{self._mode_tag}]\n"
                f"{'🟢' if pnl > 0 else '🔴'} SELL {action['name']}\n"
                f"💰 {sym}{action['price']:,.2f} → P&L: {sym}{pnl:+,.2f} ({action['pnl_pct']:+.1f}%)"
            )
            subject = (
                f"[kabu-trader {self.market_name}] {action['reason'].upper()} "
                f"{action['name']} ({action['ticker']}) {action['pnl_pct']:+.1f}%"
            )
            self._notify(subject, msg)

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
                timestamp=now_str, current_prices=price_dict,
            )

            if action:
                # Per-trade LINE notifications can be muted to conserve LINE
                # quota (free tier is 500 msgs/month). Daily summary + STRONG
                # signals + breaking news fire regardless.
                notify_trades = self.config.get("line", {}).get("notify_paper_trades", True)
                if action["action"] == "BUY":
                    self.console.print(
                        f"[bold green]{self._mode_label.upper()} BUY: {name} ({ticker}) "
                        f"{action['shares']} shares @ {sym}{price:,.2f} "
                        f"(score: {action['score']})[/bold green]"
                    )
                    if notify_trades:
                        self._notify(
                            f"[kabu-trader {self.market_name}] {self._mode_label} BUY {name} ({ticker})",
                            (f"🛒 [{self.market_name}] {self._mode_label} BUY [{self._mode_tag}]\n"
                             f"🟢 {name} ({ticker})\n"
                             f"💰 {action['shares']} shares @ {sym}{price:,.2f}\n"
                             f"📊 Signal score: {action['score']:+d}"),
                        )
                elif action["action"] == "SELL":
                    pnl = action["pnl"]
                    color = "green" if pnl > 0 else "red"
                    self.console.print(
                        f"[bold {color}]{self._mode_label.upper()} SELL: {name} ({ticker}) "
                        f"@ {sym}{price:,.2f} | P&L: {sym}{pnl:+,.2f} "
                        f"({action['pnl_pct']:+.1f}%)[/bold {color}]"
                    )
                    if notify_trades:
                        self._notify(
                            (f"[kabu-trader {self.market_name}] {self._mode_label} SELL "
                             f"{name} ({ticker}) {action['pnl_pct']:+.1f}%"),
                            (f"📋 [{self.market_name}] {self._mode_label} SELL [{self._mode_tag}]\n"
                             f"{'🟢' if pnl > 0 else '🔴'} {name} ({ticker})\n"
                             f"💰 @ {sym}{price:,.2f}\n"
                             f"📊 P&L: {sym}{pnl:+,.2f} ({action['pnl_pct']:+.1f}%)"),
                        )

        # Daily snapshot
        self.paper_trader.take_daily_snapshot(price_dict, now_str)

    def _build_paper_panel(self, prices: list) -> Panel:
        """Build trading status panel (Paper or Live depending on broker wiring)."""
        if not self.paper_trader:
            return Panel("[dim]Trading disabled[/dim]", title="Trading", border_style="dim")

        price_dict = {p["ticker"]: p["price"] for p in prices if p.get("price")}
        summary = self.paper_trader.get_summary(price_dict)

        ret = summary["total_return_pct"]
        ret_color = "green" if ret > 0 else "red" if ret < 0 else "white"
        sym = self.currency_symbol

        lines = [
            f"Capital: {sym}{summary['initial_capital']:,.2f} → {sym}{summary['total_value']:,.2f} "
            f"[{ret_color}]({ret:+.2f}%)[/{ret_color}]",
            f"Cash: {sym}{summary['cash']:,.2f} | Positions: {summary['open_positions']}/{self.paper_trader.max_positions}",
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
                    f"{pos.shares} shares @ {sym}{pos.entry_price:,.2f} → {sym}{price:,.2f} "
                    f"({sym}{pnl:+,.2f} / {pnl_pct:+.1f}%)[/{color}]"
                )

        panel_title = f"{self._mode_label} Trading"
        panel_color = "bold red" if self._is_live() else "bold cyan"
        return Panel("\n".join(lines), title=panel_title, border_style=panel_color)

    def run_once(self):
        """Run a single monitoring cycle and print results."""
        self.console.print("\n[bold]Fetching data...[/bold]")
        self._check_broker_health()
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

        tz_label = str(self.tz).split("/")[-1] if "/" in str(self.tz) else str(self.tz)
        self.console.print(Panel(
            f"Monitoring {len(self.watchlist)} [{self.market_name}] stocks every {interval}s\n"
            f"Trading hours: {self.monitor_config['trading_hours_start']} - "
            f"{self.monitor_config['trading_hours_end']} {tz_label}\n"
            f"Press Ctrl+C to stop",
            title=f"Kabu Trader Monitor [{self.market_name}]",
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
                    self._refresh_earnings()
                    self._check_corporate_actions()
                    self._send_daily_summary()
                    self._auto_retrain()
                    now = datetime.now(self.tz).strftime("%H:%M:%S")
                    self.console.print(
                        f"[dim][{now}] Market closed. Watching for news...[/dim]"
                    )

                time.sleep(interval)
        except KeyboardInterrupt:
            self.console.print("\n[bold]Monitor stopped.[/bold]")
