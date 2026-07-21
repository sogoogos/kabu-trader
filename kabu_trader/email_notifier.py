"""SMTP email notifier — backup channel for watchdog alerts.

LINE has a 200 msg/month free-tier cap. When the cap is hit, watchdog alerts
silently fail (see 2026-05-28 outage). Gmail SMTP gives an unlimited backup
channel for the small number of critical messages we actually need to deliver.

Setup (one-time):
  1. Enable 2FA on the Gmail account that will send.
  2. Generate an App Password at https://myaccount.google.com/apppasswords
  3. Put it in ~/.env as EMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
  4. Set "enabled": true plus sender/recipient in config/default.json's "email" section.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from html import escape


class EmailNotifier:
    """Sends short alert emails via SMTP. Used as a fallback when LINE is rate-limited.

    Designed to never raise — SMTP failures are logged and swallowed so the
    monitor loop keeps running.
    """

    def __init__(self, config: dict):
        self.enabled = config.get("enabled", False)
        if not self.enabled:
            return

        self.smtp_host = config.get("smtp_host", "smtp.gmail.com")
        self.smtp_port = int(config.get("smtp_port", 587))
        self.sender = config.get("sender") or os.environ.get("EMAIL_SENDER", "")
        self.recipient = config.get("recipient") or os.environ.get("EMAIL_RECIPIENT", "")
        self.app_password = (
            config.get("app_password")
            or os.environ.get("EMAIL_APP_PASSWORD", "")
        )

        if not (self.sender and self.recipient and self.app_password):
            print("Warning: email enabled but missing sender/recipient/app_password.")
            self.enabled = False

    def send(self, subject: str, body: str, monospace: bool = False) -> bool:
        """Send an email. Returns True on success, False otherwise (never raises).

        Set monospace for preformatted bodies (tables, column-aligned reports).
        Mail clients render plain text in a proportional font, which shreds any
        column alignment, so an HTML <pre> alternative is attached alongside the
        plain-text part.
        """
        if not self.enabled:
            return False

        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = self.recipient
        msg["Subject"] = subject
        msg.set_content(body)
        if monospace:
            msg.add_alternative(
                "<html><body>"
                '<pre style="font-family:ui-monospace,Menlo,Consolas,monospace;'
                'font-size:13px;line-height:1.4">'
                f"{escape(body)}</pre></body></html>",
                subtype="html",
            )

        try:
            context = ssl.create_default_context()
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as s:
                s.starttls(context=context)
                s.login(self.sender, self.app_password)
                s.send_message(msg)
            return True
        except Exception as e:
            print(f"Email send failed: {type(e).__name__}: {e}")
            return False
