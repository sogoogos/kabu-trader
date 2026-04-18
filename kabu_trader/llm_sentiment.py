"""LLM-based sentiment analysis for Japanese stocks using OpenAI API.

Analyzes news headlines and generates structured sentiment scores
that feed into the composite trading signal.
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional, Tuple

from .news_fetcher import fetch_stock_news


SENTIMENT_PROMPT = """You are a Japanese stock market analyst. Analyze the following news headlines for {company} ({ticker}) and provide a trading sentiment assessment.

News headlines:
{headlines}

Additional context:
- Current price: ¥{price:,.0f}
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


class LLMSentimentAnalyzer:
    """Analyzes stock news sentiment using OpenAI API."""

    def __init__(self, config: dict):
        self.enabled = config.get("enabled", False)
        if not self.enabled:
            return

        self.api_key = config.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        self.model = config.get("model", "gpt-4o-mini")
        self._cache: Dict[str, dict] = {}

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

        # Check cache (cache for 1 hour)
        cache_key = ticker
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            if time.time() - cached["_timestamp"] < 3600:
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

        prompt = SENTIMENT_PROMPT.format(
            company=company_name or ticker,
            ticker=ticker,
            headlines=headlines,
            price=price,
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
            return result

        except Exception as e:
            print(f"LLM analysis failed for {ticker}: {e}")
            return None

    def analyze_multiple(
        self,
        tickers: List[str],
        names: Dict[str, str] = None,
        prices: Dict[str, float] = None,
    ) -> Dict[str, dict]:
        """Analyze sentiment for multiple stocks.

        Args:
            tickers: List of stock tickers
            names: Dict of ticker -> company name
            prices: Dict of ticker -> current price

        Returns:
            Dict of ticker -> sentiment result
        """
        if not self.enabled:
            return {}

        names = names or {}
        prices = prices or {}
        results = {}

        for ticker in tickers:
            result = self.analyze_stock(
                ticker,
                company_name=names.get(ticker, ""),
                price=prices.get(ticker, 0),
            )
            if result:
                results[ticker] = result

        return results
