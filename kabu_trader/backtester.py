"""Backtesting engine for evaluating trading strategies against historical data."""

from __future__ import annotations

import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from .strategy import SwingCompositeStrategy, Signal


@dataclass
class Trade:
    """Record of a single trade."""
    ticker: str
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    shares: int = 0
    side: str = "long"
    exit_reason: str = ""

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.shares

    @property
    def pnl_pct(self) -> float:
        if self.exit_price is None or self.entry_price == 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price * 100

    @property
    def is_open(self) -> bool:
        return self.exit_date is None


@dataclass
class BacktestResult:
    """Results from a backtest run."""
    ticker: str
    initial_capital: float
    final_capital: float
    trades: List[Trade]
    equity_curve: pd.Series

    @property
    def total_return_pct(self) -> float:
        return (self.final_capital - self.initial_capital) / self.initial_capital * 100

    @property
    def total_trades(self) -> int:
        return len([t for t in self.trades if not t.is_open])

    @property
    def winning_trades(self) -> int:
        return len([t for t in self.trades if not t.is_open and t.pnl > 0])

    @property
    def losing_trades(self) -> int:
        return len([t for t in self.trades if not t.is_open and t.pnl < 0])

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades * 100

    @property
    def avg_win(self) -> float:
        wins = [t.pnl_pct for t in self.trades if not t.is_open and t.pnl > 0]
        return np.mean(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl_pct for t in self.trades if not t.is_open and t.pnl < 0]
        return np.mean(losses) if losses else 0.0

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self.trades if not t.is_open and t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if not t.is_open and t.pnl < 0))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def max_drawdown_pct(self) -> float:
        if self.equity_curve.empty:
            return 0.0
        peak = self.equity_curve.expanding().max()
        drawdown = (self.equity_curve - peak) / peak * 100
        return drawdown.min()

    @property
    def sharpe_ratio(self) -> float:
        if self.equity_curve.empty or len(self.equity_curve) < 2:
            return 0.0
        returns = self.equity_curve.pct_change().dropna()
        if returns.std() == 0:
            return 0.0
        # Annualized (252 trading days)
        return returns.mean() / returns.std() * np.sqrt(252)

    def summary(self) -> dict:
        return {
            "ticker": self.ticker,
            "initial_capital": f"¥{self.initial_capital:,.0f}",
            "final_capital": f"¥{self.final_capital:,.0f}",
            "total_return": f"{self.total_return_pct:+.2f}%",
            "total_trades": self.total_trades,
            "win_rate": f"{self.win_rate:.1f}%",
            "avg_win": f"{self.avg_win:+.2f}%",
            "avg_loss": f"{self.avg_loss:+.2f}%",
            "profit_factor": f"{self.profit_factor:.2f}",
            "max_drawdown": f"{self.max_drawdown_pct:.2f}%",
            "sharpe_ratio": f"{self.sharpe_ratio:.2f}",
        }


class Backtester:
    """Backtesting engine that simulates trades on historical data."""

    def __init__(self, config: dict):
        self.initial_capital = config["initial_capital"]
        self.commission_rate = config["commission_rate"]
        self.position_size_pct = config["position_size_pct"]
        self.max_positions = config["max_positions"]
        self.stop_loss_pct = config["stop_loss_pct"]
        self.take_profit_pct = config["take_profit_pct"]

    def run(
        self,
        df: pd.DataFrame,
        strategy: SwingCompositeStrategy,
        ticker: str = "",
    ) -> BacktestResult:
        """Run backtest on historical data.

        Args:
            df: OHLCV DataFrame
            strategy: Trading strategy to use
            ticker: Stock ticker for labeling

        Returns:
            BacktestResult with all metrics
        """
        from . import indicators

        df = indicators.compute_all(df, strategy.params, strategy.nikkei_df)

        capital = self.initial_capital
        open_trades: List[Trade] = []
        closed_trades: List[Trade] = []
        equity_values = []
        equity_dates = []

        for i in range(1, len(df)):
            row = df.iloc[i]
            prev_row = df.iloc[i - 1]
            date = df.index[i]
            close = row["Close"]
            high = row["High"]
            low = row["Low"]

            # Check stop loss and take profit for open trades
            trades_to_close = []
            for trade in open_trades:
                # Stop loss
                if low <= trade.entry_price * (1 - self.stop_loss_pct):
                    trade.exit_price = trade.entry_price * (1 - self.stop_loss_pct)
                    trade.exit_date = date
                    trade.exit_reason = "stop_loss"
                    trades_to_close.append(trade)
                # Take profit
                elif high >= trade.entry_price * (1 + self.take_profit_pct):
                    trade.exit_price = trade.entry_price * (1 + self.take_profit_pct)
                    trade.exit_date = date
                    trade.exit_reason = "take_profit"
                    trades_to_close.append(trade)

            for trade in trades_to_close:
                open_trades.remove(trade)
                proceeds = trade.exit_price * trade.shares
                commission = proceeds * self.commission_rate
                capital += proceeds - commission
                closed_trades.append(trade)

            # Generate signal
            score, reasons = strategy._score_row(df, i)

            # Buy signal
            threshold = strategy.params.get("signal_threshold", 3)
            if score >= threshold and len(open_trades) < self.max_positions:
                position_value = capital * self.position_size_pct
                if position_value > 0 and close > 0:
                    # Japanese stocks trade in units of 100
                    shares = max(100, int(position_value / close / 100) * 100)
                    cost = close * shares
                    commission = cost * self.commission_rate

                    if cost + commission <= capital:
                        capital -= cost + commission
                        trade = Trade(
                            ticker=ticker,
                            entry_date=date,
                            entry_price=close,
                            shares=shares,
                        )
                        open_trades.append(trade)

            # Sell signal - close open positions
            elif score <= -threshold and open_trades:
                for trade in open_trades[:]:
                    trade.exit_price = close
                    trade.exit_date = date
                    trade.exit_reason = "signal_sell"
                    proceeds = close * trade.shares
                    commission = proceeds * self.commission_rate
                    capital += proceeds - commission
                    closed_trades.append(trade)
                    open_trades.remove(trade)

            # Track equity
            open_value = sum(close * t.shares for t in open_trades)
            equity_values.append(capital + open_value)
            equity_dates.append(date)

        # Close any remaining open trades at last price
        last_close = df["Close"].iloc[-1]
        for trade in open_trades:
            trade.exit_price = last_close
            trade.exit_date = df.index[-1]
            trade.exit_reason = "end_of_backtest"
            proceeds = last_close * trade.shares
            commission = proceeds * self.commission_rate
            capital += proceeds - commission
            closed_trades.append(trade)

        all_trades = closed_trades
        equity_curve = pd.Series(equity_values, index=equity_dates)

        return BacktestResult(
            ticker=ticker,
            initial_capital=self.initial_capital,
            final_capital=capital,
            trades=all_trades,
            equity_curve=equity_curve,
        )

    def run_multiple(
        self,
        data: Dict[str, pd.DataFrame],
        strategy: SwingCompositeStrategy,
    ) -> Dict[str, BacktestResult]:
        """Run backtest across multiple tickers."""
        results = {}
        for ticker, df in data.items():
            try:
                results[ticker] = self.run(df, strategy, ticker)
            except Exception as e:
                print(f"Warning: Backtest failed for {ticker}: {e}")
        return results
