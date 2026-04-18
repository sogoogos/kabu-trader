"""Data fetcher module for Japanese stock market data using yfinance."""

from __future__ import annotations

import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional


class DataFetcher:
    """Fetches historical and current stock data for Japanese equities."""

    def __init__(self):
        self._cache: Dict[str, pd.DataFrame] = {}

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
        end = datetime.now()
        start = end - timedelta(days=days)

        stock = yf.Ticker(ticker)
        df = stock.history(start=start, end=end, interval=interval)

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
        """Fetch historical data for multiple tickers."""
        results = {}
        for ticker in tickers:
            try:
                results[ticker] = self.fetch_historical(ticker, days, interval)
            except Exception as e:
                print(f"Warning: Failed to fetch {ticker}: {e}")
        return results

    def fetch_current_price(self, ticker: str) -> dict:
        """Fetch current/latest price info for a ticker."""
        stock = yf.Ticker(ticker)
        info = stock.fast_info

        return {
            "ticker": ticker,
            "price": info.last_price,
            "previous_close": info.previous_close,
            "open": info.open,
            "day_high": info.day_high,
            "day_low": info.day_low,
            "volume": info.last_volume,
            "change_pct": ((info.last_price - info.previous_close) / info.previous_close * 100)
            if info.previous_close
            else 0,
        }

    def fetch_current_prices(self, tickers: List[str]) -> List[dict]:
        """Fetch current prices for multiple tickers."""
        results = []
        for ticker in tickers:
            try:
                results.append(self.fetch_current_price(ticker))
            except Exception as e:
                print(f"Warning: Failed to fetch current price for {ticker}: {e}")
        return results

    def fetch_nikkei225(self, days: int = 365) -> pd.DataFrame:
        """Fetch Nikkei 225 index data for relative strength comparison."""
        try:
            return self.fetch_historical("^N225", days=days)
        except Exception as e:
            print(f"Warning: Failed to fetch Nikkei 225: {e}")
            return pd.DataFrame()

    def get_cached(self, ticker: str) -> Optional[pd.DataFrame]:
        """Return cached data if available."""
        return self._cache.get(ticker)
