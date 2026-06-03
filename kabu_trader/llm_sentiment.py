"""LLM-based sentiment analysis for stocks using OpenAI API.

Analyzes news headlines and generates structured sentiment scores
that feed into the composite trading signal.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .news_fetcher import fetch_stock_news


SENTIMENT_PROMPT = """You are a stock market analyst. Analyze the following news headlines for {company} ({ticker}) and provide a trading sentiment assessment.

News headlines:
{headlines}

Additional context:
- Current price: {price_str}
- Recent performance: {performance}

Respond in the following JSON format only, with no other text:
{{
  "score": <integer from -5 to 5>,
  "confidence": <float from 0.0 to 1.0>,
  "reasoning": "<one sentence in English explaining your assessment>",
  "key_factors": ["<factor 1>", "<factor 2>"]
}}

Scoring guide:
- +5: Extremely bullish (major positive catalyst, strong earnings beat, major deal)
- +3: Bullish (positive earnings, upgrades, favorable policy)
- +1: Slightly bullish (minor positive news)
-  0: Neutral (no clear direction)
- -1: Slightly bearish (minor concerns)
- -3: Bearish (earnings miss, downgrades, negative policy)
- -5: Extremely bearish (scandal, major loss, regulatory action)

Be conservative. Most news is neutral (0) or slightly positive/negative (+/-1).
Only give +/-3 or higher for genuinely significant news."""


BATCH_SENTIMENT_PROMPT = """You are a stock market analyst. Analyze the news for each stock below and provide a sentiment assessment.

{stock_blocks}

Respond with a single JSON object. Each key is the ticker; each value is:
{{
  "score": <integer -5 to +5>,
  "confidence": <float 0.0 to 1.0>,
  "reasoning": "<one sentence in English>"
}}

Include ALL {count} tickers in your response. Return JSON only, no other text.

Scoring guide (be conservative — most news is 0 or ±1):
- +5/-5: major catalyst (scandal, major deal, huge beat/miss)
- +3/-3: earnings beat/miss, upgrade/downgrade, material policy
- +1/-1: minor news
-    0: neutral / no clear direction"""


class LLMSentimentAnalyzer:
    """Analyzes stock news sentiment using OpenAI API."""

    def __init__(self, config: dict, cache_path: Optional[Path] = None):
        self.enabled = config.get("enabled", False)
        self._cache_path: Optional[Path] = cache_path
        if not self.enabled:
            return

        self.api_key = config.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        self.model = config.get("model", "gpt-4o-mini")
        self._cache: Dict[str, dict] = {}
        # Circuit breaker: absolute timestamp until which we skip OpenAI calls.
        # Set on 429 errors (daily rate limit). Cleared next successful call window.
        self._rate_limited_until: float = 0.0

        if not self.api_key:
            print("Warning: LLM sentiment enabled but no API key. "
                  "Set 'api_key' in config or OPENAI_API_KEY env var.")
            self.enabled = False
            return

        try:
            from openai import OpenAI
            self.client = OpenAI(api_key=self.api_key)
        except ImportError:
            print("Warning: openai package not installed. Run: pip install openai")
            self.enabled = False
            return

        # Load persisted cache so container restart doesn't trigger a 5-10min
        # cold-start refresh of 400+ tickers. TTL is enforced on load so stale
        # entries beyond 6h are dropped.
        self._load_cache()

    def _load_cache(self) -> None:
        if not self._cache_path or not self._cache_path.exists():
            return
        try:
            with open(self._cache_path) as f:
                raw = json.load(f)
            now = time.time()
            self._cache = {
                k: v for k, v in raw.items()
                if isinstance(v, dict) and now - v.get("_timestamp", 0) < 6 * 3600
            }
            dropped = len(raw) - len(self._cache)
            print(f"LLM sentiment cache loaded: {len(self._cache)} entries"
                  + (f" ({dropped} stale dropped)" if dropped else ""))
        except Exception as e:
            print(f"Warning: failed to load LLM sentiment cache: {e}")
            self._cache = {}

    def _save_cache(self) -> None:
        if not self._cache_path:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(self._cache_path.suffix + ".tmp")
            with open(tmp, "w") as f:
                json.dump(self._cache, f, ensure_ascii=False)
            tmp.replace(self._cache_path)
        except Exception as e:
            print(f"Warning: failed to save LLM sentiment cache: {e}")

    def analyze_stock(
        self,
        ticker: str,
        company_name: str = "",
        price: float = 0,
        performance: str = "",
    ) -> Optional[dict]:
        """Analyze sentiment for a single stock.

        Returns:
            Dict with keys: score (-5 to 5), confidence (0-1),
                           reasoning (str), key_factors (list)
            Returns None if analysis fails.
        """
        if not self.enabled:
            return None

        # Skip if we recently hit the OpenAI daily rate limit.
        if self._rate_limited_until and time.time() < self._rate_limited_until:
            return None

        # Check cache (6h TTL — matches the refresh cadence to avoid re-analysis).
        cache_key = ticker
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if time.time() - cached["_timestamp"] < 6 * 3600:
                return cached

        # Fetch news
        news = fetch_stock_news(ticker, max_items=8)
        if not news:
            return {"score": 0, "confidence": 0.1, "reasoning": "No recent news available",
                    "key_factors": [], "_timestamp": time.time()}

        # Format headlines
        headlines = "\n".join(
            f"- {item['title']} ({item['publisher']})"
            for item in news
        )

        price_str = f"{price:,.2f}" if price else "N/A"
        prompt = SENTIMENT_PROMPT.format(
            company=company_name or ticker,
            ticker=ticker,
            headlines=headlines,
            price_str=price_str,
            performance=performance or "N/A",
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=300,
                messages=[{"role": "user", "content": prompt}],
            )

            text = response.choices[0].message.content.strip()

            # Handle potential markdown code blocks
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]

            result = json.loads(text)
            result["_timestamp"] = time.time()
            result["_ticker"] = ticker

            # Clamp score to valid range
            result["score"] = max(-5, min(5, int(result.get("score", 0))))
            result["confidence"] = max(0.0, min(1.0, float(result.get("confidence", 0.5))))

            self._cache[cache_key] = result
            self._save_cache()
            return result

        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate_limit_exceeded" in msg or "rate limit" in msg.lower():
                # Daily RPD hit — back off until UTC midnight when quota resets.
                self._rate_limited_until = time.time() + 60 * 60  # re-try in 1h
                print(f"OpenAI rate limit hit — backing off for 1h. ({ticker})")
            else:
                print(f"LLM analysis failed for {ticker}: {e}")
            return None

    def analyze_batch(
        self,
        ticker_news: Dict[str, tuple],
    ) -> Dict[str, dict]:
        """Analyze up to ~15 tickers in a single OpenAI call.

        Args:
            ticker_news: dict ticker -> (company_name, list_of_news_items)
                news items are dicts with 'title' and 'publisher' keys.

        Returns: dict ticker -> sentiment result. Tickers not in the response are omitted.
        """
        if not self.enabled or not ticker_news:
            return {}
        if self._rate_limited_until and time.time() < self._rate_limited_until:
            return {}

        # Build the prompt block per ticker.
        blocks = []
        for ticker, (company, news) in ticker_news.items():
            headlines = "\n".join(
                f"- {n['title']} ({n.get('publisher','')})" for n in news[:5]
            ) or "- (no recent news)"
            blocks.append(f"Ticker {ticker} ({company or ticker}):\n{headlines}")
        prompt = BATCH_SENTIMENT_PROMPT.format(
            stock_blocks="\n\n".join(blocks),
            count=len(ticker_news),
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
            )
            text = response.choices[0].message.content.strip()
            parsed = json.loads(text)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "rate_limit_exceeded" in msg or "rate limit" in msg.lower():
                self._rate_limited_until = time.time() + 60 * 60
                print(f"OpenAI rate limit hit on batch — backing off for 1h")
            else:
                print(f"LLM batch analysis failed: {e}")
            return {}

        results: Dict[str, dict] = {}
        now = time.time()
        for ticker in ticker_news:
            entry = parsed.get(ticker)
            if not isinstance(entry, dict):
                continue
            score = max(-5, min(5, int(entry.get("score", 0))))
            confidence = max(0.0, min(1.0, float(entry.get("confidence", 0.5))))
            result = {
                "score": score,
                "confidence": confidence,
                "reasoning": entry.get("reasoning", ""),
                "key_factors": entry.get("key_factors", []),
                "_timestamp": now,
                "_ticker": ticker,
            }
            self._cache[ticker] = result
            results[ticker] = result
        if results:
            self._save_cache()
        return results

    def analyze_multiple(
        self,
        tickers: List[str],
        names: Dict[str, str] = None,
        prices: Dict[str, float] = None,
        batch_size: int = 12,
    ) -> Dict[str, dict]:
        """Analyze sentiment for many stocks, batching ~12 per OpenAI call.

        Tickers hitting the 6h cache short-circuit without a new API call.
        Remaining tickers are grouped into batches of `batch_size` to minimize
        the request count (roughly N/12 calls instead of N).
        """
        if not self.enabled:
            return {}

        names = names or {}
        prices = prices or {}
        results: Dict[str, dict] = {}

        # Pull cache hits first so we only batch cache misses.
        pending: List[str] = []
        for ticker in tickers:
            cached = self._cache.get(ticker)
            if cached and time.time() - cached["_timestamp"] < 6 * 3600:
                results[ticker] = cached
                continue
            pending.append(ticker)

        if not pending:
            return results

        from .news_fetcher import fetch_stock_news

        # Build ticker -> (company, news) for each pending ticker.
        for i in range(0, len(pending), batch_size):
            if self._rate_limited_until and time.time() < self._rate_limited_until:
                remaining = int(self._rate_limited_until - time.time())
                print(f"OpenAI cooldown active — skipping {len(pending) - i} "
                      f"remaining tickers ({remaining}s left)")
                break
            chunk = pending[i : i + batch_size]
            ticker_news: Dict[str, tuple] = {}
            for ticker in chunk:
                news = fetch_stock_news(ticker, max_items=5)
                ticker_news[ticker] = (names.get(ticker, ""), news or [])
            batch_result = self.analyze_batch(ticker_news)
            results.update(batch_result)

        return results
