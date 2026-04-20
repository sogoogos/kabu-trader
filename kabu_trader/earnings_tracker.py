"""Earnings-day price-gap proxy.

We don't have reliable free consensus-estimate data, so we proxy "earnings surprise"
with the market's own reaction: the close-to-close gap around the reporting date.
A large positive gap after reporting ≈ beat; large negative ≈ miss.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import pandas as pd
import yfinance as yf

from . import rate_limit


class EarningsTracker:
    """Fetches the most recent earnings date per ticker and computes a price-gap proxy."""

    # Long TTL for stale tickers (no recent earnings): we already know the answer
    # won't change for weeks. Short TTL only for tickers currently in the decay window.
    def __init__(self, decay_days: int = 14, cache_ttl: int = 24 * 3600, stale_ttl: int = 7 * 24 * 3600):
        self.decay_days = decay_days
        self._cache: Dict[str, Optional[dict]] = {}
        self._cache_time: Dict[str, float] = {}
        self._cache_ttl = cache_ttl
        self._stale_ttl = stale_ttl

    def _ttl_for(self, data: Optional[dict]) -> int:
        """Use long TTL for tickers whose last earnings is >decay days old — already decayed to 0."""
        if data and data.get("days_ago", 0) > self.decay_days:
            return self._stale_ttl
        return self._cache_ttl

    def get(self, ticker: str) -> Optional[dict]:
        """Return cached earnings-gap dict for a ticker, or fetch if stale."""
        now = time.time()
        if ticker in self._cache:
            age = now - self._cache_time.get(ticker, 0)
            if age < self._ttl_for(self._cache[ticker]):
                return self._cache[ticker]
        data = self._fetch(ticker)
        self._cache[ticker] = data
        self._cache_time[ticker] = now
        return data

    def refresh_all(self, tickers: list, throttle: float = 2.0) -> Dict[str, dict]:
        """Fetch earnings data for all tickers. Returns only tickers with a recent event.

        Skips cached-stale tickers entirely (no yfinance call) — the vast majority of
        tickers aren't near their next earnings, and re-checking them costs rate-limit
        budget without adding any signal.
        """
        out: Dict[str, dict] = {}
        now = time.time()
        for t in tickers:
            cached = self._cache.get(t)
            age = now - self._cache_time.get(t, 0)
            if cached is not None and age < self._ttl_for(cached):
                if cached.get("days_ago", 999) <= self.decay_days:
                    out[t] = cached
                continue
            if rate_limit.is_cooling_down():
                # Stop hitting Yahoo; resume next refresh cycle.
                break
            data = self.get(t)
            if data and data.get("days_ago", 999) <= self.decay_days:
                out[t] = data
            time.sleep(throttle)
        return out

    def _fetch(self, ticker: str) -> Optional[dict]:
        if rate_limit.is_cooling_down():
            return None
        try:
            stock = yf.Ticker(ticker)
            ed = stock.earnings_dates
            if ed is None or ed.empty:
                return None
        except Exception as e:
            rate_limit.detect_and_record(e)
            return None

        tz = ed.index.tz
        now = pd.Timestamp.now(tz=tz) if tz else pd.Timestamp.now()
        past = ed.index[ed.index < now]
        if len(past) == 0:
            return None

        last = past.max()
        date_only = last.date()
        today = datetime.now(tz=tz).date() if tz else datetime.now().date()
        days_ago = (today - date_only).days

        if days_ago > self.decay_days:
            return {"date": date_only.isoformat(), "days_ago": days_ago, "gap_pct": 0.0}

        gap_pct = self._compute_gap(ticker, date_only)
        if gap_pct is None:
            return None

        return {
            "date": date_only.isoformat(),
            "days_ago": days_ago,
            "gap_pct": gap_pct,
        }

    def _compute_gap(self, ticker: str, earnings_date) -> Optional[float]:
        """Close-to-close gap: last trading close before earnings date vs. first after."""
        if rate_limit.is_cooling_down():
            return None
        try:
            start = earnings_date - timedelta(days=5)
            end = earnings_date + timedelta(days=5)
            hist = yf.Ticker(ticker).history(start=start, end=end)
        except Exception as e:
            rate_limit.detect_and_record(e)
            return None
        if hist is None or hist.empty or len(hist) < 2:
            return None

        dates = [d.date() for d in hist.index]
        before = [i for i, d in enumerate(dates) if d < earnings_date]
        after = [i for i, d in enumerate(dates) if d > earnings_date]
        if not before or not after:
            return None

        close_before = float(hist["Close"].iloc[before[-1]])
        close_after = float(hist["Close"].iloc[after[0]])
        if close_before <= 0:
            return None
        return (close_after - close_before) / close_before * 100.0
