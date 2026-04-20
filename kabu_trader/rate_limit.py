"""Shared circuit breaker for yfinance rate-limit (HTTP 429) responses.

When any yfinance caller sees a 429 / "Too Many Requests" error, it reports it
here. Subsequent callers check the breaker before firing a request and skip if
we're still cooling down. The breaker clears automatically after COOLDOWN_SECONDS
with no new 429s.

Why this matters: Yahoo's rate limit on an IP gets *extended* every time you
retry while banned. Sitting quietly is the only way out.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

# After a 429, pause all yfinance calls for this long.
COOLDOWN_SECONDS = 30 * 60  # 30 min — Yahoo bans often clear in 15–60 min.

_last_429: Optional[float] = None
_lock = threading.Lock()


def report() -> None:
    """Record that yfinance just returned 429."""
    global _last_429
    with _lock:
        _last_429 = time.time()


def is_cooling_down(cooldown: int = COOLDOWN_SECONDS) -> bool:
    """True if we're inside the cooldown window and should skip yfinance calls."""
    if _last_429 is None:
        return False
    return time.time() - _last_429 < cooldown


def seconds_remaining(cooldown: int = COOLDOWN_SECONDS) -> int:
    """How much longer the cooldown lasts. 0 if not active."""
    if _last_429 is None:
        return 0
    return max(0, cooldown - int(time.time() - _last_429))


def detect_and_record(exc: BaseException) -> bool:
    """If the exception is a rate-limit error, record it and return True."""
    msg = str(exc).lower()
    if "too many requests" in msg or "429" in msg or "rate limit" in msg:
        report()
        return True
    return False


def reset() -> None:
    """Clear the breaker (for tests or after a known-good recovery)."""
    global _last_429
    with _lock:
        _last_429 = None
