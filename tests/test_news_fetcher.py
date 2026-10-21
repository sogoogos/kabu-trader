"""Unit tests for the per-stock news fallback chain (no network)."""

import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest import mock

from kabu_trader import news_fetcher as nf


def _rss(items):
    """Build a minimal Google-News-style RSS document."""
    body = "".join(
        f"<item><title>{t} - {p}</title><link>{l}</link>"
        f"<pubDate>{d}</pubDate><source url='x'>{p}</source></item>"
        for t, p, l, d in items
    )
    return f"<rss><channel>{body}</channel></rss>".encode("utf-8")


class ParseTests(unittest.TestCase):
    def test_fetch_rss_strips_publisher_suffix(self):
        now = format_datetime(datetime.now(timezone.utc))
        xml = _rss([("トヨタ、新型車発表", "日本経済新聞", "http://a", now)])
        with mock.patch("urllib.request.urlopen") as uo:
            uo.return_value.__enter__.return_value.read.return_value = xml
            out = nf._fetch_rss("http://feed")
        self.assertEqual(out[0]["title"], "トヨタ、新型車発表")
        self.assertEqual(out[0]["publisher"], "日本経済新聞")

    def test_market_for(self):
        self.assertEqual(nf._market_for("7203.T"), "JP")
        self.assertEqual(nf._market_for("AAPL"), "US")

    def test_normalize_handles_both_yf_schemas(self):
        flat = {"title": "Old", "publisher": "P", "link": "L", "providerPublishTime": 1}
        nested = {"content": {"title": "New", "provider": {"displayName": "Q"},
                              "canonicalUrl": {"url": "U"}, "pubDate": "2026-10-21"}}
        out = nf._normalize_yf_news([flat, nested, {"content": {}}], 10)
        self.assertEqual([o["title"] for o in out], ["Old", "New"])
        self.assertEqual(out[1]["publisher"], "Q")


class CompanyRssTests(unittest.TestCase):
    def test_filters_by_age_and_sorts_newest_first(self):
        now = datetime.now(timezone.utc)
        items = [
            ("old", "p", "l1", format_datetime(now - timedelta(days=30))),
            ("mid", "p", "l2", format_datetime(now - timedelta(days=3))),
            ("new", "p", "l3", format_datetime(now - timedelta(hours=1))),
            ("nodate", "p", "l4", ""),
        ]
        with mock.patch.object(nf, "_fetch_rss", return_value=_parsed(items)):
            out = nf.fetch_company_news_rss("トヨタ自動車", "JP", max_items=5, max_age_days=7)
        self.assertEqual([o["title"] for o in out], ["new", "mid"])

    def test_empty_name_or_unknown_market(self):
        self.assertEqual(nf.fetch_company_news_rss("", "JP"), [])
        self.assertEqual(nf.fetch_company_news_rss("X", "EU"), [])


def _parsed(items):
    return [{"title": t, "publisher": p, "link": l, "published": d} for t, p, l, d in items]


class FallbackChainTests(unittest.TestCase):
    def setUp(self):
        self.now = format_datetime(datetime.now(timezone.utc))

    def test_ticker_news_wins_when_present(self):
        with mock.patch.object(nf, "_yf_ticker_news", return_value=[{"title": "T"}]), \
             mock.patch.object(nf, "_yf_search_news") as search, \
             mock.patch.object(nf, "fetch_company_news_rss") as rss:
            out = nf.fetch_stock_news("AAPL", 5, company_name="Apple")
        self.assertEqual(out[0]["title"], "T")
        search.assert_not_called()
        rss.assert_not_called()

    def test_search_used_when_ticker_news_empty(self):
        with mock.patch.object(nf, "_yf_ticker_news", return_value=[]), \
             mock.patch.object(nf, "_yf_search_news", return_value=[{"title": "S"}]), \
             mock.patch.object(nf, "fetch_company_news_rss") as rss:
            out = nf.fetch_stock_news("AAPL", 5, company_name="Apple")
        self.assertEqual(out[0]["title"], "S")
        rss.assert_not_called()

    def test_rss_used_for_jp_when_yahoo_sources_empty(self):
        with mock.patch.object(nf, "_yf_ticker_news", return_value=[]), \
             mock.patch.object(nf, "_yf_search_news", return_value=[]), \
             mock.patch.object(nf, "fetch_company_news_rss",
                               return_value=[{"title": "R"}]) as rss:
            out = nf.fetch_stock_news("7203.T", 5, company_name="トヨタ自動車")
        self.assertEqual(out[0]["title"], "R")
        rss.assert_called_once_with("トヨタ自動車", "JP", max_items=5)

    def test_no_company_name_skips_rss(self):
        with mock.patch.object(nf, "_yf_ticker_news", return_value=[]), \
             mock.patch.object(nf, "_yf_search_news", return_value=[]), \
             mock.patch.object(nf, "fetch_company_news_rss") as rss:
            self.assertEqual(nf.fetch_stock_news("7203.T", 5), [])
        rss.assert_not_called()

    def test_search_filters_related_tickers(self):
        fake = mock.Mock()
        fake.news = [
            {"title": "A", "relatedTickers": ["AAPL"]},
            {"title": "B", "relatedTickers": ["MSFT"]},
            {"title": "C"},
        ]
        with mock.patch.object(nf.yf, "Search", return_value=fake):
            out = nf._yf_search_news("AAPL", 5)
        self.assertEqual([o["title"] for o in out], ["A"])


if __name__ == "__main__":
    unittest.main()
