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
        9. Relative Strength vs Nikkei 225 (outperformance)
    """

    def __init__(self, params: dict):
        self.params = params
        self.nikkei_df = None
        self.ml_model = None
        self.sentiment_data: dict = {}  # ticker -> sentiment result

    def set_nikkei_data(self, nikkei_df):
        """Set Nikkei 225 data for relative strength calculation."""
        self.nikkei_df = nikkei_df

    def set_ml_model(self, ml_model):
        """Set trained ML model for prediction scoring."""
        self.ml_model = ml_model

    def set_sentiment_data(self, sentiment_data: dict):
        """Set LLM sentiment analysis results. Dict of ticker -> sentiment result."""
        self.sentiment_data = sentiment_data or {}

    def analyze(self, df: pd.DataFrame, ticker: str = "") -> List[TradeSignal]:
        """Analyze a DataFrame and generate signals for each row.

        Args:
            df: OHLCV DataFrame
            ticker: Stock ticker for labeling

        Returns:
            List of TradeSignal for rows that have actionable signals
        """
        df = indicators.compute_all(df, self.params, self.nikkei_df)
        df = self._add_ml_predictions(df)
        signals = []

        for i in range(1, len(df)):
            score, reasons = self._score_row(df, i, ticker)

            if abs(score) >= self.params.get("signal_threshold", 3):
                if score > 0:
                    sig = Signal.STRONG_BUY if score >= 5 else Signal.BUY
                else:
                    sig = Signal.STRONG_SELL if score <= -5 else Signal.SELL

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
        df = indicators.compute_all(df, self.params, self.nikkei_df)
        df = self._add_ml_predictions(df)
        score, reasons = self._score_row(df, len(df) - 1, ticker)

        if score >= 5:
            sig = Signal.STRONG_BUY
        elif score >= self.params.get("signal_threshold", 3):
            sig = Signal.BUY
        elif score <= -5:
            sig = Signal.STRONG_SELL
        elif score <= -self.params.get("signal_threshold", 3):
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

        Returns:
            Tuple of (score, list of reason strings)
        """
        score = 0
        reasons = []

        # 1. SMA Crossover
        sma_score, sma_reason = self._score_sma(df, i)
        score += sma_score
        if sma_reason:
            reasons.append(sma_reason)

        # 2. RSI
        rsi_score, rsi_reason = self._score_rsi(df, i)
        score += rsi_score
        if rsi_reason:
            reasons.append(rsi_reason)

        # 3. MACD
        macd_score, macd_reason = self._score_macd(df, i)
        score += macd_score
        if macd_reason:
            reasons.append(macd_reason)

        # 4. Bollinger Bands
        bb_score, bb_reason = self._score_bollinger(df, i)
        score += bb_score
        if bb_reason:
            reasons.append(bb_reason)

        # 5. Volume confirmation
        vol_score, vol_reason = self._score_volume(df, i)
        score += vol_score
        if vol_reason:
            reasons.append(vol_reason)

        # 6. Ichimoku Cloud
        ichi_score, ichi_reason = self._score_ichimoku(df, i)
        score += ichi_score
        if ichi_reason:
            reasons.append(ichi_reason)

        # 7. Money Flow Index
        mfi_score, mfi_reason = self._score_mfi(df, i)
        score += mfi_score
        if mfi_reason:
            reasons.append(mfi_reason)

        # 8. ADX trend strength (multiplier, not additive)
        adx_score, adx_reason = self._score_adx(df, i)
        score += adx_score
        if adx_reason:
            reasons.append(adx_reason)

        # 9. Relative Strength vs Nikkei
        rs_score, rs_reason = self._score_relative_strength(df, i)
        score += rs_score
        if rs_reason:
            reasons.append(rs_reason)

        # 10. ML Model prediction (if available)
        ml_score, ml_reason = self._score_ml(df, i)
        score += ml_score
        if ml_reason:
            reasons.append(ml_reason)

        # 11. LLM Sentiment (if available, only for latest row)
        sent_score, sent_reason = self._score_sentiment(ticker)
        score += sent_score
        if sent_reason:
            reasons.append(sent_reason)

        return score, reasons

    def _score_sma(self, df: pd.DataFrame, i: int) -> Tuple[int, str]:
        sma_s = df["SMA_short"].iloc[i]
        sma_l = df["SMA_long"].iloc[i]
        sma_s_prev = df["SMA_short"].iloc[i - 1]
        sma_l_prev = df["SMA_long"].iloc[i - 1]

        if pd.isna(sma_s) or pd.isna(sma_l) or pd.isna(sma_s_prev) or pd.isna(sma_l_prev):
            return 0, ""

        # Golden cross
        if sma_s_prev <= sma_l_prev and sma_s > sma_l:
            return 2, f"SMA golden cross ({self.params['sma_short']}/{self.params['sma_long']})"
        # Death cross
        if sma_s_prev >= sma_l_prev and sma_s < sma_l:
            return -2, f"SMA death cross ({self.params['sma_short']}/{self.params['sma_long']})"
        # Trending up
        if sma_s > sma_l:
            return 1, "SMA short > long (uptrend)"
        # Trending down
        if sma_s < sma_l:
            return -1, "SMA short < long (downtrend)"

        return 0, ""

    def _score_rsi(self, df: pd.DataFrame, i: int) -> Tuple[int, str]:
        rsi_val = df["RSI"].iloc[i]
        if pd.isna(rsi_val):
            return 0, ""

        oversold = self.params["rsi_oversold"]
        overbought = self.params["rsi_overbought"]

        if rsi_val < oversold:
            return 2, f"RSI oversold ({rsi_val:.1f})"
        if rsi_val < 40:
            return 1, f"RSI approaching oversold ({rsi_val:.1f})"
        if rsi_val > overbought:
            return -2, f"RSI overbought ({rsi_val:.1f})"
        if rsi_val > 60:
            return -1, f"RSI approaching overbought ({rsi_val:.1f})"

        return 0, ""

    def _score_macd(self, df: pd.DataFrame, i: int) -> Tuple[int, str]:
        macd_val = df["MACD"].iloc[i]
        sig_val = df["MACD_signal"].iloc[i]
        hist = df["MACD_hist"].iloc[i]
        hist_prev = df["MACD_hist"].iloc[i - 1]

        if pd.isna(macd_val) or pd.isna(sig_val) or pd.isna(hist_prev):
            return 0, ""

        # MACD crossover
        macd_prev = df["MACD"].iloc[i - 1]
        sig_prev = df["MACD_signal"].iloc[i - 1]

        if macd_prev <= sig_prev and macd_val > sig_val:
            return 2, "MACD bullish crossover"
        if macd_prev >= sig_prev and macd_val < sig_val:
            return -2, "MACD bearish crossover"

        # Histogram momentum
        if hist > 0 and hist > hist_prev:
            return 1, "MACD histogram growing (bullish momentum)"
        if hist < 0 and hist < hist_prev:
            return -1, "MACD histogram declining (bearish momentum)"

        return 0, ""

    def _score_bollinger(self, df: pd.DataFrame, i: int) -> Tuple[int, str]:
        close = df["Close"].iloc[i]
        upper = df["BB_upper"].iloc[i]
        lower = df["BB_lower"].iloc[i]
        middle = df["BB_middle"].iloc[i]

        if pd.isna(upper) or pd.isna(lower):
            return 0, ""

        bb_width = upper - lower
        if bb_width == 0:
            return 0, ""

        # Position within bands (0 = lower, 1 = upper)
        position = (close - lower) / bb_width

        if position <= 0.0:
            return 2, f"Price below lower Bollinger Band (bounce candidate)"
        if position <= 0.2:
            return 1, f"Price near lower Bollinger Band ({position:.2f})"
        if position >= 1.0:
            return -2, f"Price above upper Bollinger Band (reversal candidate)"
        if position >= 0.8:
            return -1, f"Price near upper Bollinger Band ({position:.2f})"

        return 0, ""

    def _score_volume(self, df: pd.DataFrame, i: int) -> Tuple[int, str]:
        vol_ratio = df["Volume_ratio"].iloc[i]
        if pd.isna(vol_ratio):
            return 0, ""

        threshold = self.params["volume_spike_threshold"]
        close = df["Close"].iloc[i]
        prev_close = df["Close"].iloc[i - 1]

        if vol_ratio >= threshold:
            if close > prev_close:
                return 1, f"Volume spike on up day ({vol_ratio:.1f}x avg)"
            else:
                return -1, f"Volume spike on down day ({vol_ratio:.1f}x avg)"

        return 0, ""

    def _score_ichimoku(self, df: pd.DataFrame, i: int) -> Tuple[int, str]:
        close = df["Close"].iloc[i]
        tenkan = df["Tenkan"].iloc[i]
        kijun = df["Kijun"].iloc[i]
        senkou_a = df["Senkou_A"].iloc[i]
        senkou_b = df["Senkou_B"].iloc[i]

        if pd.isna(tenkan) or pd.isna(kijun) or pd.isna(senkou_a) or pd.isna(senkou_b):
            return 0, ""

        cloud_top = max(senkou_a, senkou_b)
        cloud_bottom = min(senkou_a, senkou_b)

        score = 0
        reasons = []

        # Price vs cloud
        if close > cloud_top:
            score += 1
            reasons.append("above cloud")
        elif close < cloud_bottom:
            score -= 1
            reasons.append("below cloud")

        # Tenkan/Kijun cross
        if i > 0:
            tenkan_prev = df["Tenkan"].iloc[i - 1]
            kijun_prev = df["Kijun"].iloc[i - 1]
            if not pd.isna(tenkan_prev) and not pd.isna(kijun_prev):
                if tenkan_prev <= kijun_prev and tenkan > kijun:
                    score += 1
                    reasons.append("TK cross bullish")
                elif tenkan_prev >= kijun_prev and tenkan < kijun:
                    score -= 1
                    reasons.append("TK cross bearish")

        if score == 0:
            return 0, ""
        direction = "bullish" if score > 0 else "bearish"
        return score, f"Ichimoku {direction} ({', '.join(reasons)})"

    def _score_mfi(self, df: pd.DataFrame, i: int) -> Tuple[int, str]:
        mfi_val = df["MFI"].iloc[i]
        if pd.isna(mfi_val):
            return 0, ""

        # MFI is like RSI but volume-weighted — more reliable
        if mfi_val < 20:
            return 2, f"MFI oversold ({mfi_val:.0f}) — heavy selling exhaustion"
        if mfi_val < 30:
            return 1, f"MFI low ({mfi_val:.0f}) — selling pressure fading"
        if mfi_val > 80:
            return -2, f"MFI overbought ({mfi_val:.0f}) — buying exhaustion"
        if mfi_val > 70:
            return -1, f"MFI high ({mfi_val:.0f}) — buying pressure peaking"

        return 0, ""

    def _score_adx(self, df: pd.DataFrame, i: int) -> Tuple[int, str]:
        adx_val = df["ADX"].iloc[i]
        plus_di = df["Plus_DI"].iloc[i]
        minus_di = df["Minus_DI"].iloc[i]

        if pd.isna(adx_val) or pd.isna(plus_di) or pd.isna(minus_di):
            return 0, ""

        # ADX < 20: no trend — signals are unreliable, penalize
        if adx_val < 20:
            return 0, f"ADX weak ({adx_val:.0f}) — no clear trend"

        # ADX >= 25: strong trend — boost signal in trend direction
        if adx_val >= 25:
            if plus_di > minus_di:
                return 1, f"ADX strong trend ({adx_val:.0f}) +DI>{minus_di:.0f} — confirmed uptrend"
            else:
                return -1, f"ADX strong trend ({adx_val:.0f}) -DI>{plus_di:.0f} — confirmed downtrend"

        return 0, ""

    def _score_relative_strength(self, df: pd.DataFrame, i: int) -> Tuple[int, str]:
        rs = df["RS_vs_Nikkei"].iloc[i]
        if pd.isna(rs):
            return 0, ""

        # RS > 1.05: stock outperforming Nikkei by 5%+
        if rs > 1.10:
            return 2, f"Outperforming Nikkei by {(rs-1)*100:.0f}% — strong relative strength"
        if rs > 1.03:
            return 1, f"Outperforming Nikkei by {(rs-1)*100:.0f}%"
        # RS < 0.95: stock underperforming Nikkei by 5%+
        if rs < 0.90:
            return -2, f"Underperforming Nikkei by {(1-rs)*100:.0f}% — weak relative strength"
        if rs < 0.97:
            return -1, f"Underperforming Nikkei by {(1-rs)*100:.0f}%"

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
            featured = engineer_features(ohlcv, self.params, self.nikkei_df)

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

    def _score_ml(self, df: pd.DataFrame, i: int) -> Tuple[int, str]:
        """Score based on ML model prediction."""
        if "ML_proba" not in df.columns:
            return 0, ""

        proba = df["ML_proba"].iloc[i]
        if pd.isna(proba):
            return 0, ""

        # Strong conviction thresholds
        if proba >= 0.75:
            return 3, f"ML model: {proba:.0%} probability of +3% in 5 days"
        if proba >= 0.60:
            return 2, f"ML model: {proba:.0%} probability of +3% in 5 days"
        if proba >= 0.55:
            return 1, f"ML model: {proba:.0%} upside probability"
        if proba <= 0.20:
            return -3, f"ML model: {proba:.0%} upside probability (bearish)"
        if proba <= 0.30:
            return -2, f"ML model: {proba:.0%} upside probability (bearish)"
        if proba <= 0.40:
            return -1, f"ML model: {proba:.0%} upside probability (slightly bearish)"

        return 0, ""

    def _score_sentiment(self, ticker: str) -> Tuple[int, str]:
        """Score based on LLM news sentiment analysis."""
        if not ticker or not self.sentiment_data:
            return 0, ""

        sentiment = self.sentiment_data.get(ticker)
        if not sentiment:
            return 0, ""

        raw_score = sentiment.get("score", 0)
        confidence = sentiment.get("confidence", 0.5)
        reasoning = sentiment.get("reasoning", "")

        # Scale the -5..+5 sentiment score to a trading score
        # Apply confidence as a weight — low confidence = smaller impact
        if confidence < 0.3:
            return 0, ""

        if raw_score >= 4:
            score = 3
        elif raw_score >= 2:
            score = 2
        elif raw_score >= 1:
            score = 1
        elif raw_score <= -4:
            score = -3
        elif raw_score <= -2:
            score = -2
        elif raw_score <= -1:
            score = -1
        else:
            return 0, ""

        direction = "bullish" if score > 0 else "bearish"
        return score, f"News sentiment {direction} ({raw_score:+d}): {reasoning}"
