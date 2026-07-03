"""Data fetcher for stock market data via a pluggable provider (yfinance/IBKR).

Raw OHLCV retrieval is delegated to a `MarketDataProvider` (see market_data.py);
this class owns caching, current-price derivation, benchmark, and dead-ticker
suppression — all provider-agnostic. Default provider is yfinance, so behavior is
unchanged unless a provider is injected (e.g. via config `market_data_provider`).
"""

from __future__ import annotations

import pandas as pd
from typing import Dict, List, Optional

from .market_data import MarketDataProvider, YFinanceProvider


class DataFetcher:
    """Fetches historical and current stock data via a swappable data provider."""

    # Class-level: after FAILURE_THRESHOLD consecutive empty fetches for the
    # same ticker, suppress it from future batches for the rest of the process
    # lifetime. Stops log spam and saves API quota when the watchlist contains
    # delisted / renamed tickers. Resets on process restart.
    _dead_tickers: set = set()
    _failure_counts: Dict[str, int] = {}
    FAILURE_THRESHOLD = 3

    def __init__(
        self,
        benchmark_ticker: str = "^N225",
        provider: Optional[MarketDataProvider] = None,
    ):
        self._cache: Dict[str, pd.DataFrame] = {}
        self.benchmark_ticker = benchmark_ticker
        # Default preserves prior behavior exactly (yfinance).
        self._provider = provider or YFinanceProvider()

    def fetch_historical(
        self,
        ticker: str,
        days: int = 365,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """Fetch historical OHLCV data for a ticker.

        Args:
            ticker: Stock ticker (e.g., "7203.T" for Toyota)
            days: Number of days of history to fetch
            interval: Data interval ("1d", "1h", "5m", etc.)

        Returns:
            DataFrame with columns: Open, High, Low, Close, Volume
        """
        if self._provider.is_cooling_down():
            raise RuntimeError(
                f"{self._provider.name} rate-limit cooldown active "
                f"({self._provider.cooldown_seconds()}s remaining)"
            )
        df = self._provider.fetch_historical(ticker, days=days, interval=interval)
        self._cache[ticker] = df
        return df

    def fetch_multiple(
        self,
        tickers: List[str],
        days: int = 365,
        interval: str = "1d",
    ) -> Dict[str, pd.DataFrame]:
        """Fetch historical data for many tickers in a single batched yfinance request.

        Dramatically cheaper than per-ticker calls: for N tickers yfinance makes
        ~ceil(N/200) HTTP requests instead of N.
        """
        if not tickers:
            return {}
        if self._provider.is_cooling_down():
            print(
                f"Warning: {self._provider.name} cooldown active — skipping batch "
                f"fetch for {len(tickers)} tickers "
                f"({self._provider.cooldown_seconds()}s left)"
            )
            return {}

        live_tickers = [t for t in tickers if t not in self._dead_tickers]
        if not live_tickers:
            return {}

        # Provider raises on batch-level failure so we don't count those as
        # per-ticker failures (transient errors shouldn't mark tickers dead).
        try:
            results = self._provider.fetch_multiple(
                live_tickers, days=days, interval=interval
            )
        except Exception as e:
            print(f"Warning: batch fetch failed: {e}")
            return {}

        for ticker, df in results.items():
            self._cache[ticker] = df

        # Track per-ticker failures: ones we tried but got no data for.
        for ticker in live_tickers:
            if ticker in results:
                self._failure_counts.pop(ticker, None)
            else:
                n = self._failure_counts.get(ticker, 0) + 1
                self._failure_counts[ticker] = n
                if n >= self.FAILURE_THRESHOLD:
                    self._dead_tickers.add(ticker)
                    print(
                        f"Warning: {ticker} suppressed after {n} consecutive empty "
                        "fetches (likely delisted; restart to retry)"
                    )

        return results

    def fetch_current_price(self, ticker: str) -> dict:
        """Return the latest bar as a price dict.

        Uses cached history from fetch_multiple if available (zero network cost).
        Falls back to a per-ticker history fetch if cache is cold.
        """
        df = self._cache.get(ticker)
        if df is None or len(df) < 2:
            # Fallback: fetch a few days of history for this one ticker.
            try:
                df = self.fetch_historical(ticker, days=5)
            except Exception as e:
                raise RuntimeError(f"No cached data for {ticker}: {e}")
        if len(df) < 2:
            raise RuntimeError(f"Not enough data rows for {ticker}")

        last = df.iloc[-1]
        prev = df.iloc[-2]
        prev_close = float(prev["Close"])
        last_close = float(last["Close"])
        return {
            "ticker": ticker,
            "price": last_close,
            "previous_close": prev_close,
            "open": float(last["Open"]),
            "day_high": float(last["High"]),
            "day_low": float(last["Low"]),
            "volume": int(last["Volume"]),
            "change_pct": (last_close - prev_close) / prev_close * 100 if prev_close else 0,
        }

    def fetch_current_prices(self, tickers: List[str]) -> List[dict]:
        """Build per-ticker price dicts from the cache populated by fetch_multiple."""
        results = []
        for ticker in tickers:
            try:
                results.append(self.fetch_current_price(ticker))
            except Exception as e:
                # Don't log per-ticker — cache misses are common during cooldown
                continue
        return results

    def fetch_benchmark(self, days: int = 365) -> pd.DataFrame:
        """Fetch the market benchmark index for relative strength comparison."""
        try:
            return self.fetch_historical(self.benchmark_ticker, days=days)
        except Exception as e:
            print(f"Warning: Failed to fetch benchmark {self.benchmark_ticker}: {e}")
            return pd.DataFrame()

    def get_cached(self, ticker: str) -> Optional[pd.DataFrame]:
        """Return cached data if available."""
        return self._cache.get(ticker)
