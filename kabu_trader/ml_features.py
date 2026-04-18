"""Feature engineering for ML-based stock prediction.

Converts raw OHLCV data into ML-ready features that capture:
- Price momentum at multiple timeframes
- Volatility regime
- Volume patterns
- Technical indicator states
- Candlestick patterns
- Mean reversion signals
"""

from __future__ import annotations

import pandas as pd
import numpy as np
from typing import Tuple

from . import indicators


def engineer_features(df: pd.DataFrame, params: dict, nikkei_df: pd.DataFrame = None) -> pd.DataFrame:
    """Create ML features from OHLCV data.

    Args:
        df: OHLCV DataFrame
        params: Strategy parameters
        nikkei_df: Optional Nikkei 225 data for relative strength

    Returns:
        DataFrame with feature columns (NaN rows at start should be dropped)
    """
    # First compute all standard indicators
    df = indicators.compute_all(df, params, nikkei_df)

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]
    open_ = df["Open"]

    # === Price Returns at multiple timeframes ===
    for period in [1, 2, 3, 5, 10, 20]:
        df[f"return_{period}d"] = close.pct_change(period)

    # === Momentum features ===
    # Rate of change
    for period in [5, 10, 20]:
        df[f"roc_{period}d"] = (close - close.shift(period)) / close.shift(period)

    # Price relative to moving averages
    df["price_vs_sma5"] = close / df["SMA_short"] - 1
    df["price_vs_sma25"] = close / df["SMA_long"] - 1
    sma50 = indicators.sma(close, 50)
    df["price_vs_sma50"] = close / sma50 - 1

    # Distance from 20-day high/low
    df["dist_from_20d_high"] = close / high.rolling(20).max() - 1
    df["dist_from_20d_low"] = close / low.rolling(20).min() - 1

    # === Volatility features ===
    # Historical volatility at different windows
    for period in [5, 10, 20]:
        df[f"volatility_{period}d"] = close.pct_change().rolling(period).std()

    # ATR as percentage of price
    df["atr_pct"] = df["ATR"] / close

    # Bollinger Band width (volatility squeeze indicator)
    df["bb_width"] = (df["BB_upper"] - df["BB_lower"]) / df["BB_middle"]
    df["bb_position"] = (close - df["BB_lower"]) / (df["BB_upper"] - df["BB_lower"])

    # === Volume features ===
    df["volume_change_1d"] = volume.pct_change()
    df["volume_change_5d"] = volume.pct_change(5)
    # Volume ratio already computed as Volume_ratio

    # Price-volume divergence: price up but volume down = weak
    df["pv_divergence"] = df["return_1d"] * df["volume_change_1d"]

    # On Balance Volume trend
    obv = (np.sign(close.diff()) * volume).cumsum()
    df["obv_slope_10d"] = obv.rolling(10).apply(
        lambda x: np.polyfit(range(len(x)), x, 1)[0] if len(x) == 10 else np.nan,
        raw=True,
    )

    # === Candlestick features ===
    body = close - open_
    full_range = high - low
    df["body_ratio"] = body / full_range.replace(0, np.nan)
    df["upper_shadow"] = (high - pd.concat([close, open_], axis=1).max(axis=1)) / full_range.replace(0, np.nan)
    df["lower_shadow"] = (pd.concat([close, open_], axis=1).min(axis=1) - low) / full_range.replace(0, np.nan)

    # Consecutive up/down days
    direction = np.sign(close.diff())
    streak = direction.copy()
    for i in range(1, len(streak)):
        if direction.iloc[i] == direction.iloc[i - 1] and direction.iloc[i] != 0:
            streak.iloc[i] = streak.iloc[i - 1] + direction.iloc[i]
    df["streak"] = streak

    # === Technical indicator features (already computed, extract key values) ===
    # RSI
    df["rsi_value"] = df["RSI"]

    # MACD histogram momentum
    df["macd_hist_change"] = df["MACD_hist"].diff()

    # Ichimoku features
    df["ichi_cloud_thickness"] = (df["Senkou_A"] - df["Senkou_B"]) / close
    df["price_vs_kijun"] = close / df["Kijun"] - 1
    df["tenkan_vs_kijun"] = df["Tenkan"] / df["Kijun"] - 1

    # MFI value
    df["mfi_value"] = df["MFI"]

    # ADX values
    df["adx_value"] = df["ADX"]
    df["di_diff"] = df["Plus_DI"] - df["Minus_DI"]

    # Relative strength vs Nikkei
    df["rs_nikkei"] = df["RS_vs_Nikkei"]

    # === Mean reversion features ===
    # Z-score: how many std devs from 20-day mean
    df["zscore_20d"] = (close - indicators.sma(close, 20)) / close.rolling(20).std()

    # Gap (overnight move)
    df["gap_pct"] = (open_ - close.shift(1)) / close.shift(1)

    return df


def create_target(df: pd.DataFrame, forward_days: int = 5, threshold: float = 0.03) -> pd.Series:
    """Create binary classification target.

    Target = 1 if price goes up by more than threshold in the next forward_days.
    Target = 0 otherwise.

    Args:
        df: DataFrame with Close column
        forward_days: How many days ahead to look
        threshold: Minimum return to count as positive (e.g., 0.03 = 3%)

    Returns:
        Series with 0/1 labels
    """
    future_return = df["Close"].shift(-forward_days) / df["Close"] - 1
    return (future_return > threshold).astype(int)


def get_feature_columns() -> list:
    """Return the list of feature column names used by the model."""
    return [
        # Returns
        "return_1d", "return_2d", "return_3d", "return_5d", "return_10d", "return_20d",
        # Momentum
        "roc_5d", "roc_10d", "roc_20d",
        "price_vs_sma5", "price_vs_sma25", "price_vs_sma50",
        "dist_from_20d_high", "dist_from_20d_low",
        # Volatility
        "volatility_5d", "volatility_10d", "volatility_20d",
        "atr_pct", "bb_width", "bb_position",
        # Volume
        "Volume_ratio", "volume_change_1d", "volume_change_5d",
        "pv_divergence", "obv_slope_10d",
        # Candlestick
        "body_ratio", "upper_shadow", "lower_shadow", "streak",
        # Technical indicators
        "rsi_value", "macd_hist_change",
        "ichi_cloud_thickness", "price_vs_kijun", "tenkan_vs_kijun",
        "mfi_value", "adx_value", "di_diff",
        "rs_nikkei",
        # Mean reversion
        "zscore_20d", "gap_pct",
    ]


def prepare_dataset(
    data: dict,
    params: dict,
    nikkei_df: pd.DataFrame = None,
    forward_days: int = 5,
    threshold: float = 0.03,
) -> Tuple[pd.DataFrame, pd.Series]:
    """Prepare full training dataset from multiple stocks.

    Args:
        data: Dict of ticker -> OHLCV DataFrame
        params: Strategy parameters
        nikkei_df: Nikkei 225 data
        forward_days: Days ahead for target
        threshold: Return threshold for positive target

    Returns:
        Tuple of (features DataFrame, target Series)
    """
    feature_cols = get_feature_columns()
    all_features = []
    all_targets = []

    for ticker, df in data.items():
        if len(df) < 60:
            continue

        featured = engineer_features(df, params, nikkei_df)
        target = create_target(featured, forward_days, threshold)

        # Add ticker as a column for reference (not used as feature)
        featured["_ticker"] = ticker
        featured["_target"] = target

        all_features.append(featured)

    if not all_features:
        return pd.DataFrame(), pd.Series(dtype=float)

    combined = pd.concat(all_features, ignore_index=False)

    # Drop rows with NaN in features or target
    valid_mask = combined[feature_cols + ["_target"]].notna().all(axis=1)
    combined = combined[valid_mask]

    X = combined[feature_cols]
    y = combined["_target"]

    # Replace inf with NaN and drop
    X = X.replace([np.inf, -np.inf], np.nan)
    valid = X.notna().all(axis=1)
    X = X[valid]
    y = y[valid]

    return X, y
