"""Swing trading strategy engine with composite signal scoring."""

from __future__ import annotations

import pandas as pd
import numpy as np
from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple

from . import indicators


class Signal(Enum):
    STRONG_BUY = 2
    BUY = 1
    HOLD = 0
    SELL = -1
    STRONG_SELL = -2


@dataclass
class TradeSignal:
    """A trading signal with metadata."""
    ticker: str
    signal: Signal
    score: int  # composite score from all indicators
    price: float
    reasons: List[str]
    timestamp: pd.Timestamp

    @property
    def is_buy(self) -> bool:
        return self.signal in (Signal.BUY, Signal.STRONG_BUY)

    @property
    def is_sell(self) -> bool:
        return self.signal in (Signal.SELL, Signal.STRONG_SELL)


class SwingCompositeStrategy:
    """Composite swing trading strategy that combines multiple indicators.

    Scoring system:
        Each indicator contributes a score from -2 to +2.
        The composite score determines the final signal.

    Indicators used:
        1. SMA crossover (short vs long)
        2. RSI (oversold/overbought)
        3. MACD (crossover + histogram)
        4. Bollinger Bands (price relative to bands)
        5. Volume confirmation (volume spike on signal)
        6. Ichimoku Cloud (trend + support/resistance)
        7. Money Flow Index (volume-weighted buying pressure)
        8. ADX (trend strength filter)
        9. Relative Strength vs market benchmark (Nikkei 225, S&P 500, ...)
    """

    # Default weights based on ML feature importance analysis.
    # Higher weight = more influence on the final score.
    # Each indicator's raw score (-1.0 to +1.0) is multiplied by its weight.
    DEFAULT_WEIGHTS = {
        "sma": 1.5,
        "rsi": 1.5,
        "macd": 2.0,
        "bollinger": 1.0,
        "volume": 1.0,
        "ichimoku": 2.5,
        "mfi": 2.0,
        "adx": 2.0,
        "relative_strength": 2.5,
        "ml": 3.0,
        "sentiment": 2.5,
        "earnings": 2.0,
    }

    def __init__(self, params: dict, benchmark_name: str = "Nikkei"):
        self.params = params
        self.benchmark_df = None
        self.benchmark_name = benchmark_name
        self.ml_model = None
        self.sentiment_data: dict = {}  # ticker -> sentiment result
        self.earnings_data: dict = {}  # ticker -> earnings-gap dict

        # Merge user-configured weights with defaults
        user_weights = params.get("indicator_weights", {})
        self.weights = dict(self.DEFAULT_WEIGHTS)
        self.weights.update(user_weights)

    def set_benchmark_data(self, benchmark_df):
        """Set market benchmark data (Nikkei, S&P 500, ...) for relative strength."""
        self.benchmark_df = benchmark_df

    def set_ml_model(self, ml_model):
        """Set trained ML model for prediction scoring."""
        self.ml_model = ml_model

    def set_sentiment_data(self, sentiment_data: dict):
        """Set LLM sentiment analysis results. Dict of ticker -> sentiment result."""
        self.sentiment_data = sentiment_data or {}

    def set_earnings_data(self, earnings_data: dict):
        """Set earnings-day price-gap data. Dict of ticker -> {days_ago, gap_pct, date}."""
        self.earnings_data = earnings_data or {}

    def analyze(self, df: pd.DataFrame, ticker: str = "") -> List[TradeSignal]:
        """Analyze a DataFrame and generate signals for each row.

        Args:
            df: OHLCV DataFrame
            ticker: Stock ticker for labeling

        Returns:
            List of TradeSignal for rows that have actionable signals
        """
        df = indicators.compute_all(df, self.params, self.benchmark_df)
        df = self._add_ml_predictions(df)
        signals = []

        for i in range(1, len(df)):
            score, reasons = self._score_row(df, i, ticker)

            threshold = self.params.get("signal_threshold", 4)
            strong_threshold = self.params.get("strong_signal_threshold", 7)
            if abs(score) >= threshold:
                if score > 0:
                    sig = Signal.STRONG_BUY if score >= strong_threshold else Signal.BUY
                else:
                    sig = Signal.STRONG_SELL if score <= -strong_threshold else Signal.SELL

                signals.append(TradeSignal(
                    ticker=ticker,
                    signal=sig,
                    score=score,
                    price=df["Close"].iloc[i],
                    reasons=reasons,
                    timestamp=df.index[i],
                ))

        return signals

    def get_latest_signal(self, df: pd.DataFrame, ticker: str = "") -> TradeSignal:
        """Get signal for the most recent data point."""
        df = indicators.compute_all(df, self.params, self.benchmark_df)
        df = self._add_ml_predictions(df)
        score, reasons = self._score_row(df, len(df) - 1, ticker)

        threshold = self.params.get("signal_threshold", 4)
        strong_threshold = self.params.get("strong_signal_threshold", 7)
        if score >= strong_threshold:
            sig = Signal.STRONG_BUY
        elif score >= threshold:
            sig = Signal.BUY
        elif score <= -strong_threshold:
            sig = Signal.STRONG_SELL
        elif score <= -threshold:
            sig = Signal.SELL
        else:
            sig = Signal.HOLD

        return TradeSignal(
            ticker=ticker,
            signal=sig,
            score=score,
            price=df["Close"].iloc[-1],
            reasons=reasons,
            timestamp=df.index[-1],
        )

    def _score_row(self, df: pd.DataFrame, i: int, ticker: str = "") -> Tuple[int, List[str]]:
        """Compute composite score for a single row.

        Each indicator returns a raw score from -1.0 to +1.0.
        The raw score is multiplied by the indicator's weight.
        Final score is the sum of all weighted scores, rounded to int.

        Returns:
            Tuple of (score, list of reason strings)
        """
        weighted_score = 0.0
        reasons = []

        scorers = [
            ("sma", self._score_sma, (df, i)),
            ("rsi", self._score_rsi, (df, i)),
            ("macd", self._score_macd, (df, i)),
            ("bollinger", self._score_bollinger, (df, i)),
            ("volume", self._score_volume, (df, i)),
            ("ichimoku", self._score_ichimoku, (df, i)),
            ("mfi", self._score_mfi, (df, i)),
            ("adx", self._score_adx, (df, i)),
            ("relative_strength", self._score_relative_strength, (df, i)),
            ("ml", self._score_ml, (df, i)),
            ("sentiment", self._score_sentiment, (ticker,)),
            ("earnings", self._score_earnings_surprise, (ticker,)),
        ]

        for name, scorer, args in scorers:
            weight = self.weights.get(name, 0)
            if weight == 0:
                continue
            raw_score, reason = scorer(*args)
            if raw_score != 0 and reason:
                contribution = raw_score * weight
                weighted_score += contribution
                reasons.append(reason)

        return round(weighted_score), reasons

    def _score_sma(self, df: pd.DataFrame, i: int) -> Tuple[float, str]:
        sma_s = df["SMA_short"].iloc[i]
        sma_l = df["SMA_long"].iloc[i]
        sma_s_prev = df["SMA_short"].iloc[i - 1]
        sma_l_prev = df["SMA_long"].iloc[i - 1]

        if pd.isna(sma_s) or pd.isna(sma_l) or pd.isna(sma_s_prev) or pd.isna(sma_l_prev):
            return 0, ""

        if sma_s_prev <= sma_l_prev and sma_s > sma_l:
            return 1.0, f"SMA golden cross ({self.params['sma_short']}/{self.params['sma_long']})"
        if sma_s_prev >= sma_l_prev and sma_s < sma_l:
            return -1.0, f"SMA death cross ({self.params['sma_short']}/{self.params['sma_long']})"
        if sma_s > sma_l:
            return 0.5, "SMA short > long (uptrend)"
        if sma_s < sma_l:
            return -0.5, "SMA short < long (downtrend)"

        return 0, ""

    def _score_rsi(self, df: pd.DataFrame, i: int) -> Tuple[float, str]:
        rsi_val = df["RSI"].iloc[i]
        if pd.isna(rsi_val):
            return 0, ""

        oversold = self.params["rsi_oversold"]
        overbought = self.params["rsi_overbought"]

        if rsi_val < oversold:
            return 1.0, f"RSI oversold ({rsi_val:.1f})"
        if rsi_val < 40:
            return 0.5, f"RSI approaching oversold ({rsi_val:.1f})"
        if rsi_val > overbought:
            return -1.0, f"RSI overbought ({rsi_val:.1f})"
        if rsi_val > 60:
            return -0.5, f"RSI approaching overbought ({rsi_val:.1f})"

        return 0, ""

    def _score_macd(self, df: pd.DataFrame, i: int) -> Tuple[float, str]:
        macd_val = df["MACD"].iloc[i]
        sig_val = df["MACD_signal"].iloc[i]
        hist = df["MACD_hist"].iloc[i]
        hist_prev = df["MACD_hist"].iloc[i - 1]

        if pd.isna(macd_val) or pd.isna(sig_val) or pd.isna(hist_prev):
            return 0, ""

        macd_prev = df["MACD"].iloc[i - 1]
        sig_prev = df["MACD_signal"].iloc[i - 1]

        if macd_prev <= sig_prev and macd_val > sig_val:
            return 1.0, "MACD bullish crossover"
        if macd_prev >= sig_prev and macd_val < sig_val:
            return -1.0, "MACD bearish crossover"

        if hist > 0 and hist > hist_prev:
            return 0.5, "MACD histogram growing (bullish momentum)"
        if hist < 0 and hist < hist_prev:
            return -0.5, "MACD histogram declining (bearish momentum)"

        return 0, ""

    def _score_bollinger(self, df: pd.DataFrame, i: int) -> Tuple[float, str]:
        close = df["Close"].iloc[i]
        upper = df["BB_upper"].iloc[i]
        lower = df["BB_lower"].iloc[i]

        if pd.isna(upper) or pd.isna(lower):
            return 0, ""

        bb_width = upper - lower
        if bb_width == 0:
            return 0, ""

        position = (close - lower) / bb_width

        if position <= 0.0:
            return 1.0, "Price below lower Bollinger Band (bounce candidate)"
        if position <= 0.2:
            return 0.5, f"Price near lower Bollinger Band ({position:.2f})"
        if position >= 1.0:
            return -1.0, "Price above upper Bollinger Band (reversal candidate)"
        if position >= 0.8:
            return -0.5, f"Price near upper Bollinger Band ({position:.2f})"

        return 0, ""

    def _score_volume(self, df: pd.DataFrame, i: int) -> Tuple[float, str]:
        vol_ratio = df["Volume_ratio"].iloc[i]
        if pd.isna(vol_ratio):
            return 0, ""

        threshold = self.params["volume_spike_threshold"]
        close = df["Close"].iloc[i]
        prev_close = df["Close"].iloc[i - 1]

        if vol_ratio >= threshold:
            # Scale by how big the spike is (1.5x = 0.5, 3x+ = 1.0)
            intensity = min(1.0, (vol_ratio - 1) / 2)
            if close > prev_close:
                return intensity, f"Volume spike on up day ({vol_ratio:.1f}x avg)"
            else:
                return -intensity, f"Volume spike on down day ({vol_ratio:.1f}x avg)"

        return 0, ""

    def _score_ichimoku(self, df: pd.DataFrame, i: int) -> Tuple[float, str]:
        close = df["Close"].iloc[i]
        tenkan = df["Tenkan"].iloc[i]
        kijun = df["Kijun"].iloc[i]
        senkou_a = df["Senkou_A"].iloc[i]
        senkou_b = df["Senkou_B"].iloc[i]

        if pd.isna(tenkan) or pd.isna(kijun) or pd.isna(senkou_a) or pd.isna(senkou_b):
            return 0, ""

        cloud_top = max(senkou_a, senkou_b)
        cloud_bottom = min(senkou_a, senkou_b)

        score = 0.0
        reasons = []

        if close > cloud_top:
            score += 0.5
            reasons.append("above cloud")
        elif close < cloud_bottom:
            score -= 0.5
            reasons.append("below cloud")

        if i > 0:
            tenkan_prev = df["Tenkan"].iloc[i - 1]
            kijun_prev = df["Kijun"].iloc[i - 1]
            if not pd.isna(tenkan_prev) and not pd.isna(kijun_prev):
                if tenkan_prev <= kijun_prev and tenkan > kijun:
                    score += 0.5
                    reasons.append("TK cross bullish")
                elif tenkan_prev >= kijun_prev and tenkan < kijun:
                    score -= 0.5
                    reasons.append("TK cross bearish")

        if score == 0:
            return 0, ""
        direction = "bullish" if score > 0 else "bearish"
        return max(-1.0, min(1.0, score)), f"Ichimoku {direction} ({', '.join(reasons)})"

    def _score_mfi(self, df: pd.DataFrame, i: int) -> Tuple[float, str]:
        mfi_val = df["MFI"].iloc[i]
        if pd.isna(mfi_val):
            return 0, ""

        if mfi_val < 20:
            return 1.0, f"MFI oversold ({mfi_val:.0f}) — heavy selling exhaustion"
        if mfi_val < 30:
            return 0.5, f"MFI low ({mfi_val:.0f}) — selling pressure fading"
        if mfi_val > 80:
            return -1.0, f"MFI overbought ({mfi_val:.0f}) — buying exhaustion"
        if mfi_val > 70:
            return -0.5, f"MFI high ({mfi_val:.0f}) — buying pressure peaking"

        return 0, ""

    def _score_adx(self, df: pd.DataFrame, i: int) -> Tuple[float, str]:
        adx_val = df["ADX"].iloc[i]
        plus_di = df["Plus_DI"].iloc[i]
        minus_di = df["Minus_DI"].iloc[i]

        if pd.isna(adx_val) or pd.isna(plus_di) or pd.isna(minus_di):
            return 0, ""

        if adx_val < 20:
            return 0, f"ADX weak ({adx_val:.0f}) — no clear trend"

        # Scale by ADX strength (25 = 0.5, 40+ = 1.0)
        intensity = min(1.0, (adx_val - 20) / 20)
        if plus_di > minus_di:
            return intensity, f"ADX strong trend ({adx_val:.0f}) — confirmed uptrend"
        else:
            return -intensity, f"ADX strong trend ({adx_val:.0f}) — confirmed downtrend"

    def _score_relative_strength(self, df: pd.DataFrame, i: int) -> Tuple[float, str]:
        rs = df["RS_vs_Benchmark"].iloc[i]
        if pd.isna(rs):
            return 0, ""

        bench = self.benchmark_name
        if rs > 1.10:
            return 1.0, f"Outperforming {bench} by {(rs-1)*100:.0f}% — strong"
        if rs > 1.03:
            return 0.5, f"Outperforming {bench} by {(rs-1)*100:.0f}%"
        if rs < 0.90:
            return -1.0, f"Underperforming {bench} by {(1-rs)*100:.0f}% — weak"
        if rs < 0.97:
            return -0.5, f"Underperforming {bench} by {(1-rs)*100:.0f}%"

        return 0, ""

    def _add_ml_predictions(self, df: pd.DataFrame) -> pd.DataFrame:
        """Precompute ML predictions for all rows and store as a column."""
        df["ML_proba"] = np.nan

        if self.ml_model is None or not self.ml_model.is_trained:
            return df

        try:
            from .ml_features import engineer_features, get_feature_columns

            # We need to engineer features on the original OHLCV data
            # But df already has indicators added. Extract OHLCV and re-engineer.
            ohlcv = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            featured = engineer_features(ohlcv, self.params, self.benchmark_df)

            feature_cols = get_feature_columns()
            available = [c for c in feature_cols if c in featured.columns]
            if len(available) < len(feature_cols) * 0.8:
                return df

            X = featured[feature_cols].replace([np.inf, -np.inf], np.nan).fillna(0)
            valid_mask = X.notna().all(axis=1)
            if valid_mask.sum() == 0:
                return df

            probas = self.ml_model.predict_proba(X[valid_mask])
            df.loc[valid_mask[valid_mask].index, "ML_proba"] = probas
        except Exception:
            pass

        return df

    def _score_ml(self, df: pd.DataFrame, i: int) -> Tuple[float, str]:
        """Score based on ML model prediction. Returns -1.0 to +1.0."""
        if "ML_proba" not in df.columns:
            return 0, ""

        proba = df["ML_proba"].iloc[i]
        if pd.isna(proba):
            return 0, ""

        # Map probability to -1.0..+1.0
        # 0.5 = neutral, 0.75+ = strong buy, 0.25- = strong sell
        if proba >= 0.55:
            score = min(1.0, (proba - 0.5) * 4)  # 0.5→0, 0.75→1.0
            return score, f"ML model: {proba:.0%} probability of +3% in 5 days"
        if proba <= 0.45:
            score = max(-1.0, (proba - 0.5) * 4)  # 0.5→0, 0.25→-1.0
            return score, f"ML model: {proba:.0%} upside probability (bearish)"

        return 0, ""

    def _score_sentiment(self, ticker: str) -> Tuple[float, str]:
        """Score based on LLM news sentiment analysis. Returns -1.0 to +1.0."""
        if not ticker or not self.sentiment_data:
            return 0, ""

        sentiment = self.sentiment_data.get(ticker)
        if not sentiment:
            return 0, ""

        raw_score = sentiment.get("score", 0)  # -5 to +5
        confidence = sentiment.get("confidence", 0.5)
        reasoning = sentiment.get("reasoning", "")

        if confidence < 0.3 or raw_score == 0:
            return 0, ""

        # Scale -5..+5 to -1.0..+1.0, weighted by confidence
        score = (raw_score / 5.0) * confidence

        direction = "bullish" if score > 0 else "bearish"
        return max(-1.0, min(1.0, score)), f"News sentiment {direction} ({raw_score:+d}): {reasoning}"

    def _score_earnings_surprise(self, ticker: str) -> Tuple[float, str]:
        """Score based on earnings-day close-to-close gap (proxy for beat/miss).

        Effect decays with time: full within 3 days, 0.5x through 7d, 0.25x through 14d.
        Gap of ±5% maps to ±1.0 before decay.
        """
        data = self.earnings_data.get(ticker)
        if not data:
            return 0, ""

        days_ago = data.get("days_ago", 999)
        gap_pct = data.get("gap_pct", 0.0)

        if days_ago <= 3:
            decay = 1.0
        elif days_ago <= 7:
            decay = 0.5
        elif days_ago <= 14:
            decay = 0.25
        else:
            return 0, ""

        raw = max(-1.0, min(1.0, gap_pct / 5.0))
        score = raw * decay
        if abs(score) < 0.1:
            return 0, ""

        direction = "beat" if gap_pct > 0 else "miss"
        return score, f"Earnings {direction} ({gap_pct:+.1f}% gap, {days_ago}d ago)"
