"""Paper trading engine for forward-testing strategies with simulated money.

Persists all state to disk so it survives restarts. Tracks:
- Virtual cash balance
- Open and closed positions
- Every trade with entry/exit prices and P&L
- Daily portfolio snapshots
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "paper_trading"


class Position:
    """An open position."""

    def __init__(self, ticker: str, name: str, entry_price: float, shares: int,
                 entry_date: str, signal_score: int, reasons: List[str]):
        self.ticker = ticker
        self.name = name
        self.entry_price = entry_price
        self.shares = shares
        self.entry_date = entry_date
        self.signal_score = signal_score
        self.reasons = reasons

    def current_value(self, price: float) -> float:
        return price * self.shares

    def pnl(self, price: float) -> float:
        return (price - self.entry_price) * self.shares

    def pnl_pct(self, price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        return (price - self.entry_price) / self.entry_price * 100

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "entry_price": self.entry_price,
            "shares": self.shares,
            "entry_date": self.entry_date,
            "signal_score": self.signal_score,
            "reasons": self.reasons,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            ticker=d["ticker"], name=d["name"],
            entry_price=d["entry_price"], shares=d["shares"],
            entry_date=d["entry_date"], signal_score=d["signal_score"],
            reasons=d.get("reasons", []),
        )


class PaperTrader:
    """Simulates trading with virtual money. Persists state to disk."""

    def __init__(self, config: dict, state_dir: Optional[Path] = None):
        self.initial_capital = config.get("initial_capital", 1000000)
        self.commission_rate = config.get("commission_rate", 0.001)
        self.position_size_pct = config.get("position_size_pct", 0.1)
        self.max_positions = config.get("max_positions", 5)
        self.stop_loss_pct = config.get("stop_loss_pct", 0.05)
        self.take_profit_pct = config.get("take_profit_pct", 0.15)
        self.shares_per_lot = config.get("shares_per_lot", 100)

        if state_dir:
            state_path = Path(state_dir)
            # Relative paths resolve relative to the project root
            if not state_path.is_absolute():
                state_path = PROJECT_ROOT / state_path
            self.state_dir = state_path
        else:
            self.state_dir = DEFAULT_DATA_DIR

        self.cash: float = self.initial_capital
        self.positions: Dict[str, Position] = {}
        self.trade_log: List[dict] = []
        self.daily_snapshots: List[dict] = []

        self._load()

    def _state_path(self) -> Path:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir / "state.json"

    def _load(self):
        """Load state from disk."""
        path = self._state_path()
        if not path.exists():
            return

        with open(path) as f:
            state = json.load(f)

        self.initial_capital = state.get("initial_capital", self.initial_capital)
        self.cash = state.get("cash", self.initial_capital)
        self.positions = {
            k: Position.from_dict(v) for k, v in state.get("positions", {}).items()
        }
        self.trade_log = state.get("trade_log", [])
        self.daily_snapshots = state.get("daily_snapshots", [])

    def _save(self):
        """Save state to disk."""
        state = {
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
            "trade_log": self.trade_log,
            "daily_snapshots": self.daily_snapshots,
        }

        path = self._state_path()
        with open(path, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

    def process_signal(
        self,
        ticker: str,
        name: str,
        signal: str,
        score: int,
        price: float,
        reasons: List[str],
        timestamp: str,
    ) -> Optional[dict]:
        """Process a trading signal. Returns trade action taken, or None.

        Args:
            ticker: Stock ticker
            name: Company name
            signal: Signal name (STRONG_BUY, BUY, SELL, STRONG_SELL, HOLD)
            score: Composite score
            price: Current price
            reasons: Signal reasons
            timestamp: Current timestamp string

        Returns:
            Dict describing action taken, or None
        """
        action = None

        # BUY signals
        if signal in ("STRONG_BUY", "BUY") and ticker not in self.positions:
            if len(self.positions) < self.max_positions:
                action = self._buy(ticker, name, price, score, reasons, timestamp)

        # SELL signals
        elif signal in ("STRONG_SELL", "SELL") and ticker in self.positions:
            action = self._sell(ticker, price, "signal", timestamp)

        self._save()
        return action

    def check_stop_loss_take_profit(
        self, prices: Dict[str, float], timestamp: str
    ) -> List[dict]:
        """Check all open positions for stop loss / take profit triggers.

        Args:
            prices: Dict of ticker -> current price
            timestamp: Current timestamp string

        Returns:
            List of actions taken
        """
        actions = []
        tickers_to_close = []

        for ticker, pos in self.positions.items():
            price = prices.get(ticker)
            if price is None:
                continue

            pnl_pct = pos.pnl_pct(price)

            if pnl_pct <= -self.stop_loss_pct * 100:
                tickers_to_close.append((ticker, price, "stop_loss"))
            elif pnl_pct >= self.take_profit_pct * 100:
                tickers_to_close.append((ticker, price, "take_profit"))

        for ticker, price, reason in tickers_to_close:
            action = self._sell(ticker, price, reason, timestamp)
            if action:
                actions.append(action)

        if actions:
            self._save()

        return actions

    def _buy(self, ticker: str, name: str, price: float, score: int,
             reasons: List[str], timestamp: str) -> Optional[dict]:
        """Execute a virtual buy."""
        position_value = self.cash * self.position_size_pct

        lot = self.shares_per_lot
        shares = max(lot, int(position_value / price / lot) * lot)
        cost = price * shares
        commission = cost * self.commission_rate

        if cost + commission > self.cash:
            return None

        self.cash -= cost + commission
        self.positions[ticker] = Position(
            ticker=ticker, name=name, entry_price=price,
            shares=shares, entry_date=timestamp,
            signal_score=score, reasons=reasons,
        )

        trade = {
            "action": "BUY",
            "ticker": ticker,
            "name": name,
            "price": price,
            "shares": shares,
            "cost": cost + commission,
            "score": score,
            "reasons": reasons,
            "timestamp": timestamp,
        }
        self.trade_log.append(trade)
        return trade

    def _sell(self, ticker: str, price: float, reason: str,
              timestamp: str) -> Optional[dict]:
        """Execute a virtual sell."""
        pos = self.positions.get(ticker)
        if not pos:
            return None

        proceeds = price * pos.shares
        commission = proceeds * self.commission_rate
        self.cash += proceeds - commission

        pnl = pos.pnl(price)
        pnl_pct = pos.pnl_pct(price)

        trade = {
            "action": "SELL",
            "ticker": ticker,
            "name": pos.name,
            "price": price,
            "shares": pos.shares,
            "proceeds": proceeds - commission,
            "entry_price": pos.entry_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "held_since": pos.entry_date,
            "timestamp": timestamp,
        }
        self.trade_log.append(trade)
        del self.positions[ticker]
        return trade

    def take_daily_snapshot(self, prices: Dict[str, float], timestamp: str):
        """Record daily portfolio value."""
        open_value = sum(
            prices.get(t, pos.entry_price) * pos.shares
            for t, pos in self.positions.items()
        )
        total = self.cash + open_value

        snapshot = {
            "date": timestamp,
            "cash": self.cash,
            "positions_value": open_value,
            "total": total,
            "return_pct": (total - self.initial_capital) / self.initial_capital * 100,
            "open_positions": len(self.positions),
        }

        # Only one snapshot per day
        today = timestamp[:10]
        if self.daily_snapshots and self.daily_snapshots[-1]["date"][:10] == today:
            self.daily_snapshots[-1] = snapshot
        else:
            self.daily_snapshots.append(snapshot)

        self._save()

    def get_summary(self, prices: Dict[str, float] = None) -> dict:
        """Get current portfolio summary."""
        prices = prices or {}

        open_value = sum(
            prices.get(t, pos.entry_price) * pos.shares
            for t, pos in self.positions.items()
        )
        total = self.cash + open_value

        closed_trades = [t for t in self.trade_log if t["action"] == "SELL"]
        winning = [t for t in closed_trades if t.get("pnl", 0) > 0]
        losing = [t for t in closed_trades if t.get("pnl", 0) < 0]

        return {
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "positions_value": open_value,
            "total_value": total,
            "total_return_pct": (total - self.initial_capital) / self.initial_capital * 100,
            "open_positions": len(self.positions),
            "total_closed_trades": len(closed_trades),
            "winning_trades": len(winning),
            "losing_trades": len(losing),
            "win_rate": len(winning) / len(closed_trades) * 100 if closed_trades else 0,
            "total_pnl": sum(t.get("pnl", 0) for t in closed_trades),
            "days_running": len(self.daily_snapshots),
        }

    def reset(self):
        """Reset all paper trading state."""
        self.cash = self.initial_capital
        self.positions = {}
        self.trade_log = []
        self.daily_snapshots = []
        self._save()
