"""Pluggable market-data providers (yfinance / IBKR), switchable via config.

`DataFetcher` (data_fetcher.py) keeps all higher-level logic — caching, current
price derivation, benchmark, dead-ticker suppression — and delegates only the two
raw fetch operations to a provider defined here:

    fetch_historical(ticker, days, interval) -> pd.DataFrame   # OHLCV, tz-naive
    fetch_multiple(tickers, days, interval)   -> {ticker: DataFrame}

Both return DataFrames with columns [Open, High, Low, Close, Volume] and a
tz-naive DatetimeIndex, so the two providers are interchangeable.

Select with the JSON config flag `market_data_provider: "yfinance" | "ibkr"`
(default "yfinance" — behavior identical to before). The IBKR provider talks to
api.ibkr.com via ibind and can fall back to yfinance per-ticker.

NOTE: the IBKR provider is UNVALIDATED until OAuth activation completes; response
parsing is marked `# VERIFY` where the Web API JSON shape must be confirmed.
"""

from __future__ import annotations

import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from . import rate_limit

_OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Keep OHLCV columns and strip timezone for cross-provider consistency."""
    df = df[_OHLCV].copy()
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


class MarketDataProvider:
    """Raw OHLCV source. Subclasses implement the two fetch methods."""

    name = "base"

    def is_cooling_down(self) -> bool:
        return False

    def cooldown_seconds(self) -> int:
        return 0

    def fetch_historical(self, ticker: str, days: int, interval: str) -> pd.DataFrame:
        raise NotImplementedError

    def fetch_multiple(
        self, tickers: List[str], days: int, interval: str
    ) -> Dict[str, pd.DataFrame]:
        raise NotImplementedError


class YFinanceProvider(MarketDataProvider):
    """Yahoo Finance via yfinance (the historical default)."""

    name = "yfinance"

    def is_cooling_down(self) -> bool:
        return rate_limit.is_cooling_down()

    def cooldown_seconds(self) -> int:
        return rate_limit.seconds_remaining()

    def fetch_historical(self, ticker: str, days: int, interval: str) -> pd.DataFrame:
        import yfinance as yf

        end = datetime.now()
        start = end - timedelta(days=days)
        try:
            df = yf.Ticker(ticker).history(start=start, end=end, interval=interval)
        except Exception as e:
            rate_limit.detect_and_record(e)
            raise
        if df.empty:
            raise ValueError(f"No data returned for {ticker}")
        return _normalize(df)

    def fetch_multiple(
        self, tickers: List[str], days: int, interval: str
    ) -> Dict[str, pd.DataFrame]:
        import yfinance as yf

        end = datetime.now()
        start = end - timedelta(days=days)
        # Raises on batch-level failure so DataFetcher can skip failure-counting.
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
            raise

        results: Dict[str, pd.DataFrame] = {}
        has_multiindex = isinstance(data.columns, pd.MultiIndex)
        for ticker in tickers:
            try:
                if has_multiindex:
                    if ticker in data.columns.get_level_values(0):
                        df = data[ticker]
                    elif ticker in data.columns.get_level_values(-1):
                        df = data.xs(ticker, axis=1, level=-1)
                    else:
                        continue
                else:
                    df = data
                df = _normalize(df).dropna(how="all")
                if df.empty:
                    continue
                results[ticker] = df
            except Exception:
                continue
        return results


class IBKRProvider(MarketDataProvider):
    """IBKR Client Portal Web API via ibind (historical bars + snapshots).

    Requires OAuth env vars (see docs/IBKR_OAUTH_SETUP.md). Falls back to
    `fallback` (typically YFinanceProvider) per-ticker on any error, so a missing
    market-data permission degrades gracefully instead of dropping the ticker.
    """

    name = "ibkr"

    # yfinance interval -> IBKR bar string.
    _BAR = {
        "1m": "1min", "2m": "2min", "5m": "5min", "15m": "15min", "30m": "30min",
        "1h": "1h", "1d": "1d", "1wk": "1w", "1mo": "1m",
    }

    def __init__(self, client=None, fallback: Optional[MarketDataProvider] = None):
        # client: an authenticated ibind IbkrClient (shared with the broker is
        # ideal). Created lazily if not supplied.
        self._client = client
        self._fallback = fallback

    def _get_client(self):
        if self._client is None:
            from ibind import IbkrClient
            self._client = IbkrClient(use_oauth=True)
        return self._client

    def _bar(self, interval: str) -> str:
        return self._BAR.get(interval, "1d")

    @staticmethod
    def _to_query(ticker: str):
        from ibind import StockQuery
        if ticker.endswith(".T"):
            return StockQuery(ticker[:-2], contract_conditions={"currency": "JPY"})  # VERIFY
        return StockQuery(ticker, contract_conditions={"currency": "USD"})

    @staticmethod
    def _bars_to_df(payload) -> pd.DataFrame:
        """Convert an IBKR history payload to a normalized OHLCV DataFrame."""
        data = payload.data if hasattr(payload, "data") else payload
        bars = data.get("data") if isinstance(data, dict) else data  # VERIFY key
        if not bars:
            raise ValueError("No bars returned")
        rows = []
        idx = []
        for b in bars:
            # IBKR bar keys: t (epoch ms), o/h/l/c/v.  # VERIFY
            idx.append(pd.to_datetime(b["t"], unit="ms"))
            rows.append({
                "Open": float(b["o"]), "High": float(b["h"]),
                "Low": float(b["l"]), "Close": float(b["c"]),
                "Volume": float(b.get("v") or 0),
            })
        df = pd.DataFrame(rows, index=pd.DatetimeIndex(idx))
        return _normalize(df)

    def fetch_historical(self, ticker: str, days: int, interval: str) -> pd.DataFrame:
        try:
            client = self._get_client()
            res = client.marketdata_history_by_symbol(
                self._to_query(ticker), bar=self._bar(interval), period=f"{days}d",
            )
            return self._bars_to_df(res)
        except Exception:
            if self._fallback is not None:
                return self._fallback.fetch_historical(ticker, days, interval)
            raise

    def fetch_multiple(
        self, tickers: List[str], days: int, interval: str
    ) -> Dict[str, pd.DataFrame]:
        results: Dict[str, pd.DataFrame] = {}
        for t in tickers:
            try:
                results[t] = self.fetch_historical(t, days, interval)
            except Exception:
                continue  # fallback already tried inside fetch_historical
        return results


def build_provider(
    name: str = "yfinance",
    *,
    ibkr_client=None,
    ibkr_fallback: bool = True,
) -> MarketDataProvider:
    """Factory: map a config flag to a provider instance.

    name="yfinance" -> YFinanceProvider
    name="ibkr"     -> IBKRProvider (with YFinance fallback unless ibkr_fallback=False)
    """
    key = (name or "yfinance").strip().lower()
    if key in ("ibkr", "ib", "webapi"):
        fallback = YFinanceProvider() if ibkr_fallback else None
        return IBKRProvider(client=ibkr_client, fallback=fallback)
    if key in ("yfinance", "yahoo", "yf"):
        return YFinanceProvider()
    raise ValueError(f"Unknown market_data_provider: {name!r}")
