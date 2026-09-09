"""Interest-rate inputs: JGB yields (MOF) and the US 10-year (yfinance ^TNX).

Two sources, both free and daily:

- JGB 2y / 10y from the Ministry of Finance CSVs. `jgbcm.csv` holds the
  current month (updated each business day after the close), and
  `data/jgbcm_all.csv` holds the full history since 1974 (updated monthly).
  Together they give an uninterrupted daily series. Dates are in Japanese era
  notation ("R8.9.1" = Reiwa 8 = 2026-09-01), so they need converting.
- US 10y from Yahoo (^TNX, quoted in percent), fetched through the shared
  DataFetcher so it respects the yfinance rate-limit breaker.

The MOF close is published after the TSE session, so before the open the
latest row is the previous business day — the same lead/lag as the regime
filter's US/VIX inputs.

Fail-open by design: every source is fetched independently and a failure just
leaves that key absent. Consumers must treat a missing key as "no opinion".
"""

from __future__ import annotations

import io
import re
import time
import urllib.request
from pathlib import Path
from typing import Optional

import pandas as pd

MOF_CURRENT_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
MOF_HISTORY_URL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"

# First year of each era minus one, so "R8" -> 2018 + 8 = 2026.
_ERA_BASE = {"M": 1867, "T": 1911, "S": 1925, "H": 1988, "R": 2018}
_ERA_RE = re.compile(r"^([MTSHR])(\d+)\.(\d+)\.(\d+)$")

# Column positions in the MOF CSV: 0=date, 1=1y, 2=2y, ..., 10=10y, ...
_TENOR_COLS = {"jgb2y": 2, "jgb10y": 10}


def _parse_era_date(value) -> Optional[pd.Timestamp]:
    m = _ERA_RE.match(str(value).strip())
    if not m:
        return None
    era, y, mo, d = m.groups()
    try:
        return pd.Timestamp(_ERA_BASE[era] + int(y), int(mo), int(d))
    except ValueError:
        return None


def parse_mof_csv(raw_text: str) -> pd.DataFrame:
    """Parse a MOF yield CSV (already decoded) into a DataFrame indexed by date.

    Returns columns `jgb2y` and `jgb10y` as floats (percent). Rows whose date
    doesn't parse (the title line, the footnote) are dropped; "-" yields
    become NaN.
    """
    df = pd.read_csv(io.StringIO(raw_text), skiprows=1, header=0, dtype=str)
    if df.shape[1] <= max(_TENOR_COLS.values()):
        return pd.DataFrame(columns=list(_TENOR_COLS))
    out = pd.DataFrame({
        name: pd.to_numeric(df.iloc[:, col], errors="coerce")
        for name, col in _TENOR_COLS.items()
    })
    out.index = df.iloc[:, 0].map(_parse_era_date)
    out = out[out.index.notna()]
    out.index = pd.DatetimeIndex(out.index)
    return out.sort_index()


def _http_get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("cp932", errors="replace")


def _metrics(series: pd.Series) -> Optional[dict]:
    """Level plus 5- and 20-row changes in basis points for a daily yield series."""
    s = series.dropna()
    if s.empty:
        return None
    last = float(s.iloc[-1])

    def _chg(n):
        if len(s) <= n:
            return None
        return round((last - float(s.iloc[-1 - n])) * 100.0, 1)

    return {
        "level": last,
        "date": s.index[-1].strftime("%Y-%m-%d"),
        "chg_5d_bp": _chg(5),
        "chg_20d_bp": _chg(20),
    }


class RatesTracker:
    """Fetches and caches the rate inputs once a day.

    `refresh()` returns (and stores in `.data`) a dict keyed by
    `jgb2y` / `jgb10y` / `us10y`, each a dict of
    {level, date, chg_5d_bp, chg_20d_bp}. Keys are absent when that source
    failed. The MOF history file (~1 MB) is cached on disk at `cache_path`
    when given and refetched only after `history_ttl` seconds.
    """

    def __init__(
        self,
        fetcher=None,
        us_ticker: str = "^TNX",
        cache_path: Optional[Path] = None,
        history_ttl: int = 7 * 24 * 3600,
        refresh_interval: int = 24 * 3600,
    ):
        self.fetcher = fetcher
        self.us_ticker = us_ticker
        self.cache_path = Path(cache_path) if cache_path else None
        self.history_ttl = history_ttl
        self.refresh_interval = refresh_interval
        self.data: dict = {}
        self.jgb: Optional[pd.DataFrame] = None
        self._last_refresh: float = 0.0
        self.errors: list[str] = []

    # -- JGB ----------------------------------------------------------------

    def _load_history_text(self) -> str:
        if self.cache_path and self.cache_path.exists():
            age = time.time() - self.cache_path.stat().st_mtime
            if age < self.history_ttl:
                return self.cache_path.read_text(encoding="utf-8")
        text = _http_get(MOF_HISTORY_URL)
        if self.cache_path:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(text, encoding="utf-8")
            except OSError:
                pass
        return text

    def fetch_jgb(self) -> pd.DataFrame:
        """Full daily JGB series: cached history plus the live current month."""
        frames = []
        try:
            frames.append(parse_mof_csv(self._load_history_text()))
        except Exception as e:  # noqa: BLE001 - fail-open by design
            self.errors.append(f"MOF history: {type(e).__name__}: {e}")
        try:
            frames.append(parse_mof_csv(_http_get(MOF_CURRENT_URL)))
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"MOF current: {type(e).__name__}: {e}")
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame(columns=list(_TENOR_COLS))
        df = pd.concat(frames)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df

    # -- US -----------------------------------------------------------------

    def fetch_us10y(self) -> Optional[pd.Series]:
        if self.fetcher is None or not self.us_ticker:
            return None
        df = self.fetcher.fetch_historical(self.us_ticker, days=60)
        if df is None or df.empty or "Close" not in df:
            return None
        return df["Close"].dropna()

    # -- public ---------------------------------------------------------------

    def refresh(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self.data and now - self._last_refresh < self.refresh_interval:
            return self.data
        self.errors = []
        data: dict = {}

        jgb = self.fetch_jgb()
        if not jgb.empty:
            self.jgb = jgb
            for name in _TENOR_COLS:
                m = _metrics(jgb[name])
                if m:
                    data[name] = m

        try:
            us = self.fetch_us10y()
            if us is not None and len(us):
                m = _metrics(us)
                if m:
                    data["us10y"] = m
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{self.us_ticker}: {type(e).__name__}: {e}")

        self.data = data
        self._last_refresh = now
        return data

    @staticmethod
    def describe(data: dict) -> str:
        """One-line summary, e.g. 'JGB 2y 1.85% / 10y 2.90% (+5bp/20d) | US 10y 4.81% (+12bp/20d)'."""
        if not data:
            return ""

        def _fmt(m, label):
            chg = m.get("chg_20d_bp")
            chg_s = f" ({chg:+.0f}bp/20d)" if chg is not None else ""
            return f"{label} {m['level']:.2f}%{chg_s}"

        parts = []
        jp = []
        if "jgb2y" in data:
            jp.append(f"2y {data['jgb2y']['level']:.2f}%")
        if "jgb10y" in data:
            jp.append(_fmt(data["jgb10y"], "10y"))
        if jp:
            parts.append("JGB " + " / ".join(jp))
        if "us10y" in data:
            parts.append(_fmt(data["us10y"], "US 10y"))
        return " | ".join(parts)
