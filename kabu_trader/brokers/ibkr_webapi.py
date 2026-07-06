"""Interactive Brokers broker adapter via the Client Portal Web API (OAuth 1.0a).

Drop-in replacement for `IBKRBroker` (ibkr.py) that talks directly to
`api.ibkr.com` using first-party OAuth 1.0a tokens — **no IB Gateway / TWS and
no interactive login / 2FA**. Built on the `ibind` client.

Why this exists: from 2026-07-01 IBKR Japan made passkey 2FA mandatory, which a
headless IB Gateway cannot satisfy. OAuth token auth sidesteps interactive login
entirely. See docs/IBKR_OAUTH_SETUP.md.

Credentials come from environment variables read by ibind (set from
`~/ibkr-oauth/oauth.env` on the server, 600-perm, never committed):
    IBIND_USE_OAUTH=True
    IBIND_OAUTH1A_CONSUMER_KEY, IBIND_OAUTH1A_ACCESS_TOKEN,
    IBIND_OAUTH1A_ACCESS_TOKEN_SECRET, IBIND_OAUTH1A_DH_PRIME,
    IBIND_OAUTH1A_SIGNATURE_KEY_FP, IBIND_OAUTH1A_ENCRYPTION_KEY_FP

Interface parity with IBKRBroker:
    connect / disconnect / is_healthy / place_order / cancel_order /
    get_positions / get_orders / get_quote / get_account_summary

Safety design mirrors ibkr.py: methods raise on error (no silent fallback);
`is_healthy` never raises. The OAuth account is fixed by the username the token
was registered under — pass `account_id` (or let it auto-resolve) and verify it
points at the intended live/paper account before trusting orders.

NOTE: field-name parsing below is written defensively (tries several key names)
because the Web API's exact JSON keys must be confirmed on the first live/paper
run once OAuth activation completes. Marked with `# VERIFY` where relevant.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

# IBKR live market-data field codes for the snapshot endpoint.
_FLD_LAST = "31"
_FLD_BID = "84"
_FLD_ASK = "86"
_FLD_CLOSE = "7296"  # prior close  # VERIFY

_TERMINAL_STATUSES = {"Filled", "Cancelled", "ApiCancelled", "Inactive", "Rejected"}


class IBKRWebAPIBroker:
    """Web API (OAuth) broker adapter, interface-compatible with IBKRBroker."""

    def __init__(
        self,
        account_id: Optional[str] = None,
        paper: bool = True,
        readonly: bool = False,
        timeout: int = 30,
        # Accepted for drop-in compatibility with IBKRBroker; unused here since
        # OAuth talks directly to api.ibkr.com (no host/port/client_id).
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None,
    ):
        # Defer ib import so kabu_trader keeps loading even if ibind isn't
        # installed (only required when this broker is actually enabled).
        try:
            from ibind import IbkrClient  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "ibind is required for IBKRWebAPIBroker. Install with: "
                "pip install ibind pycryptodome"
            ) from e

        self.account_id = account_id
        self.paper = paper
        self.readonly = readonly
        self.timeout = timeout
        self._client = None
        self._lock = threading.RLock()
        self._conid_cache: dict[str, int] = {}
        self._last_connect_attempt = 0.0
        self._reconnect_min_interval = 30.0

    # --- connection ---

    def connect(self) -> None:
        with self._lock:
            if self._client is not None:
                return
            from ibind import IbkrClient
            client = IbkrClient(use_oauth=True)
            # LST is fetched on construction, but the brokerage (iserver)
            # session is NOT established immediately: the first
            # initialize_brokerage_session() returns established=False and
            # /iserver/accounts is empty; it flips to established=True within a
            # few seconds. Trading endpoints (orders/whatif) reject the account
            # ("accountId is not valid") until then, so retry until established.
            established = False
            for _ in range(10):
                client.initialize_brokerage_session()
                try:
                    established = bool(
                        client.authentication_status().data.get("established")
                    )
                except Exception:
                    established = False
                if established:
                    break
                time.sleep(2)
            if not established:
                raise ConnectionError(
                    "IBKR brokerage session did not establish (still "
                    "established=False after retries)"
                )
            # Prime /iserver/accounts — required before market-data snapshots and
            # order endpoints, which otherwise 500 with 'Please query /accounts
            # first'.
            try:
                client.receive_brokerage_accounts()
            except Exception:
                pass
            client.start_tickler(60)
            self._client = client
            if self.account_id is None:
                self.account_id = self._resolve_account_id(client)

    def disconnect(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.stop_tickler()
                except Exception:
                    pass
                try:
                    self._client.oauth_shutdown()
                except Exception:
                    pass
                self._client = None

    def _ensure(self):
        """Return a live client, (re)connecting if needed, throttled."""
        with self._lock:
            if self._client is not None:
                return self._client
            now = time.time()
            if now - self._last_connect_attempt < self._reconnect_min_interval:
                raise ConnectionError(
                    f"IBKR Web API not connected; last attempt "
                    f"{now - self._last_connect_attempt:.0f}s ago "
                    f"(waiting {self._reconnect_min_interval:.0f}s)"
                )
            self._last_connect_attempt = now
            self.connect()
            return self._client

    @staticmethod
    def _resolve_account_id(client) -> str:
        """Pick the account id the OAuth token controls."""
        res = client.portfolio_accounts()
        data = res.data if hasattr(res, "data") else res
        if not data:
            raise RuntimeError("No accounts returned for this OAuth token")
        acct = data[0]
        return acct.get("accountId") or acct.get("id") or acct["accountId"]

    def is_healthy(self) -> tuple[bool, str]:
        """Lightweight liveness check for the monitor watchdog. Never raises."""
        try:
            client = self._ensure()
            with self._lock:
                client.tickle()
            return True, "ok"
        except Exception as e:
            # Drop the (possibly dead) client so the next call reconnects.
            with self._lock:
                self._client = None
            return False, f"{type(e).__name__}: {e}"

    # --- contract resolution ---

    def _resolve_conid(self, ticker: str) -> int:
        """Translate our ticker convention to an IBKR conid, cached.

        Uses the /trsrv/stocks reference DB (ibind.stock_conid_by_symbol). That
        endpoint returns one instrument per exchange, so a bare numeric symbol
        like "2371" matches TSE (Japan), TWSE (Taiwan) and SEHK (Hong Kong) —
        we must filter by exchange. IBKR's default filter is {isUS: True}, which
        drops all JP listings, so default_filtering must be disabled.

        - "2371.T" -> exchange TSEJ  (Japanese TSE listing)  -> conid 44060588
        - "AAPL"   -> isUS True      (US listing)
        """
        if ticker in self._conid_cache:
            return self._conid_cache[ticker]

        from ibind import StockQuery
        client = self._ensure()
        if ticker.endswith(".T"):
            symbol = ticker[:-2]
            conditions = {"exchange": "TSEJ"}
        else:
            symbol = ticker
            conditions = {"isUS": True}

        query = StockQuery(symbol, contract_conditions=conditions)
        with self._lock:
            res = client.stock_conid_by_symbol(query, default_filtering=False)
        data = res.data if hasattr(res, "data") else res
        # stock_conid_by_symbol returns {symbol: conid} or {symbol: [conids]}.
        conid = data.get(symbol) if isinstance(data, dict) else data
        if isinstance(conid, (list, tuple)):
            if not conid:
                raise ValueError(f"No conid found for {ticker}")
            conid = conid[0]
        if conid is None:
            raise ValueError(f"No conid found for {ticker}")
        conid = int(conid)
        self._conid_cache[ticker] = conid
        return conid

    @staticmethod
    def _ticker_from_position(pos: dict) -> str:
        """Map a Web API position/order row back to our ticker convention."""
        raw = pos.get("ticker") or pos.get("contractDesc") or pos.get("symbol") or ""
        symbol = raw.split()[0] if raw else ""
        currency = pos.get("currency") or pos.get("baseCurrency")
        if currency == "JPY":
            return f"{symbol}.T"
        return symbol

    # --- orders ---

    def _answers(self):
        """Auto-confirm all precautionary order questions (TWS-API parity)."""
        from ibind import QuestionType
        return {q: True for q in QuestionType}

    def place_order(
        self, ticker: str, side: str, shares: int,
        order_type: str = "MKT", limit_price: Optional[float] = None,
        outside_rth: bool = False,
    ) -> dict:
        """Place a buy/sell order. Returns broker fill info (same shape as
        IBKRBroker.place_order):
            {order_id, perm_id, status, ticker, side, shares,
             filled_shares, avg_fill_price}
        """
        from ibind import OrderRequest
        client = self._ensure()

        action = side.upper().replace("STRONG_", "")  # STRONG_BUY -> BUY
        if action not in ("BUY", "SELL"):
            raise ValueError(f"Invalid side: {side}")
        ot = order_type.upper()
        if ot not in ("MKT", "LMT"):
            raise ValueError(f"Unsupported order_type: {order_type}")
        if ot == "LMT" and limit_price is None:
            raise ValueError("LMT order requires limit_price")

        conid = self._resolve_conid(ticker)
        # coid: unique client order id so retries don't double-submit.
        coid = f"kt-{conid}-{int(time.time())}"
        order = OrderRequest(
            conid=conid,
            side=action,
            quantity=int(shares),
            order_type=ot,
            acct_id=self.account_id,
            price=limit_price if ot == "LMT" else None,
            tif="DAY",
            outside_rth=outside_rth,
            coid=coid,
        )

        with self._lock:
            res = client.place_order(order, self._answers(), account_id=self.account_id)
            data = res.data if hasattr(res, "data") else res
            first = data[0] if isinstance(data, (list, tuple)) and data else data
            order_id = str(
                (first or {}).get("order_id")
                or (first or {}).get("orderId")
                or (first or {}).get("id")
                or ""
            )

            # Poll for a terminal status (~10s), like the TWS-API adapter.
            status = (first or {}).get("order_status") or (first or {}).get("status") or ""
            filled = 0
            avg_price = 0.0
            for _ in range(20):  # 20 x 0.5s = 10s
                if status in _TERMINAL_STATUSES:
                    break
                time.sleep(0.5)
                try:
                    st = client.order_status(order_id)
                    sd = st.data if hasattr(st, "data") else st
                except Exception:
                    continue
                status = sd.get("order_status") or sd.get("status") or status
                filled = int(float(sd.get("filled_quantity") or sd.get("cumFill")
                                   or sd.get("filledQuantity") or 0) or 0)
                avg_price = float(sd.get("average_price") or sd.get("avgPrice")
                                  or sd.get("avg_price") or 0) or 0.0

        return {
            "order_id": order_id,
            "perm_id": (first or {}).get("perm_id") or (first or {}).get("permId"),
            "status": status,
            "ticker": ticker,
            "side": action,
            "shares": int(shares),
            "filled_shares": filled,
            "avg_fill_price": avg_price,
        }

    def cancel_order(self, order_id) -> bool:
        client = self._ensure()
        with self._lock:
            res = client.cancel_order(str(order_id), account_id=self.account_id)
        data = res.data if hasattr(res, "data") else res
        # Web API returns a confirmation dict; treat any non-error as success.
        return bool(data)

    # --- state queries ---

    def get_positions(self) -> list[dict]:
        client = self._ensure()
        with self._lock:
            res = client.positions(account_id=self.account_id)
        data = res.data if hasattr(res, "data") else res
        out = []
        for p in (data or []):
            qty = p.get("position")
            if qty in (None, 0):
                continue
            out.append({
                "ticker": self._ticker_from_position(p),
                "shares": int(qty),
                "avg_cost": float(p.get("avgCost") or p.get("avg_cost") or 0),
            })
        return out

    def get_orders(self) -> list[dict]:
        client = self._ensure()
        with self._lock:
            res = client.live_orders(account_id=self.account_id)
        data = res.data if hasattr(res, "data") else res
        # live_orders returns {"orders": [...]} or a bare list.
        orders = data.get("orders") if isinstance(data, dict) else data
        out = []
        for o in (orders or []):
            total = int(float(o.get("totalSize") or o.get("quantity") or 0) or 0)
            filled = int(float(o.get("filledQuantity") or o.get("cumFill") or 0) or 0)
            out.append({
                "order_id": str(o.get("orderId") or o.get("order_id") or ""),
                "ticker": self._ticker_from_position(o),
                "side": (o.get("side") or "").upper(),
                "shares": total,
                "status": o.get("status") or o.get("order_status") or "",
                "filled": filled,
                "remaining": max(total - filled, 0),
                "avg_fill_price": float(o.get("avgPrice") or o.get("average_price") or 0) or 0.0,
            })
        return out

    def get_quote(self, ticker: str) -> dict:
        """Snapshot quote. Returns {ticker, bid, ask, last, close}."""
        client = self._ensure()
        conid = self._resolve_conid(ticker)
        fields = [_FLD_LAST, _FLD_BID, _FLD_ASK, _FLD_CLOSE]
        row = {}
        with self._lock:
            # First snapshot call can return empty while IBKR primes the feed.
            for _ in range(6):
                res = client.live_marketdata_snapshot(str(conid), fields)
                data = res.data if hasattr(res, "data") else res
                row = (data[0] if isinstance(data, (list, tuple)) and data else data) or {}
                if any(f in row for f in fields):
                    break
                time.sleep(0.5)
        return {
            "ticker": ticker,
            "bid": float(row.get(_FLD_BID) or 0) or 0.0,
            "ask": float(row.get(_FLD_ASK) or 0) or 0.0,
            "last": float(row.get(_FLD_LAST) or 0) or 0.0,
            "close": float(row.get(_FLD_CLOSE) or 0) or 0.0,
        }

    def get_account_summary(self) -> dict:
        """Return key account stats: cash, net liquidation, buying power."""
        client = self._ensure()
        with self._lock:
            res = client.portfolio_summary(account_id=self.account_id)
        data = res.data if hasattr(res, "data") else res
        # Web API summary values are dicts like {"amount": 123.4, ...}. Map to
        # the TWS-API tag names IBKRBroker returned for caller compatibility.
        key_map = {
            "totalcashvalue": "TotalCashValue",
            "netliquidation": "NetLiquidation",
            "buyingpower": "BuyingPower",
            "availablefunds": "AvailableFunds",
        }
        result = {}
        for src, tag in key_map.items():
            val = (data or {}).get(src)
            if isinstance(val, dict):
                val = val.get("amount")
            if val is not None:
                try:
                    result[tag] = float(val)
                except (TypeError, ValueError):
                    pass
        return result
