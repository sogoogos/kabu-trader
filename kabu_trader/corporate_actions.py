"""Detect stock splits and dividends for held positions.

We use yfinance's `auto_adjust=True` for historical OHLCV, which keeps technical
indicators clean across corporate actions. But paper-trader state (entry_price,
shares) is stored at purchase time and gets stale when splits/dividends happen
after we've opened the position. Without correction:
- A 2:1 split makes a long-held position look like it crashed 50%, triggering
  the stop-loss artificially.
- An ex-dividend price drop also looks like a small loss.

This module fetches `.splits` and `.dividends` from yfinance for held tickers
(weekly cadence, ~10 calls / week — trivial rate-limit footprint) and returns
the events that occurred since each position's entry date. The PaperTrader
then applies the adjustments to its state.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, Optional

import pandas as pd
import yfinance as yf

from . import rate_limit


class CorporateActionsTracker:
    """Fetches stock splits and dividends from yfinance with a 7-day cache."""

    def __init__(self, cache_ttl: int = 7 * 24 * 3600):
        self._cache: Dict[str, dict] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_ttl = cache_ttl

    def get_actions_since(self, ticker: str, since_date: str) -> Optional[dict]:
        """Return any splits and dividends for ticker after `since_date`.

        Returns:
            {"splits":    [{"date": "YYYY-MM-DD", "ratio": float}, ...],
             "dividends": [{"date": "YYYY-MM-DD", "amount": float}, ...]}
            or None if rate-limited / fetch failed.
        """
        if rate_limit.is_cooling_down():
            return None

        now = time.time()
        cached = self._cache.get(ticker)
        cache_age = now - self._cache_time.get(ticker, 0)
        if cached is None or cache_age >= self._cache_ttl:
            try:
                stock = yf.Ticker(ticker)
                splits = stock.splits
                dividends = stock.dividends
            except Exception as e:
                rate_limit.detect_and_record(e)
                return None
            cached = {"splits": splits, "dividends": dividends}
            self._cache[ticker] = cached
            self._cache_time[ticker] = now

        since_dt = self._parse_date(since_date)
        if since_dt is None:
            return None

        return {
            "splits": _events_after(cached["splits"], since_dt, "ratio"),
            "dividends": _events_after(cached["dividends"], since_dt, "amount"),
        }

    @staticmethod
    def _parse_date(s: str) -> Optional[datetime]:
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(s.split("+")[0].strip(), fmt)
            except ValueError:
                continue
        return None


def _events_after(series: pd.Series, since_dt: datetime, value_key: str) -> list:
    if series is None or len(series) == 0:
        return []
    out = []
    for ts, value in series.items():
        d = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        d_naive = d.replace(tzinfo=None) if hasattr(d, "tzinfo") and d.tzinfo else d
        if d_naive > since_dt:
            out.append({"date": d_naive.strftime("%Y-%m-%d"), value_key: float(value)})
    return out
