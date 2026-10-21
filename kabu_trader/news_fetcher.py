"""Fetch recent news for stocks.

Two modes:
- fetch_market_news: one RSS call per market, filtered locally by company name.
  Cheap — use this for bulk watchlists.
- fetch_stock_news: per-ticker lookup with a fallback chain. Yahoo's /xhr/ncp
  endpoint behind yfinance ``Ticker.news`` has returned 404 since ~2026-10-01
  (yfinance swallows it as ``[]``), so we fall back to ``yf.Search`` (works for
  US tickers) and then to a Google News RSS search by company name (the only
  source that still returns per-stock headlines for .T tickers).
"""

from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional


def shorten_url(url: str, timeout: int = 3) -> str:
    """Shorten a URL via TinyURL. Returns the original on any failure."""
    if not url or len(url) <= 60:
        return url
    try:
        api = f"https://tinyurl.com/api-create.php?url={urllib.parse.quote(url, safe='')}"
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

# Google News RSS search — same parser as the headline feed. ``{q}`` is the
# URL-encoded query; ``when:Nd`` is appended at call time to limit the window.
_GNEWS_SEARCH = {
    "JP": "https://news.google.com/rss/search?q={q}&hl=ja&gl=JP&ceid=JP:ja",
    "US": "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en",
}


def _market_for(ticker: str) -> str:
    return "JP" if ticker.upper().endswith(".T") else "US"


def _fetch_rss(url: str, timeout: int = 10) -> List[dict]:
    """GET a Google News RSS URL and return normalized headline dicts (newest first as served)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            xml_bytes = resp.read()
    except Exception:
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

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
    return headlines


def _parse_pubdate(value: str) -> Optional[datetime]:
    try:
        dt = parsedate_to_datetime(value)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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

    headlines = _fetch_rss(url, timeout=timeout)
    if not headlines:
        return {}

    results: Dict[str, List[dict]] = {}
    for ticker, name in watchlist_names.items():
        needles = [n for n in ([name] + list(aliases.get(ticker, []))) if n]
        if not needles:
            continue
        matched = [h for h in headlines if any(n in h["title"] for n in needles)]
        if matched:
            results[ticker] = matched
    return results


def _normalize_yf_news(items: List[dict], max_items: int) -> List[dict]:
    """Map yfinance news items (old flat and new ``content``-nested schema) to our schema."""
    results: List[dict] = []
    for item in items[:max_items]:
        if not isinstance(item, dict):
            continue
        # yfinance nests data under "content" in newer versions
        content = item.get("content", item) or {}
        provider = content.get("provider", {}) or {}
        canonical = content.get("canonicalUrl", {}) or {}

        title = content.get("title", "") or item.get("title", "")
        publisher = provider.get("displayName", "") or item.get("publisher", "")
        link = canonical.get("url", "") or item.get("link", "")
        published = content.get("pubDate", "") or item.get("providerPublishTime", "")
        if isinstance(published, (int, float)):
            # Search items carry an epoch; keep the schema string-typed.
            published = datetime.fromtimestamp(published, tz=timezone.utc).isoformat()

        if title:
            results.append({
                "title": title,
                "publisher": publisher,
                "link": link,
                "published": published,
            })
    return results


def _yf_ticker_news(ticker: str) -> List[dict]:
    """yfinance ``Ticker.news``. Dead since ~2026-10-01 (Yahoo 404) but kept first in
    case Yahoo restores it — it is the richest source when it works."""
    try:
        return list(yf.Ticker(ticker).news or [])
    except Exception:
        return []


def _yf_search_news(ticker: str, max_items: int) -> List[dict]:
    """yfinance ``Search(...).news`` restricted to items tagged with this ticker.

    Yahoo's search endpoint still serves ticker-tagged news for US symbols but
    returns nothing for .T symbols, so this mostly helps the US watchlist.
    """
    try:
        items = yf.Search(ticker, news_count=max_items).news or []
    except Exception:
        return []
    tagged = [
        it for it in items
        if isinstance(it, dict) and ticker in (it.get("relatedTickers") or [])
    ]
    return tagged


def fetch_company_news_rss(
    company_name: str,
    market: str,
    max_items: int = 10,
    max_age_days: int = 7,
    timeout: int = 10,
) -> List[dict]:
    """Google News RSS search for a company name, newest first, within ``max_age_days``.

    Google returns up to ~100 items spanning months and not strictly date-ordered,
    so we add a ``when:`` operator to the query AND filter/sort locally.
    """
    name = (company_name or "").strip()
    template = _GNEWS_SEARCH.get(market.upper())
    if not name or not template:
        return []

    # Exact-phrase match on the name cuts cross-company noise (e.g. a 商社 roundup
    # surfacing for every trading house); ``when:`` bounds the window server-side.
    query = f'"{name}" when:{max_age_days}d'
    url = template.format(q=urllib.parse.quote(query, safe=""))
    headlines = _fetch_rss(url, timeout=timeout)
    if not headlines:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    dated: List[tuple] = []
    for h in headlines:
        dt = _parse_pubdate(h.get("published", ""))
        if dt is None or dt < cutoff:
            continue
        dated.append((dt, h))
    dated.sort(key=lambda p: p[0], reverse=True)
    return [h for _, h in dated[:max_items]]


def fetch_stock_news(ticker: str, max_items: int = 10, company_name: str = "") -> List[dict]:
    """Fetch recent news articles for a stock.

    Fallback chain, first non-empty wins:
      1. yfinance ``Ticker.news``   (Yahoo endpoint currently 404 → usually empty)
      2. yfinance ``Search.news``   (US tickers only in practice)
      3. Google News RSS search by ``company_name`` (needed for .T tickers)

    Args:
        ticker: Stock ticker (e.g., "7203.T")
        max_items: Maximum number of news items to return
        company_name: Company name used for the RSS search fallback. Without it
            only the yfinance sources are tried.

    Returns:
        List of dicts with keys: title, publisher, link, published
    """
    results = _normalize_yf_news(_yf_ticker_news(ticker), max_items)
    if results:
        return results

    results = _normalize_yf_news(_yf_search_news(ticker, max_items), max_items)
    if results:
        return results

    if company_name:
        return fetch_company_news_rss(company_name, _market_for(ticker), max_items=max_items)
    return []


def fetch_multiple_news(
    tickers: List[str],
    max_per_stock: int = 5,
    names: Dict[str, str] | None = None,
) -> Dict[str, List[dict]]:
    """Fetch news for multiple stocks.

    Returns:
        Dict of ticker -> list of news items
    """
    names = names or {}
    results = {}
    for ticker in tickers:
        news = fetch_stock_news(ticker, max_per_stock, company_name=names.get(ticker, ""))
        if news:
            results[ticker] = news
    return results
