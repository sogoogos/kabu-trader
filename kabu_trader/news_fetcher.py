"""Fetch recent news for stocks.

Two modes:
- fetch_market_news: one RSS call per market, filtered locally by company name.
  Cheap — use this for bulk watchlists.
- fetch_stock_news: per-ticker yfinance call. Heavier; use only for one-off lookups.
"""

from __future__ import annotations

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, List


def shorten_url(url: str, timeout: int = 3) -> str:
    """Shorten a URL via is.gd. Returns the original on any failure."""
    if not url or len(url) <= 60:
        return url
    try:
        api = f"https://is.gd/create.php?format=simple&url={urllib.parse.quote(url, safe='')}"
        req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            short = resp.read().decode("utf-8").strip()
            if short.startswith("http"):
                return short
    except Exception:
        pass
    return url

import yfinance as yf


# Google News RSS — keyless, stable URL shape, returns ~30–50 recent business items.
_MARKET_FEEDS = {
    "JP": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=ja&gl=JP&ceid=JP:ja",
    "US": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
}


def fetch_market_news(
    market: str,
    watchlist_names: Dict[str, str],
    watchlist_aliases: Dict[str, List[str]] | None = None,
    timeout: int = 10,
) -> Dict[str, List[dict]]:
    """Fetch market-wide headlines once and attribute them to tickers by name match.

    Args:
        market: "JP" or "US".
        watchlist_names: ticker -> primary company name. Substring-matched against titles.
        watchlist_aliases: optional ticker -> list of extra short names / aliases
            (e.g. {"META": ["Meta", "Facebook"], "GOOGL": ["Alphabet", "Google"]}).
            A ticker matches a headline if ANY of its primary name or aliases is a substring.
        timeout: HTTP timeout in seconds.

    Returns:
        ticker -> list of matched headlines (schema: title, publisher, link, published).
    """
    aliases = watchlist_aliases or {}
    url = _MARKET_FEEDS.get(market.upper())
    if not url:
        return {}

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            xml_bytes = resp.read()
    except Exception:
        return {}

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return {}

    headlines: List[dict] = []
    for item in root.iter("item"):
        raw_title = (item.findtext("title") or "").strip()
        if not raw_title:
            continue
        source_el = item.find("source")
        publisher = (source_el.text or "").strip() if source_el is not None and source_el.text else ""
        # Google News appends " - <publisher>" to every title. Strip it so aliases
        # don't incorrectly match the publisher name (e.g. "Yahoo" matching
        # "Yahoo!ニュース" on unrelated articles).
        title = raw_title
        if publisher and title.endswith(" - " + publisher):
            title = title[: -len(publisher) - 3].rstrip()
        headlines.append({
            "title": title,
            "publisher": publisher,
            "link": (item.findtext("link") or "").strip(),
            "published": (item.findtext("pubDate") or "").strip(),
        })

    results: Dict[str, List[dict]] = {}
    for ticker, name in watchlist_names.items():
        needles = [n for n in ([name] + list(aliases.get(ticker, []))) if n]
        if not needles:
            continue
        matched = [h for h in headlines if any(n in h["title"] for n in needles)]
        if matched:
            results[ticker] = matched
    return results


def fetch_stock_news(ticker: str, max_items: int = 10) -> List[dict]:
    """Fetch recent news articles for a stock.

    Args:
        ticker: Stock ticker (e.g., "7203.T")
        max_items: Maximum number of news items to return

    Returns:
        List of dicts with keys: title, publisher, link, published
    """
    news = []
    for attempt in range(2):
        try:
            stock = yf.Ticker(ticker)
            news = stock.news or []
        except Exception:
            news = []
        if news:
            break
        if attempt == 0:
            time.sleep(1.5)

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
