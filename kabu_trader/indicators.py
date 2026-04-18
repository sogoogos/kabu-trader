"""Technical indicators for swing trading analysis."""

import pandas as pd
import numpy as np


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(window=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """MACD (Moving Average Convergence Divergence).

    Returns:
        Tuple of (macd_line, signal_line, histogram)
    """
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(
    series: pd.Series,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands.

    Returns:
        Tuple of (upper_band, middle_band, lower_band)
    """
    middle = sma(series, period)
    rolling_std = series.rolling(window=period).std()
    upper = middle + (rolling_std * std_dev)
    lower = middle - (rolling_std * std_dev)
    return upper, middle, lower


def volume_sma(volume: pd.Series, period: int = 20) -> pd.Series:
    """Volume Simple Moving Average."""
    return sma(volume, period)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range - measures volatility."""
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(window=period).mean()


def ichimoku(df: pd.DataFrame, tenkan: int = 9, kijun: int = 26, senkou_b: int = 52) -> pd.DataFrame:
    """Ichimoku Cloud (一目均衡表).

    Returns DataFrame with columns:
        Tenkan-sen (転換線): short-term trend
        Kijun-sen (基準線): medium-term trend
        Senkou_A (先行スパンA): leading span A
        Senkou_B (先行スパンB): leading span B
        Chikou (遅行スパン): lagging span
    """
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    # Tenkan-sen: (highest high + lowest low) / 2 over tenkan period
    tenkan_sen = (high.rolling(tenkan).max() + low.rolling(tenkan).min()) / 2

    # Kijun-sen: (highest high + lowest low) / 2 over kijun period
    kijun_sen = (high.rolling(kijun).max() + low.rolling(kijun).min()) / 2

    # Senkou Span A: (Tenkan + Kijun) / 2, shifted forward by kijun periods
    senkou_a = ((tenkan_sen + kijun_sen) / 2).shift(kijun)

    # Senkou Span B: (highest high + lowest low) / 2 over senkou_b period, shifted forward
    senkou_b_line = ((high.rolling(senkou_b).max() + low.rolling(senkou_b).min()) / 2).shift(kijun)

    # Chikou Span: close shifted back by kijun periods
    chikou = close.shift(-kijun)

    result = pd.DataFrame({
        "Tenkan": tenkan_sen,
        "Kijun": kijun_sen,
        "Senkou_A": senkou_a,
        "Senkou_B": senkou_b_line,
        "Chikou": chikou,
    }, index=df.index)

    return result


def mfi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Money Flow Index - volume-weighted RSI.

    Combines price and volume to measure buying/selling pressure.
    """
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
    raw_money_flow = typical_price * df["Volume"]

    delta = typical_price.diff()
    positive_flow = raw_money_flow.where(delta > 0, 0.0)
    negative_flow = raw_money_flow.where(delta < 0, 0.0)

    positive_sum = positive_flow.rolling(period).sum()
    negative_sum = negative_flow.rolling(period).sum()

    money_ratio = positive_sum / negative_sum.replace(0, np.nan)
    return 100 - (100 / (1 + money_ratio))


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Average Directional Index - measures trend strength.

    Returns DataFrame with columns: ADX, +DI, -DI
    """
    high = df["High"]
    low = df["Low"]
    close = df["Close"]

    # Directional movement
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    # True Range
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Smoothed with Wilder's method (EMA with alpha=1/period)
    atr_val = true_range.ewm(alpha=1/period, min_periods=period).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr_val)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, min_periods=period).mean() / atr_val)

    # DX and ADX
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1/period, min_periods=period).mean()

    return pd.DataFrame({
        "ADX": adx_val,
        "Plus_DI": plus_di,
        "Minus_DI": minus_di,
    }, index=df.index)


def relative_strength_vs_index(stock_close: pd.Series, index_close: pd.Series, period: int = 20) -> pd.Series:
    """Relative strength of a stock vs a benchmark index.

    Returns ratio of stock return to index return over the period.
    Values > 1 mean outperforming, < 1 mean underperforming.
    """
    stock_ret = stock_close / stock_close.shift(period)
    index_ret = index_close / index_close.shift(period)
    return stock_ret / index_ret


def compute_all(df: pd.DataFrame, params: dict, nikkei_df: pd.DataFrame = None) -> pd.DataFrame:
    """Compute all indicators and add them as columns to the DataFrame.

    Args:
        df: DataFrame with OHLCV columns
        params: Strategy parameters dict
        nikkei_df: Optional Nikkei 225 DataFrame for relative strength

    Returns:
        DataFrame with indicator columns added
    """
    df = df.copy()
    close = df["Close"]
    volume = df["Volume"]

    # Moving Averages
    df["SMA_short"] = sma(close, params["sma_short"])
    df["SMA_long"] = sma(close, params["sma_long"])
    df["EMA_short"] = ema(close, params["sma_short"])
    df["EMA_long"] = ema(close, params["sma_long"])

    # RSI
    df["RSI"] = rsi(close, params["rsi_period"])

    # MACD
    df["MACD"], df["MACD_signal"], df["MACD_hist"] = macd(
        close, params["macd_fast"], params["macd_slow"], params["macd_signal"]
    )

    # Bollinger Bands
    df["BB_upper"], df["BB_middle"], df["BB_lower"] = bollinger_bands(
        close, params["bb_period"], params["bb_std"]
    )

    # Volume
    df["Volume_SMA"] = volume_sma(volume, params["volume_sma_period"])
    df["Volume_ratio"] = volume / df["Volume_SMA"]

    # ATR
    df["ATR"] = atr(df)

    # Ichimoku Cloud
    ichi = ichimoku(
        df,
        tenkan=params.get("ichimoku_tenkan", 9),
        kijun=params.get("ichimoku_kijun", 26),
        senkou_b=params.get("ichimoku_senkou_b", 52),
    )
    df["Tenkan"] = ichi["Tenkan"]
    df["Kijun"] = ichi["Kijun"]
    df["Senkou_A"] = ichi["Senkou_A"]
    df["Senkou_B"] = ichi["Senkou_B"]
    df["Chikou"] = ichi["Chikou"]

    # Money Flow Index
    df["MFI"] = mfi(df, period=params.get("mfi_period", 14))

    # ADX
    adx_result = adx(df, period=params.get("adx_period", 14))
    df["ADX"] = adx_result["ADX"]
    df["Plus_DI"] = adx_result["Plus_DI"]
    df["Minus_DI"] = adx_result["Minus_DI"]

    # Relative Strength vs Nikkei 225
    if nikkei_df is not None and not nikkei_df.empty:
        # Align index dates
        nikkei_aligned = nikkei_df["Close"].reindex(df.index, method="ffill")
        rs_period = params.get("rs_period", 20)
        df["RS_vs_Nikkei"] = relative_strength_vs_index(close, nikkei_aligned, rs_period)
    else:
        df["RS_vs_Nikkei"] = np.nan

    return df
