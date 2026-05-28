"""LINE Messaging API notification."""

from __future__ import annotations

import json
import os
import time
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


LINE_API_URL = "https://api.line.me/v2/bot/message/push"


class LineNotifier:
    """Sends alerts via LINE Messaging API (free tier: 200 messages/month)."""

    def __init__(self, config: dict, currency_symbol: str = "¥", market_name: str = "JP"):
        self.enabled = config.get("enabled", False)
        self.paper_mode = False
        self.currency_symbol = currency_symbol
        self.market_name = market_name
        if not self.enabled:
            return

        self.channel_access_token = (
            config.get("channel_access_token")
            or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
        )
        self.user_id = (
            config.get("user_id")
            or os.environ.get("LINE_USER_ID", "")
        )

        if not self.channel_access_token or not self.user_id:
            print("Warning: LINE enabled but missing channel_access_token or user_id.")
            self.enabled = False

        # Circuit breaker state for 429 (rate limit / monthly cap).
        self._rate_limited_until: float = 0.0

    def send(self, message: str) -> bool:
        """Send a LINE message. Returns True on success.

        Backs off silently when rate-limited so we don't burn cycles retrying.
        """
        if not self.enabled:
            return False
        if self._rate_limited_until and time.time() < self._rate_limited_until:
            return False

        payload = json.dumps({
            "to": self.user_id,
            "messages": [
                {"type": "text", "text": message}
            ],
        }).encode("utf-8")

        req = Request(
            LINE_API_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.channel_access_token}",
            },
            method="POST",
        )

        try:
            with urlopen(req) as resp:
                return resp.status == 200
        except HTTPError as e:
            if e.code == 429:
                # Try to honor Retry-After if LINE provides it; otherwise back off
                # for an hour (covers per-minute throttle and gives monthly-cap
                # users a chance to notice without spamming the log).
                retry_after = e.headers.get("Retry-After") if e.headers else None
                cooldown = 3600
                try:
                    if retry_after:
                        cooldown = max(60, int(retry_after))
                except ValueError:
                    pass
                self._rate_limited_until = time.time() + cooldown
                print(
                    f"LINE rate-limited (429) — backing off {cooldown}s. "
                    f"Likely the free-tier monthly cap (500 msgs/month) — "
                    f"check the LINE Developers console."
                )
            else:
                print(f"LINE send failed: HTTP {e.code} {e.reason}")
            return False
        except URLError as e:
            print(f"LINE send failed: {e}")
            return False

    def format_alert(self, alert: dict, name: str = "") -> str:
        """Format a trading alert into a LINE message."""
        ticker = alert["ticker"]
        label = f"{name} ({ticker})" if name else ticker
        signal = alert["signal"]
        price = alert["price"]
        score = alert["score"]
        reasons = alert.get("reasons", [])

        mode_tag = "🧪 PAPER TEST" if self.paper_mode else "💹 LIVE"
        lines = [
            f"📊 Kabu Trader [{self.market_name}] Alert [{mode_tag}]",
            f"",
            f"{'🟢' if 'BUY' in signal else '🔴'} {signal}",
            f"📌 {label}",
            f"💰 {self.currency_symbol}{price:,.2f}",
            f"📈 Score: {score}",
        ]
        # ML reasons are kept in scoring but hidden from LINE: they're noisy and
        # the user has asked to keep messages focused on actionable indicators.
        visible = [r for r in reasons if not r.startswith("ML model:")]
        if visible:
            lines.append("")
            for r in visible[:3]:
                lines.append(f"• {r}")

        return "\n".join(lines)
