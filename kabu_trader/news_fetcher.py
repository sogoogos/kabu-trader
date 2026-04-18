"""Fetch recent news for Japanese stocks via yfinance."""

from __future__ import annotations

import yfinance as yf
from typing import Dict, List


def fetch_stock_news(ticker: str, max_items: int = 10) -> List[dict]:
    """Fetch recent news articles for a stock.

    Args:
        ticker: Stock ticker (e.g., "7203.T")
        max_items: Maximum number of news items to return

    Returns:
        List of dicts with keys: title, publisher, link, published
    """
    try:
        stock = yf.Ticker(ticker)
        news = stock.news or []
    except Exception:
        return []

    results = []
    for item in news[:max_items]:
        # yfinance nests data under "content" in newer versions
        content = item.get("content", item)
        provider = content.get("provider", {})
        canonical = content.get("canonicalUrl", {})

        title = content.get("title", "") or item.get("title", "")
        publisher = provider.get("displayName", "") or item.get("publisher", "")
        link = canonical.get("url", "") or item.get("link", "")
        published = content.get("pubDate", "") or item.get("providerPublishTime", "")

        if title:
            results.append({
                "title": title,
                "publisher": publisher,
                "link": link,
                "published": published,
            })

    return results


def fetch_multiple_news(tickers: List[str], max_per_stock: int = 5) -> Dict[str, List[dict]]:
    """Fetch news for multiple stocks.

    Returns:
        Dict of ticker -> list of news items
    """
    results = {}
    for ticker in tickers:
        news = fetch_stock_news(ticker, max_per_stock)
        if news:
            results[ticker] = news
    return results
