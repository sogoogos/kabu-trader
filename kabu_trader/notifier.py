"""LINE Messaging API notification."""

from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen
from urllib.error import URLError


LINE_API_URL = "https://api.line.me/v2/bot/message/push"


class LineNotifier:
    """Sends alerts via LINE Messaging API (free tier: 200 messages/month)."""

    def __init__(self, config: dict):
        self.enabled = config.get("enabled", False)
        self.paper_mode = False
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

    def send(self, message: str) -> bool:
        """Send a LINE message. Returns True on success."""
        if not self.enabled:
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
            f"📊 Kabu Trader Alert [{mode_tag}]",
            f"",
            f"{'🟢' if 'BUY' in signal else '🔴'} {signal}",
            f"📌 {label}",
            f"💰 ¥{price:,.0f}",
            f"📈 Score: {score}",
        ]
        if reasons:
            lines.append("")
            for r in reasons[:3]:
                lines.append(f"• {r}")

        return "\n".join(lines)
