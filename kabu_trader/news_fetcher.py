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
        results.append({
            "title": item.get("title", ""),
            "publisher": item.get("publisher", ""),
            "link": item.get("link", ""),
            "published": item.get("providerPublishTime", ""),
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
