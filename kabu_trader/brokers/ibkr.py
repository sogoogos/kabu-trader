"""Interactive Brokers (IBKR) broker adapter via ib_insync.

Connects to a running IB Gateway or TWS instance over the TWS API socket protocol.
Default ports:
  - IB Gateway: 4002 (paper) / 4001 (live)
  - TWS: 7497 (paper) / 7496 (live)

Safety design:
  - `enabled=False` by default. Must be explicitly set true in config.
  - `paper=True` by default. Caller must pass `paper=False` *and* the live port
    to actually place real orders.
  - Methods raise on error (no silent fallback) so the caller decides whether
    to retry or refuse to record a trade locally.

Setup:
  1. Install ib_insync:                pip install ib_insync
  2. Run IB Gateway (Docker recommended on EC2):
       docker run -d --name ib-gateway \\
         -e TWS_USERID=$IBKR_USER -e TWS_PASSWORD=$IBKR_PASS \\
         -e TRADING_MODE=paper -p 4002:4002 \\
         gnzsnz/ib-gateway:stable
  3. In IB Gateway's Configuration → API → Settings:
       ✓ Enable ActiveX and Socket Clients
       Trusted IPs: add your kabu-trader container IP (or 127.0.0.1 / 0.0.0.0)
  4. Configure kabu_trader with the connection details.
"""

from __future__ import annotations

import threading
from typing import Optional


class IBKRBroker:
    """Thin wrapper around ib_insync.IB with synchronous, blocking semantics.

    All methods that hit the API are called inside short event-loop runs so
    callers don't need to deal with asyncio. Connection state is kept across
    calls; explicit `connect()` / `disconnect()` allowed but most methods will
    auto-connect if needed.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4002,
        client_id: int = 1,
        paper: bool = True,
        readonly: bool = False,
        timeout: int = 10,
    ):
        # Defer ib_insync import so kabu_trader keeps loading even if the package
        # isn't installed (only required when the IBKR broker is actually enabled).
        try:
            from ib_insync import IB
        except ImportError as e:
            raise RuntimeError(
                "ib_insync is required for IBKRBroker. Install with: pip install ib_insync"
            ) from e

        self.host = host
        self.port = port
        self.client_id = client_id
        self.paper = paper
        self.readonly = readonly
        self.timeout = timeout
        self._ib = IB()
        self._lock = threading.Lock()
        # Track last successful connect so we can throttle reconnect attempts.
        self._last_connect_attempt = 0.0
        self._reconnect_min_interval = 30.0  # seconds

    # --- connection ---

    def connect(self) -> None:
        with self._lock:
            if self._ib.isConnected():
                return
            self._ib.connect(
                host=self.host, port=self.port, clientId=self.client_id,
                readonly=self.readonly, timeout=self.timeout,
            )

    def disconnect(self) -> None:
        with self._lock:
            if self._ib.isConnected():
                self._ib.disconnect()

    def _ensure(self) -> None:
        """Connect if needed. Auto-reconnect after Gateway nightly restart.

        ib_insync.IB.isConnected() returns False after Gateway drops the
        session (which happens daily ~midnight ET due to IBKR's forced
        logout). We attempt reconnect on the next API call but throttle to
        avoid hammering Gateway while it's mid-restart.
        """
        import time
        if self._ib.isConnected():
            return
        now = time.time()
        if now - self._last_connect_attempt < self._reconnect_min_interval:
            raise ConnectionError(
                f"IBKR Gateway disconnected; last reconnect attempt "
                f"{now - self._last_connect_attempt:.0f}s ago (waiting "
                f"{self._reconnect_min_interval:.0f}s between attempts)"
            )
        self._last_connect_attempt = now
        # Reset the IB instance to clear any stale event handlers / state.
        from ib_insync import IB
        self._ib = IB()
        self.connect()

    def is_healthy(self) -> tuple[bool, str]:
        """Lightweight liveness check used by the monitor watchdog.

        Attempts to (re)connect if needed, then issues a small server roundtrip
        to confirm the API is actually responsive (Gateway can be 'up' as a
        container but stuck pre-login after a failed 2FA — TCP refused on 4002).
        Never raises; returns (ok, reason).
        """
        try:
            self._ensure()
            with self._lock:
                self._ib.reqCurrentTime()
            return True, "ok"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    # --- contract building ---

    @staticmethod
    def _make_contract(ticker: str):
        """Translate our ticker conventions to ib_insync.Stock contracts.

        - "7203.T" → SMART routing with primaryExchange=TSEJ, JPY
        - "BRK-B"  → SMART, USD  (preserves dash; IBKR accepts it for class shares)
        - "AAPL"   → SMART, USD

        Note: direct-routing (exchange='TSEJ') gets rejected by Gateway's
        precautionary settings ("Error 10311"). SMART routing avoids that.
        """
        from ib_insync import Stock
        if ticker.endswith(".T"):
            return Stock(ticker[:-2], "SMART", "JPY", primaryExchange="TSEJ")
        return Stock(ticker, "SMART", "USD")

    # --- orders ---

    def place_order(
        self, ticker: str, side: str, shares: int,
        order_type: str = "MKT", limit_price: Optional[float] = None,
        outside_rth: bool = False,
    ) -> dict:
        """Place a buy/sell order. Returns {order_id, status, ticker, side, shares}.

        side: "BUY" or "SELL"
        order_type: "MKT" (market) or "LMT" (limit; requires limit_price)
        """
        from ib_insync import MarketOrder, LimitOrder
        self._ensure()
        contract = self._make_contract(ticker)

        action = side.upper().replace("STRONG_", "")  # STRONG_BUY → BUY
        if action not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side: {side}")

        if order_type.upper() == "MKT":
            order = MarketOrder(action, shares)
        elif order_type.upper() == "LMT":
            if limit_price is None:
                raise ValueError("LMT order requires limit_price")
            order = LimitOrder(action, shares, limit_price)
        else:
            raise ValueError(f"Unsupported order_type: {order_type}")
        order.outsideRth = outside_rth
        order.tif = "DAY"

        with self._lock:
            trade = self._ib.placeOrder(contract, order)
            for _ in range(10):
                self._ib.sleep(0.3)
                if trade.orderStatus.status in (
                    "Submitted", "PreSubmitted", "Filled", "Cancelled",
                    "ApiCancelled", "Inactive",
                ):
                    break
        return {
            "order_id": trade.order.orderId,
            "perm_id": trade.order.permId,
            "status": trade.orderStatus.status,
            "ticker": ticker,
            "side": action,
            "shares": shares,
        }

    def cancel_order(self, order_id: int) -> bool:
        self._ensure()
        with self._lock:
            for trade in self._ib.openTrades():
                if trade.order.orderId == order_id:
                    self._ib.cancelOrder(trade.order)
                    return True
        return False

    # --- state queries ---

    def get_positions(self) -> list[dict]:
        self._ensure()
        with self._lock:
            positions = self._ib.positions()
        return [
            {
                "ticker": _ticker_from_contract(p.contract),
                "shares": int(p.position),
                "avg_cost": float(p.avgCost),
            }
            for p in positions
        ]

    def get_orders(self) -> list[dict]:
        self._ensure()
        with self._lock:
            trades = self._ib.openTrades()
        return [
            {
                "order_id": t.order.orderId,
                "ticker": _ticker_from_contract(t.contract),
                "side": t.order.action,
                "shares": int(t.order.totalQuantity),
                "status": t.orderStatus.status,
                "filled": int(t.orderStatus.filled),
                "remaining": int(t.orderStatus.remaining),
                "avg_fill_price": float(t.orderStatus.avgFillPrice or 0),
            }
            for t in trades
        ]

    def get_quote(self, ticker: str) -> dict:
        """Snapshot quote. Returns {bid, ask, last, close}."""
        self._ensure()
        contract = self._make_contract(ticker)
        with self._lock:
            t = self._ib.reqMktData(contract, snapshot=True)
            # snapshot fills in within ~11 seconds; wait briefly
            self._ib.sleep(2.0)
            data = {
                "ticker": ticker,
                "bid": float(t.bid or 0),
                "ask": float(t.ask or 0),
                "last": float(t.last or 0),
                "close": float(t.close or 0),
            }
            self._ib.cancelMktData(contract)
        return data

    def get_account_summary(self) -> dict:
        """Return key account stats: cash, net liquidation, buying power."""
        self._ensure()
        with self._lock:
            summary = self._ib.accountSummary()
        result = {}
        for item in summary:
            if item.tag in ("TotalCashValue", "NetLiquidation", "BuyingPower",
                            "AvailableFunds"):
                try:
                    result[item.tag] = float(item.value)
                except ValueError:
                    pass
        return result


def _ticker_from_contract(contract) -> str:
    """Map an IBKR Contract back to our ticker convention (.T suffix for TSE)."""
    if (contract.exchange == "TSEJ" or contract.primaryExchange == "TSEJ"
            or contract.currency == "JPY"):
        return f"{contract.symbol}.T"
    return contract.symbol
