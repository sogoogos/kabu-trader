"""Paper trading engine for forward-testing strategies with simulated money.

Persists all state to disk so it survives restarts. Tracks:
- Virtual cash balance
- Open and closed positions
- Every trade with entry/exit prices and P&L
- Daily portfolio snapshots
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional


PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "paper_trading"


class Position:
    """An open position."""

    def __init__(self, ticker: str, name: str, entry_price: float, shares: int,
                 entry_date: str, signal_score: int, reasons: List[str],
                 high_water_mark: Optional[float] = None,
                 last_adjusted_at: Optional[str] = None):
        self.ticker = ticker
        self.name = name
        self.entry_price = entry_price
        self.shares = shares
        self.entry_date = entry_date
        self.signal_score = signal_score
        self.reasons = reasons
        # Highest price seen since entry — used for trailing stop.
        self.high_water_mark = high_water_mark if high_water_mark is not None else entry_price
        # Most recent date a corporate-action adjustment has been applied for
        # this position. Defaults to entry_date so the first check looks
        # backward to entry. After each adjustment, this is bumped to the
        # action's date so subsequent weekly checks don't re-apply the same
        # split or dividend (which used to compound entry_price every week
        # the bug went undetected — see kabu_trader/monitor.py:365).
        self.last_adjusted_at = last_adjusted_at or entry_date

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
            "high_water_mark": self.high_water_mark,
            "last_adjusted_at": self.last_adjusted_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        return cls(
            ticker=d["ticker"], name=d["name"],
            entry_price=d["entry_price"], shares=d["shares"],
            entry_date=d["entry_date"], signal_score=d["signal_score"],
            reasons=d.get("reasons", []),
            high_water_mark=d.get("high_water_mark"),
            last_adjusted_at=d.get("last_adjusted_at"),
        )


_BROKER_OK_STATUSES = {"filled", "submitted", "presubmitted", "pendingsubmit"}


def _broker_status_ok(status: str) -> bool:
    """True when the broker considers the order live or filled.

    We whitelist rather than blacklist because IBKR uses several "failure"
    statuses (Cancelled, ApiCancelled, Rejected, Inactive, plus undocumented
    ones), and a missed one silently desyncs local paper state from the broker.
    """
    return (status or "").lower() in _BROKER_OK_STATUSES


class PaperTrader:
    """Simulates trading with virtual money. Persists state to disk."""

    def __init__(self, config: dict, state_dir: Optional[Path] = None,
                 live_broker: Optional["object"] = None):
        self.initial_capital = config.get("initial_capital", 1000000)
        self.commission_rate = config.get("commission_rate", 0.001)
        self.position_size_pct = config.get("position_size_pct", 0.1)
        self.max_positions = config.get("max_positions", 5)
        self.stop_loss_pct = config.get("stop_loss_pct", 0.05)
        self.take_profit_pct = config.get("take_profit_pct", 0.15)
        self.shares_per_lot = config.get("shares_per_lot", 100)
        # Trailing stop: once a position is up trailing_stop_activate_pct from
        # entry, exit if price falls trailing_stop_distance_pct below the high.
        self.trailing_stop_enabled = config.get("trailing_stop_enabled", True)
        self.trailing_stop_activate_pct = config.get("trailing_stop_activate_pct", 0.05)
        self.trailing_stop_distance_pct = config.get("trailing_stop_distance_pct", 0.03)
        # Time-based exit: force-close positions held longer than this many days.
        # 0 = disabled.
        self.max_hold_days = config.get("max_hold_days", 30)
        # Position rotation: when portfolio is full and a new STRONG_BUY arrives,
        # rotate out the worst-performing held position. Only fires for positions
        # that have been held long enough to develop AND are clearly losing —
        # otherwise rotation churns through every new STRONG_BUY signal at trivial
        # negative P&L (the active US trading universe generates many candidates).
        self.rotation_enabled = config.get("rotation_enabled", True)
        self.rotation_max_pnl_pct = config.get("rotation_max_pnl_pct", -0.02)
        self.rotation_min_hold_hours = config.get("rotation_min_hold_hours", 24)
        # Re-entry cooldown: don't re-buy the same ticker for N days after any exit.
        # Without this, a take-profit / trailing-stop exit immediately re-opens
        # the position because the composite score is still bullish.
        self.reentry_cooldown_days = config.get("reentry_cooldown_days", 1)

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
        # ticker -> ISO timestamp of last exit (for re-entry cooldown).
        self.last_exit: Dict[str, str] = {}

        # Live-trading bridge. When set, _buy and _sell submit real orders
        # via this adapter (e.g. IBKRBroker). Local state still gets updated
        # so the trade log, summary, and notification flows work the same.
        self.live_broker = live_broker

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
        self.last_exit = state.get("last_exit", {})

    def _save(self):
        """Save state to disk."""
        state = {
            "initial_capital": self.initial_capital,
            "cash": self.cash,
            "positions": {k: v.to_dict() for k, v in self.positions.items()},
            "trade_log": self.trade_log,
            "daily_snapshots": self.daily_snapshots,
            "last_exit": self.last_exit,
        }

        path = self._state_path()
        with open(path, "w") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)

        self._save_trades_csv()

    def _save_trades_csv(self):
        """Mirror trade_log to a CSV next to state.json for easy spreadsheet review."""
        if not self.trade_log:
            return
        fieldnames = [
            "timestamp", "action", "ticker", "name", "price", "shares",
            "cost", "proceeds", "entry_price", "pnl", "pnl_pct",
            "score", "reason", "reasons", "held_since",
        ]
        path = self.state_dir / "trades.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for trade in self.trade_log:
                row = dict(trade)
                if isinstance(row.get("reasons"), list):
                    row["reasons"] = " | ".join(row["reasons"])
                writer.writerow(row)

    def process_signal(
        self,
        ticker: str,
        name: str,
        signal: str,
        score: int,
        price: float,
        reasons: List[str],
        timestamp: str,
        current_prices: Optional[Dict[str, float]] = None,
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
            elif (self.rotation_enabled and signal == "STRONG_BUY"
                  and current_prices is not None):
                rotated = self._try_rotate(
                    new_ticker=ticker, name=name, price=price, score=score,
                    reasons=reasons, timestamp=timestamp,
                    current_prices=current_prices,
                )
                if rotated:
                    action = rotated

        # SELL signals
        elif signal in ("STRONG_SELL", "SELL") and ticker in self.positions:
            action = self._sell(ticker, price, "signal", timestamp)

        self._save()
        return action

    @staticmethod
    def _parse_timestamp(ts: str) -> Optional[datetime]:
        """Parse the trader's timestamp strings. Tolerant of seconds being absent."""
        if not ts:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(ts, fmt)
            except ValueError:
                continue
        return None

    def _try_rotate(self, new_ticker, name, price, score, reasons, timestamp,
                    current_prices):
        """Find the worst-performing held position and swap it out for a stronger one.

        Two guards prevent churn:
        - `rotation_max_pnl_pct` — only rotate clear losers (default -2%, not just
          any red position).
        - `rotation_min_hold_hours` — don't rotate positions just opened; they
          need time to develop (default 24h).
        """
        now_dt = self._parse_timestamp(timestamp)
        worst = None
        worst_pnl_pct = None
        for held_ticker, pos in self.positions.items():
            held_price = current_prices.get(held_ticker)
            if held_price is None:
                continue
            # Min-hold check: skip positions opened recently.
            if self.rotation_min_hold_hours > 0 and now_dt is not None:
                entry_dt = self._parse_timestamp(pos.entry_date)
                if entry_dt is not None:
                    held_hours = (now_dt - entry_dt).total_seconds() / 3600
                    if held_hours < self.rotation_min_hold_hours:
                        continue
            held_pnl_pct = pos.pnl_pct(held_price) / 100.0  # back to fraction
            if held_pnl_pct >= self.rotation_max_pnl_pct:
                continue
            if worst_pnl_pct is None or held_pnl_pct < worst_pnl_pct:
                worst = (held_ticker, held_price)
                worst_pnl_pct = held_pnl_pct
        if not worst:
            return None
        old_ticker, old_price = worst
        self._sell(old_ticker, old_price, "rotated_out", timestamp)
        return self._buy(new_ticker, name, price, score, reasons, timestamp)

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
        now = self._parse_timestamp(timestamp)

        for ticker, pos in self.positions.items():
            price = prices.get(ticker)
            if price is None:
                continue

            # Update high-water mark for trailing stop.
            if price > pos.high_water_mark:
                pos.high_water_mark = price

            pnl_pct = pos.pnl_pct(price)

            # Defensive guard against unrecorded corporate actions. The corp-
            # action check is throttled, so a 2:1 split that fires today will
            # show as pnl_pct ≈ -50% until apply_corporate_actions runs and
            # divides entry_price by the ratio. Without this guard, stop_loss
            # would fire immediately on the post-split price and close the
            # position at a fake -5%. Threshold: 5× the stop_loss_pct (e.g.,
            # -25% when stop is -5%) — well beyond a typical daily move,
            # safely inside split territory. Real -25% one-cycle crashes do
            # exist but are best handled by max_hold_days / rotation_out /
            # discretionary review rather than blind stop fire.
            corp_action_floor = -self.stop_loss_pct * 100 * 5
            if pnl_pct <= corp_action_floor:
                print(
                    f"Warning: {ticker} pnl_pct={pnl_pct:.1f}% beyond plausible "
                    f"single-cycle move ({corp_action_floor:.0f}%); "
                    f"suspecting unrecorded corp action, skipping stop_loss"
                )
                continue

            if pnl_pct <= -self.stop_loss_pct * 100:
                tickers_to_close.append((ticker, price, "stop_loss"))
            elif pnl_pct >= self.take_profit_pct * 100:
                tickers_to_close.append((ticker, price, "take_profit"))
            elif self.trailing_stop_enabled and pnl_pct >= self.trailing_stop_activate_pct * 100:
                # Trailing stop has armed — exit if price drops far enough below the high.
                trailing_floor = pos.high_water_mark * (1 - self.trailing_stop_distance_pct)
                if price < trailing_floor:
                    tickers_to_close.append((ticker, price, "trailing_stop"))
                    continue
            if self.max_hold_days > 0 and now is not None:
                entry_dt = self._parse_timestamp(pos.entry_date)
                if entry_dt is not None:
                    held_days = (now - entry_dt).days
                    if held_days >= self.max_hold_days:
                        # Avoid duplicate close entries.
                        if not any(t[0] == ticker for t in tickers_to_close):
                            tickers_to_close.append(
                                (ticker, price, f"time_exit_{held_days}d")
                            )

        for ticker, price, reason in tickers_to_close:
            action = self._sell(ticker, price, reason, timestamp)
            if action:
                actions.append(action)

        if actions:
            self._save()

        return actions

    def _buy(self, ticker: str, name: str, price: float, score: int,
             reasons: List[str], timestamp: str) -> Optional[dict]:
        """Execute a virtual buy.

        Position size = initial_capital * position_size_pct (not remaining cash),
        so each of `max_positions` slots gets the same intended budget regardless
        of how many other slots are already filled. Falls back to remaining cash
        as a hard ceiling if commissions have eaten too much.

        Refuses to re-buy a ticker within `reentry_cooldown_days` of its last
        exit, to prevent take-profit / trailing-stop exits from immediately
        re-opening the same position.
        """
        if self.reentry_cooldown_days > 0:
            last = self.last_exit.get(ticker)
            if last:
                last_dt = self._parse_timestamp(last)
                now_dt = self._parse_timestamp(timestamp)
                if last_dt and now_dt:
                    days_since = (now_dt - last_dt).total_seconds() / 86400
                    if days_since < self.reentry_cooldown_days:
                        return None

        position_value = min(
            self.initial_capital * self.position_size_pct,
            self.cash,
        )

        lot = self.shares_per_lot
        shares = max(lot, int(position_value / price / lot) * lot)
        cost = price * shares
        commission = cost * self.commission_rate

        if cost + commission > self.cash:
            return None

        # Live order routing — submit before mutating local state. If the
        # broker rejects, we abort the local update so paper state stays
        # consistent with the broker.
        if self.live_broker is not None:
            try:
                order_result = self.live_broker.place_order(
                    ticker=ticker, side="BUY", shares=shares, order_type="MKT",
                )
            except Exception as e:
                print(f"Live BUY rejected for {ticker}: {e}")
                return None
            if not _broker_status_ok(order_result.get("status", "")):
                print(f"Live BUY {order_result.get('status')} for {ticker} — local state not updated")
                return None
            # Prefer the broker's actual fill data over the signal-time daily
            # close so the ledger reflects what really happened. Fall back to
            # the signal estimate if the broker hasn't filled yet by the
            # polling deadline (rare for MKT on liquid names) — log clearly
            # so reconcile picks up the drift.
            raw_fill_shares = int(order_result.get("filled_shares", 0))
            raw_fill_price = float(order_result.get("avg_fill_price", 0))
            status = order_result.get("status", "")
            if raw_fill_shares == 0:
                print(f"Live BUY {ticker}: NO fill within deadline (status={status}); "
                      f"recording {shares}@¥{price:,.2f} from signal — broker may still "
                      f"complete the order; reconcile will reveal any drift")
                fill_shares, fill_price = shares, price
            elif raw_fill_shares < shares:
                print(f"Live BUY {ticker}: PARTIAL fill {raw_fill_shares}/{shares}@¥{raw_fill_price:,.2f} "
                      f"(status={status}); recording the filled portion only — "
                      f"remaining {shares - raw_fill_shares} may still execute at broker")
                fill_shares, fill_price = raw_fill_shares, raw_fill_price
            else:
                if abs(raw_fill_price - price) > 1e-9:
                    print(f"Live BUY fill drift {ticker}: signal ¥{price:,.2f} → fill ¥{raw_fill_price:,.2f}")
                fill_shares, fill_price = raw_fill_shares, raw_fill_price
            shares = fill_shares
            cost = fill_price * fill_shares
            commission = cost * self.commission_rate
            price = fill_price
            # Re-validate against cash after the fill — slippage above the
            # signal price can push the actual cost above the pre-check
            # budget. Going slightly negative is harmless arithmetically
            # but breaks the invariant that cash >= 0, which downstream
            # reporting and reconcile rely on.
            if cost + commission > self.cash:
                print(f"Live BUY {ticker}: fill cost ¥{cost + commission:,.2f} "
                      f"exceeds cash ¥{self.cash:,.2f} after slippage — "
                      f"recording position but cash will go negative; reconcile to true up")

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

        # Live order routing — same pattern as _buy.
        if self.live_broker is not None:
            # If the broker doesn't hold this position, the SELL would open
            # an unintended short. Skip the broker call and keep local-only
            # bookkeeping in sync (these are legacy positions opened before
            # the broker was wired in).
            try:
                broker_positions = {p["ticker"] for p in self.live_broker.get_positions()}
            except Exception as e:
                print(f"Could not query broker positions before SELL {ticker}: {e}")
                return None
            if ticker not in broker_positions:
                print(f"Skipping live SELL for {ticker} (not held at broker) — local-only close")
            else:
                try:
                    order_result = self.live_broker.place_order(
                        ticker=ticker, side="SELL", shares=pos.shares, order_type="MKT",
                    )
                except Exception as e:
                    print(f"Live SELL rejected for {ticker}: {e}")
                    return None
                if not _broker_status_ok(order_result.get("status", "")):
                    print(f"Live SELL {order_result.get('status')} for {ticker} — local state not updated")
                    return None
                # Use the broker's actual fill data so realized P&L matches
                # what really executed, not the daily-close estimate.
                fill_price = order_result.get("avg_fill_price", 0) or price
                if abs(fill_price - price) > 1e-9:
                    print(f"Live SELL fill drift {ticker}: signal ¥{price:,.2f} → "
                          f"fill ¥{fill_price:,.2f}")
                price = fill_price

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
        self.last_exit[ticker] = timestamp
        return trade

    def apply_corporate_actions(self, actions_by_ticker: Dict[str, dict]) -> List[dict]:
        """Adjust held positions for splits and dividends that occurred post-entry.

        For each split: divide entry_price and high-water mark by the split ratio,
        and multiply shares by it (so position economic value is preserved).
        For each dividend: credit `amount × shares` to cash AND subtract the
        per-share amount from entry_price (so the ex-div price drop doesn't trip
        the stop-loss).

        Each adjustment is logged to trade_log so it shows up in trades.csv.
        Returns the list of adjustments applied.
        """
        applied: List[dict] = []
        for ticker, actions in actions_by_ticker.items():
            pos = self.positions.get(ticker)
            if not pos:
                continue
            latest_action_date = pos.last_adjusted_at
            for split in actions.get("splits", []):
                ratio = float(split["ratio"])
                if ratio <= 0:
                    continue
                pos.entry_price = pos.entry_price / ratio
                pos.shares = int(round(pos.shares * ratio))
                pos.high_water_mark = pos.high_water_mark / ratio
                entry = {
                    "action": "ADJUST_SPLIT",
                    "ticker": ticker,
                    "name": pos.name,
                    "price": pos.entry_price,
                    "shares": pos.shares,
                    "reason": f"split {ratio:g}:1",
                    "timestamp": split["date"] + " 00:00:00",
                }
                self.trade_log.append(entry)
                applied.append(entry)
                if split["date"] > latest_action_date:
                    latest_action_date = split["date"]
            for div in actions.get("dividends", []):
                amount = float(div["amount"])
                if amount <= 0:
                    continue
                proceeds = amount * pos.shares
                self.cash += proceeds
                pos.entry_price = max(0.01, pos.entry_price - amount)
                entry = {
                    "action": "DIVIDEND",
                    "ticker": ticker,
                    "name": pos.name,
                    "price": amount,
                    "shares": pos.shares,
                    "proceeds": proceeds,
                    "reason": f"dividend {amount:.4f}/share × {pos.shares} = {proceeds:.2f}",
                    "timestamp": div["date"] + " 00:00:00",
                }
                self.trade_log.append(entry)
                applied.append(entry)
                if div["date"] > latest_action_date:
                    latest_action_date = div["date"]
            # Bump the watermark so next week's check doesn't re-apply these.
            if latest_action_date != pos.last_adjusted_at:
                pos.last_adjusted_at = latest_action_date
        if applied:
            self._save()
        return applied

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
