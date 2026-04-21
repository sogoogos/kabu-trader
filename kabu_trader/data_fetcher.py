"""Data fetcher module for stock market data using yfinance."""

from __future__ import annotations

import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from . import rate_limit


class DataFetcher:
    """Fetches historical and current stock data for any yfinance-supported market."""

    def __init__(self, benchmark_ticker: str = "^N225"):
        self._cache: Dict[str, pd.DataFrame] = {}
        self.benchmark_ticker = benchmark_ticker

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
        if rate_limit.is_cooling_down():
            raise RuntimeError(
                f"yfinance rate-limit cooldown active ({rate_limit.seconds_remaining()}s remaining)"
            )

        end = datetime.now()
        start = end - timedelta(days=days)

        try:
            stock = yf.Ticker(ticker)
            df = stock.history(start=start, end=end, interval=interval)
        except Exception as e:
            rate_limit.detect_and_record(e)
            raise

        if df.empty:
            raise ValueError(f"No data returned for {ticker}")

        # Keep only OHLCV columns
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index)

        # Remove timezone info for consistency
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)

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
        if rate_limit.is_cooling_down():
            print(
                f"Warning: yfinance cooldown active — skipping batch fetch for "
                f"{len(tickers)} tickers ({rate_limit.seconds_remaining()}s left)"
            )
            return {}

        end = datetime.now()
        start = end - timedelta(days=days)

        try:
            data = yf.download(
                tickers=tickers,
                start=start,
                end=end,
                interval=interval,
                group_by="ticker",
                threads=False,       # single-threaded to avoid bursting Yahoo
                progress=False,
                auto_adjust=True,
            )
        except Exception as e:
            rate_limit.detect_and_record(e)
            print(f"Warning: batch fetch failed: {e}")
            return {}

        results: Dict[str, pd.DataFrame] = {}
        has_multiindex = isinstance(data.columns, pd.MultiIndex)
        for ticker in tickers:
            try:
                if has_multiindex:
                    # group_by='ticker' puts ticker at the outer level
                    if ticker in data.columns.get_level_values(0):
                        df = data[ticker]
                    elif ticker in data.columns.get_level_values(-1):
                        df = data.xs(ticker, axis=1, level=-1)
                    else:
                        continue
                else:
                    df = data
                df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
                df = df.dropna(how="all")
                if df.empty:
                    continue
                df.index = pd.to_datetime(df.index)
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                self._cache[ticker] = df
                results[ticker] = df
            except Exception:
                continue
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
